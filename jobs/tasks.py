"""Celery tasks and the core asynchronous job-search workflow.

``run_job_search`` holds the plain (non-Celery) logic so it can be unit-tested
directly. ``process_job_search`` is the Celery task wrapper invoked via
``.delay(search_run_id)`` from the view.
"""
import logging
import os
import tempfile

from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.core.mail import send_mail

from .models import ATSReport, CV, Job, SearchRun
from .services.apify_service import ApifyConfigError, ApifySearchError, search_jobs
from .services.ats_checker import ATSChecker, extract_job_requirements
from .services.google_sheets import GoogleSheetsLogger
from .services.job_keywords import (
    all_contract_terms,
    contract_summary,
    extract_job_keywords,
    term_present,
)
from .services.keyword_extractor import (
    extract_search_keywords,
    extract_skills_from_text,
    keyword_match_score,
    missing_skills,
)
from .services.matching import (
    compute_match_score,
    detect_sponsorship,
    salary_within_range,
)
from .services.pdf_generator import build_pdf_filename, generate_tailored_pdf
from .services.tailoring import tailor_cv_for_job_with_ats

logger = logging.getLogger(__name__)


def _ats_check(cv_text, job):
    """Full ATS report for the candidate's CV against this job.

    Offline and deterministic, so it is cheap enough to run on every job. Returns
    None if the check itself blows up — a broken checker must never reject a job.
    """
    try:
        requirements = extract_job_requirements(job.description, job.title, job.location)
        checker = ATSChecker(cv_text, job.description, requirements)
        return checker.get_detailed_report()
    except Exception:
        logger.exception('ATS check failed for job %s', job.pk)
        return None


def _knocked_out(report):
    """Would this CV be auto-rejected *for this job*?

    Only the phase 2 knock-outs count here. Phase 1 (parsing/formatting) is a
    property of the CV itself, identical for every job in the run — it is
    reported once at upload time, and rejecting all 200 jobs over it would tell
    the user nothing per-job. It still costs the CV 10% of its score.
    """
    return not report['phases']['phase2_knockout']['pass']


def _check_duplicate_application(job, report):
    """Phase 7: has this candidate already applied to this company with another CV?

    An ATS compares the parsed text of incoming applications and merges duplicates
    into one profile. We do the same by comparing the CV's content hash against
    the CVs already submitted to the same company by the same user.
    """
    text_hash = report.get('text_hash')
    if not text_hash or not job.company:
        return

    previous = (
        ATSReport.objects
        .filter(
            job__search_run__user_id=job.search_run.user_id,
            job__company__iexact=job.company,
        )
        .exclude(job_id=job.pk)
        .order_by('created_at')
    )
    for other in previous:
        other_hash = (other.report_data or {}).get('text_hash')
        if not other_hash:
            continue
        report['duplicate_application'] = True
        # Same company, *different* CV text is the case a recruiter actually sees
        # flagged; an identical re-send is just the same application again.
        report['duplicate_is_identical'] = other_hash == text_hash
        report['duplicate_of_job_id'] = other.job_id
        return


def _save_ats_report(job, report):
    """Persist the full report and mirror its headline figures onto the job."""
    _check_duplicate_application(job, report)
    job.ats_score = report['overall_score']
    strict = getattr(settings, 'ATS_STRICT_MODE', False)
    if _knocked_out(report) or (strict and not report['pass']):
        job.ats_status = Job.ATS_REJECTED
    elif report['pass']:
        job.ats_status = Job.ATS_PASSED
    else:
        job.ats_status = Job.ATS_BELOW_THRESHOLD

    ATSReport.objects.update_or_create(
        job=job,
        defaults={'overall_score': report['overall_score'], 'report_data': report},
    )


def _generate_tailored_pdf_for_job(job, cv_text, candidate_name, contract=None):
    """Tailor the CV for a high-scoring job, ATS-score it, and attach a PDF.

    The keyword contract is built once for the job and shared by tailoring and
    scoring, so the CV is written towards exactly the terms it is then measured
    on. Tailoring loops against that score, aiming for ATS_TARGET_SCORE.

    Best-effort: on failure the job is still marked processed and a note is added
    to match_reason; the search itself is never aborted.
    """
    try:
        if contract is None:
            contract = extract_job_keywords(job.description, job.title)
        tailored, report, attempts = tailor_cv_for_job_with_ats(
            cv_text, job.description, job.title, job.company, job.location,
            contract=contract,
        )
        job.tailored_text = tailored
        _save_ats_report(job, report)
        logger.info(
            'Job %s tailored in %s attempt(s); ATS %s/100 (%s) against %s',
            job.pk, attempts, report['overall_score'], job.ats_status,
            contract_summary(contract),
        )

        filename = build_pdf_filename(candidate_name, job.title, job.company)
        tmp_path = os.path.join(tempfile.gettempdir(), filename)
        generate_tailored_pdf(tailored, candidate_name, job.title, job.company, tmp_path)
        with open(tmp_path, 'rb') as fh:
            job.tailored_pdf.save(filename, File(fh), save=False)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    except Exception:
        logger.exception('Tailoring/PDF generation failed for job %s', job.pk)
        note = ' (tailored CV generation failed)'
        if note not in (job.match_reason or ''):
            job.match_reason = (job.match_reason or '') + note
    finally:
        job.processed = True


