import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import CVUploadForm, ProfileForm, UserPreferencesForm
from .models import CV, Job, SearchRun, UserPreferences
from .services.ats_checker import check_cv_format
from .services.excel_export import build_workbook
from .services.locations import cities_by_country_name
from .services.keyword_extractor import (
    extract_cv_profile,
    extract_search_keywords,
    get_salary_range,
)
from .tasks import process_job_search
from .utils import SESSION_ACTIVE_CV, extract_cv_text, resolve_active_cv

logger = logging.getLogger(__name__)


def get_effective_min_salary(user):
    """Resolve a user's minimum salary: pref -> profile default -> system default."""
    preferences = UserPreferences.objects.filter(user=user).first()
    if preferences and preferences.salary_min is not None:
        return preferences.salary_min
    profile = getattr(user, 'profile', None)
    if profile and profile.default_min_salary is not None:
        return profile.default_min_salary
    return settings.DEFAULT_MIN_SALARY


def get_effective_salary_range(user):
    """Return (min, max) salary for a user. Max is None when no upper limit."""
    preferences = UserPreferences.objects.filter(user=user).first()
    salary_max = preferences.salary_max if preferences else None
    return get_effective_min_salary(user), salary_max


@login_required
def dashboard(request):
    """Landing page after login: active profile, CV status, prefs, search runs."""
    profile = request.user.profile
    active_cv = resolve_active_cv(request)
    preferences = UserPreferences.objects.filter(user=request.user).first()

    # Search runs for the active profile (plus any legacy runs with no CV link).
    runs_qs = SearchRun.objects.filter(user=request.user)
    if active_cv:
        runs_qs = runs_qs.filter(Q(cv=active_cv) | Q(cv__isnull=True))
    search_runs = list(runs_qs[:20])

    effective_min_salary, effective_max_salary = get_effective_salary_range(request.user)

    # Tailored CVs generated for the active profile (most recent first).
    tailored_qs = Job.objects.filter(search_run__user=request.user).exclude(tailored_pdf='')
    if active_cv:
        tailored_qs = tailored_qs.filter(
            Q(search_run__cv=active_cv) | Q(search_run__cv__isnull=True)
        )
    tailored_jobs = tailored_qs.order_by('-search_run__created_at', '-match_score')[:50]

    # Dashboard stat cards (scoped to the active profile's runs).
    jobs_qs = Job.objects.filter(search_run__in=runs_qs)
    last_run = runs_qs.order_by('-created_at').first()
    stats = {
        'total_jobs': jobs_qs.count(),
        'matched': jobs_qs.filter(match_score__gte=settings.MATCH_THRESHOLD).count(),
        'tailored': jobs_qs.exclude(tailored_pdf='').count(),
        'last_search': last_run.created_at if last_run else None,
    }

    context = {
        'profile': profile,
        'active_cv': active_cv,
        'preferences': preferences,
        'search_runs': search_runs,
        'effective_min_salary': effective_min_salary,
        'effective_max_salary': effective_max_salary,
        'tailored_jobs': tailored_jobs,
        'stats': stats,
        'MATCH_THRESHOLD': settings.MATCH_THRESHOLD,
    }
    return render(request, 'dashboard.html', context)


@login_required
@require_POST
def create_profile(request):
    """Create a new (empty) CV profile / tab and switch to it."""
    form = ProfileForm(request.POST)
    if form.is_valid():
        cv = form.save(commit=False)
        cv.user = request.user
        cv.parsed_data = {}
        cv.save()
        request.session[SESSION_ACTIVE_CV] = cv.pk
        messages.success(
            request,
            f'Profile "{cv.display_name}" created. Upload a CV file for it.',
        )
    else:
        errors = form.errors.get('name') or ['Invalid profile name.']
        messages.error(request, errors[0])
    return redirect('dashboard')


@login_required
@require_POST
def delete_cv(request, cv_id):
    """Delete a CV profile (file + record) and switch to another profile."""
    cv = get_object_or_404(CV, pk=cv_id, user=request.user)
    name = cv.display_name
    if cv.original_file:
        cv.original_file.delete(save=False)
    cv.delete()

    remaining = CV.objects.filter(user=request.user).order_by('id').first()
    if remaining:
        request.session[SESSION_ACTIVE_CV] = remaining.pk
    else:
        request.session.pop(SESSION_ACTIVE_CV, None)
    messages.success(request, f'Profile "{name}" deleted.')
    return redirect('dashboard')


