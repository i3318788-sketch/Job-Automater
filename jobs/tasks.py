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

from .models import CV, Job, SearchRun
from .services.apify_service import ApifyConfigError, ApifySearchError, search_jobs
from .services.google_sheets import GoogleSheetsLogger
from .services.keyword_extractor import (
    extract_search_keywords,
    extract_skills_from_text,
    keyword_match_score,
    missing_skills,
)
from .services.matching import (
    compute_ats_score,
    compute_match_score,
    detect_sponsorship,
    salary_within_range,
)
from .services.pdf_generator import build_pdf_filename, generate_tailored_pdf
from .services.tailoring import tailor_cv_for_job

logger = logging.getLogger(__name__)


def _generate_tailored_pdf_for_job(job, cv_text, candidate_name):
    """Tailor the CV for a high-scoring job and attach a generated PDF.

    Best-effort: on failure the job is still marked processed and a note is added
    to match_reason; the search itself is never aborted.
    """
    try:
        tailored = tailor_cv_for_job(cv_text, job.description, job.title, job.company)
        job.tailored_text = tailored

        # Estimate how the tailored CV would fare in an ATS for this job.
        job.ats_score = compute_ats_score(tailored, job.description)

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

    total = len(raw_jobs) or 1
    # Record the fetched total so the UI can show "processing X of Y".
    search_run.total_jobs = len(raw_jobs)
    search_run.save(update_fields=['total_jobs'])

    # One Sheets client per run (authenticating per job would be very slow).
    sheets = GoogleSheetsLogger()

    created = 0
    tailored = 0
    scored = 0
    try:
        for index, raw in enumerate(raw_jobs, start=1):
            description = raw.get('description', '')
            sponsorship = detect_sponsorship(f"{description} {raw.get('title', '')}")
            within, _parsed, range_reason = salary_within_range(
                raw.get('salary', ''), min_salary, max_salary,
            )

            # Stage 1: cheap keyword pre-score against the job's required skills.
            job_skills = extract_skills_from_text(f"{description} {raw.get('title', '')}")
            gaps = missing_skills(cv_skills, job_skills)
            prescore = keyword_match_score(cv_skills, job_skills)
            # Only gate on the pre-score when both sides actually yielded skills —
            # otherwise we'd filter out every job for a CV we couldn't mine.
            can_prescore = bool(cv_skills) and bool(job_skills)

            if not within:
                score, reason = 0, range_reason  # below min or above max
            elif scored >= max_scored:
                # Cost cap reached: store the job but skip OpenAI scoring.
                score, reason = 0, 'Not scored (scoring limit reached)'
            elif can_prescore and prescore < prescore_threshold:
                # Stage 1 filter: too little skill overlap to spend an OpenAI call.
                score = prescore
                reason = f'Pre-screened: {prescore}% keyword overlap (below threshold)'
            else:
                # Stage 2: precise OpenAI scoring for the jobs that qualified.
                result = compute_match_score(cv_text, description)
                score, reason = result['score'], result['reason']
                scored += 1

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

            if score >= match_threshold:
                _generate_tailored_pdf_for_job(job, cv_text, candidate_name)
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
    logger.info('Search %s completed: %d jobs, %d tailored', search_run.pk, created, tailored)
    _notify_user(search_run, created, tailored)
    return {'status': 'COMPLETED', 'created': created, 'tailored': tailored}


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