def _notify_user(search_run, job_count, tailored_count):
    """Best-effort completion email; never raises."""
    if not getattr(settings, 'SEND_COMPLETION_EMAIL', False):
        return
    user = search_run.user
    if not user.email:
        return
    try:
        send_mail(
            subject=f'Your job search #{search_run.pk} is complete',
            message=(
                f'Your job search finished with {job_count} job(s) found and '
                f'{tailored_count} tailored CV(s) generated. Log in to view the '
                f'results.'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        logger.exception('Failed to send completion email for run %s', search_run.pk)


def _set_progress(search_run, value):
    search_run.progress = max(0, min(100, int(value)))
    search_run.save(update_fields=['progress'])


def run_job_search(search_run_id):
    """Execute the full search workflow for a SearchRun. Returns a summary dict.

    Handles its own status transitions (RUNNING -> COMPLETED/FAILED) and never
    raises for expected failures; unexpected errors are recorded on the run.
    """
    search_run = SearchRun.objects.get(pk=search_run_id)
    search_run.status = SearchRun.STATUS_RUNNING
    search_run.progress = 0
    search_run.error_message = ''
    search_run.save(update_fields=['status', 'progress', 'error_message'])

    # Prefer the CV the search was started for; fall back to the newest one.
    cv = search_run.cv or CV.objects.filter(user=search_run.user).order_by('-id').first()
    if cv is None:
        return _fail(search_run, 'No CV found for user.')

    max_jobs = getattr(settings, 'MAX_JOBS_PER_SEARCH', 200)
    match_threshold = getattr(settings, 'MATCH_THRESHOLD', 75)
    max_scored = getattr(settings, 'OPENAI_MAX_SCORED_JOBS', 50)
    prescore_threshold = getattr(settings, 'KEYWORD_PRESCORE_THRESHOLD', 60)

    countries = search_run.countries or ['United Kingdom']
    min_salary = search_run.min_salary
    max_salary = search_run.max_salary

    # Search terms come from the CV itself (roles the candidate should target).
    cv_data = cv.parsed_data or {}
    cv_skills = cv_data.get('skills') or []
    keywords = extract_search_keywords(cv_data)
    logger.info('Search %s using keywords: %s', search_run.pk, keywords)

    try:
        raw_jobs = search_jobs(
            countries, min_salary=min_salary, limit=max_jobs, keywords=keywords,
        )
    except ApifyConfigError as exc:
        return _fail(search_run, f'Apify not configured: {exc}')
    except ApifySearchError as exc:
        return _fail(search_run, f'Job search failed: {exc}')

    cv_text = cv.parsed_text or ''
    # Prefer the profile name for PDF naming; fall back to the account's name.
    candidate_name = (
        cv.name
        or search_run.user.profile.candidate_name
        or search_run.user.username
    )

    # Stage 1: pre-rank every fetched job by cheap keyword overlap, so the OpenAI
    # scoring budget is spent on the most promising jobs. Previously the cap was
    # consumed in fetch order, which could burn all 50 calls on poor matches and
    # leave a perfect one unscored.
    prescores = {}
    for index, raw in enumerate(raw_jobs):
        job_skills = extract_skills_from_text(
            f"{raw.get('description', '')} {raw.get('title', '')}"
        )
        prescores[index] = {
            'job_skills': job_skills,
            'score': keyword_match_score(cv_skills, job_skills),
            'can_prescore': bool(cv_skills) and bool(job_skills),
        }

    eligible = [
        index for index, data in prescores.items()
        if salary_within_range(
            raw_jobs[index].get('salary', ''), min_salary, max_salary
        )[0]
        and not (data['can_prescore'] and data['score'] < prescore_threshold)
    ]
    eligible.sort(key=lambda i: prescores[i]['score'], reverse=True)
    # Only these get an OpenAI match call; the rest are stored unscored.
    to_score = set(eligible[:max_scored])
    logger.info(
        'Search %s: %d jobs fetched, %d passed stage 1, %d will be OpenAI-scored',
        search_run.pk, len(raw_jobs), len(eligible), len(to_score),
    )

    total = len(raw_jobs) or 1
    # Record the fetched total so the UI can show "processing X of Y".
    search_run.total_jobs = len(raw_jobs)
    search_run.save(update_fields=['total_jobs'])

    # One Sheets client per run (authenticating per job would be very slow).
    sheets = GoogleSheetsLogger()

    created = 0
    tailored = 0
    scored = 0
    rejected = 0
    try:
        for index, raw in enumerate(raw_jobs, start=1):
            position = index - 1  # index into the pre-ranking, which is 0-based
            description = raw.get('description', '')
            sponsorship = detect_sponsorship(f"{description} {raw.get('title', '')}")
            within, _parsed, range_reason = salary_within_range(
                raw.get('salary', ''), min_salary, max_salary,
            )

            stage1 = prescores[position]
            job_skills = stage1['job_skills']
            gaps = missing_skills(cv_skills, job_skills)
            prescore = stage1['score']

            if not within:
                score, reason = 0, range_reason  # below min or above max
            elif position in to_score:
                # Stage 2: precise OpenAI scoring, spent on the top-ranked jobs.
                result = compute_match_score(cv_text, description)
                score, reason = result['score'], result['reason']
                scored += 1
            elif stage1['can_prescore'] and prescore < prescore_threshold:
                # Too little skill overlap to be worth an OpenAI call.
                score = prescore
                reason = f'Pre-screened: {prescore}% keyword overlap (below threshold)'
            else:
                # Qualified, but outranked by better matches within the cost cap.
                score, reason = 0, 'Not scored (scoring limit reached)'

            job = Job.objects.create(
                job_skills=job_skills,
                missing_skills=gaps,
                search_run=search_run,
                title=raw.get('title', '')[:255] or 'Untitled',
                company=raw.get('company', '')[:255],
                location=raw.get('location', '')[:255],
                description=description,
                date_posted=raw.get('datePosted', '')[:100],
                employment_type=raw.get('employmentType', '')[:50],
                seniority_level=raw.get('seniorityLevel', '')[:50],
                salary=raw.get('salary', '')[:100],
                sponsorship_flag=sponsorship,
                match_score=score,
                match_reason=reason,
                application_link=raw.get('applyLink', '')[:500],
            )
            created += 1

            # Phase 2 knock-outs: if this CV would be auto-rejected for this job,
            # there is no point spending an OpenAI call tailoring a CV that will
            # never reach a human.
            ats = _ats_check(cv_text, job)
            if ats and _knocked_out(ats):
                _save_ats_report(job, ats)
                job.match_reason = (
                    (job.match_reason or '')
                    + ' (ATS Rejected: '
                    + '; '.join(ats['knockout_reasons'][:2]) + ')'
                ).strip()
                rejected += 1
                job.processed = True
                job.save()
            elif score >= match_threshold:
                # Build the keyword contract once, here: only jobs good enough to
                # tailor for are worth an extraction call, and the same contract
                # then drives the rewrite, the score and the skills we record.
                contract = extract_job_keywords(job.description, job.title)
                contract_terms = sorted(all_contract_terms(contract))
                if contract_terms:
                    # The contract sees the whole advert, not just our 115-word
                    # vocabulary, so it is the better record of what the job wants.
                    cv_lower = cv_text.lower()
                    job.job_skills = contract_terms
                    job.missing_skills = [
                        term for term in contract_terms
                        if not term_present(term, cv_lower)
                    ]
                _generate_tailored_pdf_for_job(
                    job, cv_text, candidate_name, contract=contract,
                )
                if job.tailored_pdf:
                    tailored += 1
                job.save()

            # Best-effort logging to the candidate's own tab in the Google Sheet.
            if sheets.enabled:
                sheets.log_job(job, candidate_name, cv_skills=cv_skills)
            _set_progress(search_run, index / total * 100)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception('Search %s failed during processing', search_run.pk)
        return _fail(search_run, f'Processing error: {exc}')

    search_run.status = SearchRun.STATUS_COMPLETED
    search_run.progress = 100
    search_run.save(update_fields=['status', 'progress'])
    logger.info(
        'Search %s completed: %d jobs, %d tailored, %d ATS-rejected',
        search_run.pk, created, tailored, rejected,
    )
    _notify_user(search_run, created, tailored)
    return {
        'status': 'COMPLETED', 'created': created, 'tailored': tailored,
        'ats_rejected': rejected,
    }


def _fail(search_run, message):
    logger.error('Search %s failed: %s', search_run.pk, message)
    search_run.status = SearchRun.STATUS_FAILED
    search_run.error_message = message
    search_run.save(update_fields=['status', 'error_message'])
    return {'status': 'FAILED', 'error': message}


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_job_search(self, search_run_id):
    """Celery entry point: run the search workflow for the given SearchRun id."""
    try:
        return run_job_search(search_run_id)
    except SearchRun.DoesNotExist:
        logger.error('process_job_search: SearchRun %s does not exist', search_run_id)
        return {'status': 'FAILED', 'error': 'SearchRun not found'}