@login_required
def upload_cv(request):
    """Upload a PDF/DOCX file into the active profile (or a new one)."""
    active_cv = resolve_active_cv(request)
    if request.method == 'POST':
        # Update the active profile in place, or create a fresh CV if none.
        form = CVUploadForm(request.POST, request.FILES, instance=active_cv)
        if form.is_valid():
            cv = form.save(commit=False)
            cv.user = request.user
            if not cv.name:
                cv.name = request.user.profile.candidate_name or 'Profile'
            try:
                parsed_text = extract_cv_text(cv.original_file)
            except ValidationError as exc:
                form.add_error('original_file', exc)
            except Exception as exc:  # pragma: no cover - defensive
                form.add_error(
                    'original_file',
                    f'Could not read the file: {exc}',
                )
            else:
                cv.parsed_text = parsed_text
                # Mine skills and the roles this candidate should search for; these
                # drive the job-search keywords and the keyword pre-scoring stage.
                profile_data = extract_cv_profile(parsed_text)
                cv.parsed_data = {
                    'raw_text': parsed_text,
                    'skills': profile_data['skills'],
                    'job_titles': profile_data['job_titles'],
                    'experience': [],
                    'education': [],
                }
                cv.save()  # save first, so the file is on disk for the ATS check

                # Phase 1 is a property of the CV file itself, not of any one job,
                # so it runs once here rather than per job during a search.
                ats_format = _check_cv_ats_format(cv, parsed_text)
                cv.parsed_data['ats_format'] = ats_format
                cv.save(update_fields=['parsed_data'])

                request.session[SESSION_ACTIVE_CV] = cv.pk
                messages.success(
                    request,
                    'CV uploaded and parsed. Search keywords: '
                    + (', '.join(profile_data['job_titles'][:5]) or 'default'),
                )
                _report_ats_format(request, ats_format)
                return redirect('dashboard')
    else:
        form = CVUploadForm(instance=active_cv)

    return render(request, 'jobs/upload_cv.html', {'form': form, 'active_cv': active_cv})


def _check_cv_ats_format(cv, parsed_text):
    """Phase 1 ATS check on the uploaded file. Never blocks the upload."""
    try:
        path = cv.original_file.path if cv.original_file else None
    except (NotImplementedError, ValueError):
        path = None  # non-filesystem storage backend
    try:
        return check_cv_format(parsed_text, file_path=path)
    except Exception:
        logger.exception('ATS format check failed for CV %s', cv.pk)
        return {}


def _report_ats_format(request, ats_format):
    """Surface Phase 1 findings to the user as a message."""
    if not ats_format:
        return
    if ats_format.get('pass'):
        messages.success(request, 'ATS format check passed — this CV is machine-readable.')
        return

    problems = []
    if not ats_format.get('file_format_ok'):
        problems.append('the file is not machine-readable')
    if ats_format.get('prohibited_elements'):
        problems.append('it contains ' + ', '.join(ats_format['prohibited_elements']))
    if ats_format.get('layout') not in ('single-column', 'unknown', None):
        problems.append(f'the layout is {ats_format["layout"]}')
    if ats_format.get('missing_headers'):
        problems.append(
            'these sections are missing: ' + ', '.join(ats_format['missing_headers'])
        )
    messages.warning(
        request,
        'ATS format warning — an ATS may struggle with this CV because '
        + '; '.join(problems)
        + '. See the recommendations on your dashboard.',
    )


@login_required
def ats_report(request, job_id):
    """Full phase-by-phase ATS report for one job's tailored CV."""
    job = get_object_or_404(Job, pk=job_id, search_run__user=request.user)
    report = getattr(job, 'ats_report', None)
    if report is None:
        messages.info(request, 'No ATS report was generated for this job.')
        return redirect('search_results', run_id=job.search_run_id)

    return render(request, 'jobs/ats_report.html', {
        'job': job,
        'report': report,
        'data': report.report_data or {},
        'ATS_THRESHOLD': settings.ATS_THRESHOLD,
    })


@login_required
def edit_preferences(request):
    """Create or edit the logged-in user's job search preferences."""
    preferences, _ = UserPreferences.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = UserPreferencesForm(request.POST, instance=preferences)
        if form.is_valid():
            form.save()
            messages.success(request, 'Preferences saved.')
            return redirect('dashboard')
    else:
        form = UserPreferencesForm(instance=preferences)

    return render(
        request, 'jobs/preferences.html',
        {
            'form': form,
            'preferences': preferences,
            # Rendered into the page as JSON so the city dropdown is populated
            # client-side, with no external API call.
            'city_map': cities_by_country_name(),
        },
    )


@login_required
def profile(request):
    """The logged-in user's account details, preferences and CV profiles.

    Deliberately shows no password: Django stores only a salted hash and cannot
    recover the original, so there is nothing to show. "Password last changed" is
    the closest honest signal available, and it is only approximate — see below.
    """
    user = request.user
    preferences = UserPreferences.objects.filter(user=user).first()
    active_cv = resolve_active_cv(request)

    return render(request, 'jobs/profile.html', {
        'profile': getattr(user, 'profile', None),
        'preferences': preferences,
        'active_cv': active_cv,
        'cv_count': CV.objects.filter(user=user).count(),
        'search_count': SearchRun.objects.filter(user=user).count(),
        'effective_min_salary': get_effective_min_salary(user),
    })


