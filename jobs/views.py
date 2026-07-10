import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import CVUploadForm, UserPreferencesForm
from .models import CV, Job, SearchRun, UserPreferences
from .services.excel_export import build_workbook
from .tasks import process_job_search
from .utils import extract_cv_text

logger = logging.getLogger(__name__)


def get_effective_min_salary(user):
    """Resolve a user's minimum salary: pref -> profile default -> system default."""
    preferences = UserPreferences.objects.filter(user=user).first()
    if preferences and preferences.min_salary is not None:
        return preferences.min_salary
    profile = getattr(user, 'profile', None)
    if profile and profile.default_min_salary is not None:
        return profile.default_min_salary
    return settings.DEFAULT_MIN_SALARY


@login_required
def dashboard(request):
    """Landing page after login: profile, CV status, preferences, search runs."""
    profile = request.user.profile
    latest_cv = CV.objects.filter(user=request.user).first()
    preferences = UserPreferences.objects.filter(user=request.user).first()
    search_runs = SearchRun.objects.filter(user=request.user)[:20]

    effective_min_salary = get_effective_min_salary(request.user)

    # Tailored CVs generated across the user's searches (most recent first).
    tailored_jobs = (
        Job.objects.filter(search_run__user=request.user)
        .exclude(tailored_pdf='')
        .order_by('-search_run__created_at', '-match_score')[:50]
    )

    context = {
        'profile': profile,
        'latest_cv': latest_cv,
        'preferences': preferences,
        'search_runs': search_runs,
        'effective_min_salary': effective_min_salary,
        'tailored_jobs': tailored_jobs,
    }
    return render(request, 'dashboard.html', context)


@login_required
def upload_cv(request):
    """Upload a PDF/DOCX CV, extract its text and store structured data."""
    if request.method == 'POST':
        form = CVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            cv = form.save(commit=False)
            cv.user = request.user
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
                # Simple structured data for now; refined in a later phase.
                cv.parsed_data = {
                    'raw_text': parsed_text,
                    'skills': [],
                    'experience': [],
                    'education': [],
                }
                cv.save()
                messages.success(request, 'CV uploaded and parsed successfully.')
                return redirect('dashboard')
    else:
        form = CVUploadForm()

    return render(request, 'jobs/upload_cv.html', {'form': form})


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

    return render(request, 'jobs/preferences.html', {'form': form})


@login_required
@require_POST
def start_search(request):
    """Create a PENDING SearchRun and enqueue the async job-search task."""
    latest_cv = CV.objects.filter(user=request.user).first()
    if latest_cv is None:
        messages.error(request, 'Please upload a CV before starting a search.')
        return redirect('dashboard')

    preferences = UserPreferences.objects.filter(user=request.user).first()
    countries = (preferences.target_countries if preferences else None) or ['United Kingdom']
    min_salary = get_effective_min_salary(request.user)

    search_run = SearchRun.objects.create(
        user=request.user,
        countries=countries,
        min_salary=min_salary,
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
    return JsonResponse({
        'id': search_run.pk,
        'status': search_run.status,
        'status_display': search_run.get_status_display(),
        'progress': search_run.progress,
        'error_message': search_run.error_message,
        'job_count': search_run.jobs.count(),
    })


@login_required
def search_results(request, run_id):
    """List all jobs found for a given search run, ordered by match score."""
    search_run = get_object_or_404(SearchRun, pk=run_id, user=request.user)
    jobs = search_run.jobs.all().order_by('-match_score', 'title')
    return render(
        request,
        'jobs/search_results.html',
        {'search_run': search_run, 'jobs': jobs},
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