@login_required
@require_POST
def start_search(request):
    """Create a PENDING SearchRun for the active profile and enqueue the task."""
    active_cv = resolve_active_cv(request)
    if active_cv is None or not active_cv.has_file:
        messages.error(
            request,
            'Please upload a CV for this profile before starting a search.',
        )
        return redirect('dashboard')

    preferences = UserPreferences.objects.filter(user=request.user).first()
    countries = (preferences.target_countries if preferences else None) or ['United Kingdom']
    city = (preferences.target_city if preferences else '') or ''
    min_salary, max_salary = get_effective_salary_range(request.user)

    # If the user never set a minimum, derive a sensible one from the CV's target
    # roles instead of a flat system default (a junior role shouldn't be filtered
    # out by a £30k floor, and a director role shouldn't use one either).
    if preferences is None or preferences.salary_min is None:
        keywords = extract_search_keywords(active_cv.parsed_data or {})
        role_min, _role_max = get_salary_range(keywords, default_min=int(min_salary))
        min_salary = role_min
        logger.info('Derived role-based minimum salary %s from %s', role_min, keywords)

    search_run = SearchRun.objects.create(
        user=request.user,
        cv=active_cv,
        countries=countries,
        # Snapshot the city on the run, so changing preferences later doesn't
        # rewrite what this search actually looked for.
        city=city,
        min_salary=min_salary,
        max_salary=max_salary,
        status=SearchRun.STATUS_PENDING,
        progress=0,
    )

    # Hand off to Celery; returns immediately so the request never blocks.
    process_job_search.delay(search_run.pk)
    logger.info('Enqueued search %s for user %s', search_run.pk, request.user.username)

    messages.success(
        request,
        'Search started — this runs in the background. '
        'Check the results shortly; the status will update automatically.',
    )
    return redirect('dashboard')


@login_required
def search_status(request, run_id):
    """Return the SearchRun status + progress as JSON (used by dashboard polling)."""
    search_run = get_object_or_404(SearchRun, pk=run_id, user=request.user)
    processed = search_run.jobs.count()
    eta = search_run.eta_seconds()
    return JsonResponse({
        'id': search_run.pk,
        'status': search_run.status,
        'status_display': search_run.get_status_display(),
        'progress': search_run.progress,
        'phase': _progress_phase(search_run),
        'eta_seconds': eta,
        'eta_display': _format_eta(eta),
        'error_message': search_run.error_message,
        'job_count': processed,
        'processed': processed,
        'total': search_run.total_jobs,
    })


def _progress_phase(search_run):
    """Which phase the bar is in, so the UI can say what is happening, not just %."""
    if search_run.status != SearchRun.STATUS_RUNNING:
        return ''
    progress = search_run.progress
    if progress < 15:
        return 'Fetching jobs'
    if progress < 75:
        return 'Scoring against your CV'
    if progress < 95:
        return 'Tailoring CVs'
    return 'Finalising'


def _format_eta(seconds):
    """Human ETA. Deliberately coarse: a to-the-second estimate implies a
    precision this extrapolation does not have."""
    if seconds is None:
        return 'Estimating…'
    if seconds < 60:
        return 'less than a minute'
    minutes = int(round(seconds / 60.0))
    if minutes == 1:
        return 'about 1 minute'
    if minutes < 60:
        return f'about {minutes} minutes'
    hours, minutes = divmod(minutes, 60)
    return f'about {hours}h {minutes}m'


@login_required
@require_POST
def clear_search_history(request):
    """Delete all of the current user's search runs (and their jobs, by cascade)."""
    deleted, _ = SearchRun.objects.filter(user=request.user).delete()
    messages.success(request, 'Search history cleared.')
    logger.info('User %s cleared search history (%s rows)', request.user.username, deleted)
    return redirect('dashboard')


@login_required
def search_results(request, run_id):
    """List all jobs found for a given search run, ordered by real match score."""
    search_run = get_object_or_404(SearchRun, pk=run_id, user=request.user)
    threshold = settings.MATCH_THRESHOLD

    # Every job the search found is shown here — nothing is rejected and nothing
    # is withheld. The three tabs filter client-side, so switching between them is
    # instant. (The Excel export stays limited to >= threshold — that is a "what
    # should I act on" artefact, whereas this page is "what did the search find".)
    #
    # An unscored job (empty advert, nothing to match against) sorts last rather
    # than being treated as a zero: its score is unknown, not bad.
    jobs = list(
        search_run.jobs
        .select_related('ats_report')
        .order_by(F('match_score').desc(nulls_last=True), 'title')
    )
    scored = [j for j in jobs if j.match_score is not None]
    above = [j for j in scored if j.match_score >= threshold]

    return render(
        request,
        'jobs/search_results.html',
        {
            'search_run': search_run,
            'jobs': jobs,
            'MATCH_THRESHOLD': threshold,
            'ATS_THRESHOLD': settings.ATS_THRESHOLD,
            'total_found': len(jobs),
            'above_count': len(above),
            # Everything that isn't above the bar, including the unscored: the two
            # tabs must account for every row in "All Jobs".
            'below_count': len(jobs) - len(above),
            'not_scored_count': len(jobs) - len(scored),
        },
    )



@login_required
def export_excel(request, run_id):
    """Download a formatted .xlsx of a completed search run's jobs."""
    search_run = get_object_or_404(SearchRun, pk=run_id, user=request.user)
    if search_run.status != SearchRun.STATUS_COMPLETED:
        messages.error(request, 'Excel export is only available for completed searches.')
        return redirect('search_results', run_id=search_run.pk)

    buffer = build_workbook(search_run)
    return FileResponse(
        buffer,
        as_attachment=True,
        filename=f'search_results_{search_run.pk}.xlsx',
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@login_required
def search_jobs_json(request, run_id):
    """Return paginated JSON job results for a given search run.

    Supports ?page=N&page_size=N query parameters for lazy loading.
    Used by the frontend for caching and incremental rendering.
    """
    search_run = get_object_or_404(SearchRun, pk=run_id, user=request.user)
    threshold = settings.MATCH_THRESHOLD
    ats_threshold = settings.ATS_THRESHOLD

    try:
        page = max(1, int(request.GET.get('page', 1)))
        page_size = max(1, min(100, int(request.GET.get('page_size', 25))))
    except (ValueError, TypeError):
        page = 1
        page_size = 25

    filter_tab = request.GET.get('filter', 'all')  # 'all', 'above', 'below'

    jobs_qs = (
        search_run.jobs
        .select_related('ats_report')
        .order_by(F('match_score').desc(nulls_last=True), 'title')
    )

    if filter_tab == 'above':
        jobs_qs = jobs_qs.filter(match_score__gte=threshold)
    elif filter_tab == 'below':
        from django.db.models import Q as _Q
        jobs_qs = jobs_qs.filter(
            _Q(match_score__lt=threshold) | _Q(match_score__isnull=True)
        )

    total = jobs_qs.count()
    offset = (page - 1) * page_size
    jobs_page = list(jobs_qs[offset: offset + page_size])

    def _sponsorship_label(job):
        if job.sponsorship_flag == 'SPONSORED':
            return 'Sponsored'
        return job.get_sponsorship_flag_display()

    jobs_data = []
    for job in jobs_page:
        ats_score = None
        ats_url = None
        if hasattr(job, 'ats_report') and job.ats_report is not None:
            ats_score = job.ats_score
            ats_url = f'/job/{job.pk}/ats/'

        jobs_data.append({
            'id': job.pk,
            'title': job.title,
            'company': job.company or '',
            'location': job.location or '',
            'salary': job.salary or '',
            'sponsorship_flag': job.sponsorship_flag,
            'sponsorship_label': _sponsorship_label(job),
            'match_score': job.match_score,
            'match_reason': job.match_reason or '',
            'missing_skills': job.missing_skills or [],
            'above_salary_preference': job.above_salary_preference,
            'ats_score': ats_score,
            'ats_url': ats_url,
            'tailored_pdf_url': job.tailored_pdf.url if job.tailored_pdf else None,
            'application_link': job.application_link or '',
            'score_class': (
                'high' if (job.match_score is not None and job.match_score >= threshold)
                else 'medium' if (job.match_score is not None and job.match_score >= 50)
                else 'low' if job.match_score is not None
                else 'unscored'
            ),
        })

    all_jobs = list(search_run.jobs.order_by(F('match_score').desc(nulls_last=True)))
    scored = [j for j in all_jobs if j.match_score is not None]
    above_count = sum(1 for j in scored if j.match_score >= threshold)

    return JsonResponse({
        'run_id': run_id,
        'status': search_run.status,
        'total': total,
        'total_all': len(all_jobs),
        'above_count': above_count,
        'below_count': len(all_jobs) - above_count,
        'not_scored_count': len(all_jobs) - len(scored),
        'page': page,
        'page_size': page_size,
        'has_more': (offset + page_size) < total,
        'match_threshold': threshold,
        'ats_threshold': ats_threshold,
        'jobs': jobs_data,
    })
