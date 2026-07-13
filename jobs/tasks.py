"""Celery tasks and the core asynchronous job-search workflow.

``run_job_search`` holds the plain (non-Celery) logic so it can be unit-tested
directly. ``process_job_search`` is the Celery task wrapper invoked via
``.delay(search_run_id)`` from the view.
"""
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.core.mail import send_mail
from django.utils import timezone

from .models import ATSReport, CV, Job, SearchRun
from .services.apify_service import ApifyConfigError, ApifySearchError, search_jobs
from .services.ats_checker import (
    ATSChecker,
    extract_job_requirements,
    score_cv_against_contract,
)
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
    missing_skills,
    prescore_job,
)
from .services.matching import detect_sponsorship, salary_within_range
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


def _build_tailored_cv(job_data, cv_text, candidate_name):
    """Do the network-bound tailoring work for one job. Runs on a worker thread.

    Touches no database and no model instance — it takes a plain dict and returns
    a plain dict — so it is safe to run concurrently. Every DB write for these
    results happens back on the main thread in ``_apply_tailored_cv``.

    Returns a payload dict, including an ``error`` key when the work failed; a
    single bad job must not abort the whole search.
    """
    payload = {'job_id': job_data['id'], 'error': None}
    try:
        contract = job_data.get('contract') or extract_job_keywords(
            job_data['description'], job_data['title'],
        )
        tailored, report, attempts = tailor_cv_for_job_with_ats(
            cv_text, job_data['description'], job_data['title'], job_data['company'],
            job_data['location'], contract=contract,
        )
        filename = build_pdf_filename(
            candidate_name, job_data['title'], job_data['company'],
        )
        # Unique temp path per job: workers run concurrently and two jobs at the
        # same company would otherwise race on the same filename.
        tmp_path = os.path.join(
            tempfile.gettempdir(), f'{job_data["id"]}_{filename}',
        )
        generate_tailored_pdf(
            tailored, candidate_name, job_data['title'], job_data['company'], tmp_path,
        )
        payload.update({
            'contract': contract,
            'tailored_text': tailored,
            'report': report,
            'attempts': attempts,
            'filename': filename,
            'tmp_path': tmp_path,
        })
    except Exception as exc:
        logger.exception('Tailoring failed for job %s', job_data['id'])
        payload['error'] = str(exc)
    return payload


def _apply_tailored_cv(job, payload, cv_text):
    """Persist a worker's tailoring payload. Main thread only (it writes to the DB)."""
    if payload.get('error'):
        note = ' (tailored CV generation failed)'
        if note not in (job.match_reason or ''):
            job.match_reason = (job.match_reason or '') + note
        job.processed = True
        return

    try:
        contract = payload.get('contract')
        contract_terms = sorted(all_contract_terms(contract)) if contract else []
        if contract_terms:
            # The contract sees the whole advert, not just our 115-word vocabulary,
            # so it is the better record of what the job actually wants.
            cv_lower = (cv_text or '').lower()
            job.job_skills = contract_terms
            job.missing_skills = [
                term for term in contract_terms if not term_present(term, cv_lower)
            ]

        job.tailored_text = payload['tailored_text']
        _save_ats_report(job, payload['report'])
        logger.info(
            'Job %s tailored in %s attempt(s); ATS %s/100 (%s) against %s',
            job.pk, payload['attempts'], payload['report']['overall_score'],
            job.ats_status, contract_summary(contract),
        )

        tmp_path = payload['tmp_path']
        with open(tmp_path, 'rb') as fh:
            job.tailored_pdf.save(payload['filename'], File(fh), save=False)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    except Exception:
        logger.exception('Saving tailored CV failed for job %s', job.pk)
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


# Progress is reported in phases so the bar moves steadily instead of sitting at
# 0 and then snapping to 100. Each phase owns a band of the bar.
PHASE_FETCH = (0, 15)       # Apify fetch
PHASE_SCORING = (15, 75)    # pre-rank, OpenAI match scoring, Job row creation
PHASE_TAILORING = (75, 95)  # tailoring + PDF generation for the matched jobs
PHASE_FINALISE = (95, 100)  # Sheets logging, final save


def _set_progress(search_run, value):
    search_run.progress = max(0, min(100, int(value)))
    search_run.save(update_fields=['progress'])


def _phase_progress(search_run, phase, done, total):
    """Advance the bar within a phase's band, in proportion to work completed.

    Always called from the main thread — never from inside a worker — so the
    counter can't be updated concurrently.
    """
    low, high = phase
    fraction = (done / total) if total else 1.0
    _set_progress(search_run, low + (high - low) * min(1.0, max(0.0, fraction)))


def _score_workers():
    """Concurrency for match scoring. Bounded: these are OpenAI calls, not CPU."""
    return max(1, min(8, getattr(settings, 'SEARCH_SCORING_WORKERS', 6)))


def _tailor_workers():
    """Concurrency for tailoring. Lower than scoring: each job is several calls."""
    return max(1, min(8, getattr(settings, 'SEARCH_TAILORING_WORKERS', 4)))


def _match_reason(coverage, contract):
    """A one-line explanation of a match score, from the coverage that produced it."""
    hard = contract.get('hard_skills') or []
    if not hard:
        return 'No screenable skills could be mined from this job description.'

    found = len(coverage.get('found_hard') or [])
    missing = coverage.get('missing_hard') or []
    must_missing = coverage.get('missing_must') or []

    parts = [f'Covers {found}/{len(hard)} of the skills this job asks for']
    if must_missing:
        parts.append('missing must-haves: ' + ', '.join(must_missing[:4]))
    elif missing:
        parts.append('missing: ' + ', '.join(missing[:4]))
    if coverage.get('title_ok'):
        parts.append('job title matches')
    return '. '.join(parts) + '.'


def _score_job(job_data, cv_text, use_openai):
    """Score ONE job against the candidate's real CV. Runs on a worker thread.

    The score is the CV's coverage of this job's own keyword contract, so it is
    genuinely per-job and varies with the advert. It is measured against the
    ORIGINAL CV, never the tailored one: the question this answers is "how well
    does this candidate already fit this job", which is what decides whether the
    job is worth pursuing at all. (The tailored CV's coverage is a different
    number, stored separately as ats_score.)

    Touches no DB and no model instance, so it is safe to run concurrently.
    """
    result = {'position': job_data['position'], 'contract': None,
              'score': 0, 'reason': 'Unable to compute'}
    try:
        contract = extract_job_keywords(
            job_data['description'], job_data['title'], use_openai=use_openai,
        )
        coverage = score_cv_against_contract(cv_text, contract)
        result.update({
            'contract': contract,
            'coverage': coverage,
            'score': coverage['score'],
            'reason': _match_reason(coverage, contract),
        })
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception('Match scoring failed for job at %s', job_data['position'])
        result['reason'] = f'Unable to compute: {exc}'
    return result


def run_job_search(search_run_id):
    """Execute the full search workflow for a SearchRun. Returns a summary dict.

    Handles its own status transitions (RUNNING -> COMPLETED/FAILED) and never
    raises for expected failures; unexpected errors are recorded on the run.
    """
    search_run = SearchRun.objects.get(pk=search_run_id)
    search_run.status = SearchRun.STATUS_RUNNING
    search_run.progress = 0
    search_run.error_message = ''
    # Stamped when the worker picks the run up, not when it was queued — the ETA
    # is extrapolated from this, and queue time is not work time.
    search_run.started_at = timezone.now()
    search_run.save(
        update_fields=['status', 'progress', 'error_message', 'started_at'],
    )

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

    _set_progress(search_run, PHASE_FETCH[0])
    try:
        raw_jobs = search_jobs(
            countries, min_salary=min_salary, limit=max_jobs, keywords=keywords,
            city=search_run.city,
        )
    except ApifyConfigError as exc:
        return _fail(search_run, f'Apify not configured: {exc}')
    except ApifySearchError as exc:
        return _fail(search_run, f'Job search failed: {exc}')
    _set_progress(search_run, PHASE_FETCH[1])  # fetch done: 15%

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
        job_text = f"{raw.get('description', '')} {raw.get('title', '')}"
        job_skills = extract_skills_from_text(job_text)
        prescores[index] = {
            'job_skills': job_skills,
            # Ranked on the best of two signals, one of which does not depend on
            # the hardcoded vocabulary — otherwise a Snowflake/dbt advert looks
            # skill-less, scores a neutral 50, and gets cut before the contract
            # that would have loved it is ever built.
            'score': prescore_job(cv_skills, job_skills, job_text),
            'can_prescore': bool(cv_skills) and bool(job_skills),
        }

    # Every job gets a real, per-job score. The pre-rank no longer decides WHETHER
    # a job is scored — only how precisely: the best-ranked jobs get a
    # model-extracted keyword contract, the long tail gets the deterministic one.
    # Previously everything past the cap was stored with a hardcoded 0, which made
    # the scores meaningless and hid good jobs at the bottom of the list.
    eligible = [
        index for index, data in prescores.items()
        if salary_within_range(
            raw_jobs[index].get('salary', ''), min_salary, max_salary
        )[0]
        and not (data['can_prescore'] and data['score'] < prescore_threshold)
    ]
    eligible.sort(key=lambda i: prescores[i]['score'], reverse=True)
    precise = set(eligible[:max_scored])
    logger.info(
        'Search %s: %d jobs fetched, %d passed stage 1, %d get a model-extracted '
        'contract (the rest are scored from the deterministic one)',
        search_run.pk, len(raw_jobs), len(eligible), len(precise),
    )

    total = len(raw_jobs) or 1
    # Record the fetched total so the UI can show "processing X of Y".
    search_run.total_jobs = len(raw_jobs)
    search_run.save(update_fields=['total_jobs'])

    # One Sheets client per run (authenticating per job would be very slow).
    sheets = GoogleSheetsLogger()

    created = 0
    tailored = 0
    rejected = 0
    try:
        # ------------------------------------------------------------------
        # Phase: scoring (15 -> 75%)
        # ------------------------------------------------------------------
        # Every job is scored against its OWN keyword contract, so the score
        # genuinely varies with the advert instead of being a constant. The work
        # is network-bound and independent, so it runs concurrently: the workers
        # touch no database and no model instances — plain dicts in, plain dicts
        # out — so there is no shared connection or cursor. Every Job row is
        # created below, on the main thread.
        scores = {}
        done = 0
        with ThreadPoolExecutor(max_workers=_score_workers()) as pool:
            futures = [
                pool.submit(
                    _score_job,
                    {
                        'position': i,
                        'title': raw.get('title', ''),
                        'description': raw.get('description', ''),
                    },
                    cv_text,
                    i in precise,
                )
                for i, raw in enumerate(raw_jobs)
            ]
            for future in as_completed(futures):
                result = future.result()
                scores[result['position']] = result
                done += 1
                # Progress is advanced here, on the main thread, as each future
                # lands — never from inside a worker.
                _phase_progress(
                    search_run, (PHASE_SCORING[0], 65), done, len(raw_jobs),
                )
        scored = len(scores)

        # Create the Job rows (main thread, so DB access stays single-threaded).
        jobs_to_tailor = []
        contracts = {}
        for position, raw in enumerate(raw_jobs):
            description = raw.get('description', '')
            sponsorship = detect_sponsorship(f"{description} {raw.get('title', '')}")
            within, _parsed, range_reason = salary_within_range(
                raw.get('salary', ''), min_salary, max_salary,
            )

            result = scores.get(position) or {}
            contract = result.get('contract')
            coverage = result.get('coverage') or {}

            # The score is the real one either way. A job outside the salary range
            # keeps its true skills match — it just isn't pursued, and the reason
            # says so. Reporting it as 0 would be a lie about the fit.
            score = result.get('score', 0)
            reason = result.get('reason', 'Unable to compute')
            if not within:
                reason = f'{range_reason}. {reason}'

            # The contract sees the whole advert, not just the 115-word vocabulary,
            # so it is the better record of what the job wants and what the CV
            # lacks. Fall back to the vocabulary view only if there is no contract.
            if contract and (contract.get('hard_skills') or contract.get('acronyms')):
                contracts[position] = contract
                job_skills = sorted(all_contract_terms(contract))
                gaps = list(coverage.get('missing_hard') or [])
            else:
                job_skills = prescores[position]['job_skills']
                gaps = missing_skills(cv_skills, job_skills)

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
            elif within and score >= match_threshold:
                # `within` matters: a job can now score well on skills and still be
                # outside the salary range, and there is no point tailoring a CV
                # for a job the candidate has ruled out on pay.
                jobs_to_tailor.append((job, contracts.get(position)))

            _phase_progress(
                search_run, (65, PHASE_SCORING[1]), position + 1, len(raw_jobs),
            )

        # ------------------------------------------------------------------
        # Phase: tailoring (75 -> 95%)
        # ------------------------------------------------------------------
        # Same shape as the scoring phase: the slow, network-bound work (contract
        # extraction, the two-pass tailoring loop, PDF rendering to a temp file)
        # runs on workers against plain dicts; the resulting payloads are written
        # to the DB here, on the main thread. Each job keeps its full two-pass
        # tailoring — the concurrency is across jobs, not within one.
        _set_progress(search_run, PHASE_TAILORING[0])
        if jobs_to_tailor:
            by_id = {job.pk: job for job, _c in jobs_to_tailor}
            job_data = [
                {
                    'id': job.pk, 'title': job.title, 'company': job.company,
                    'location': job.location, 'description': job.description,
                    # Reuse the contract already built during scoring rather than
                    # paying for a second extraction of the same advert.
                    'contract': contract,
                }
                for job, contract in jobs_to_tailor
            ]
            done = 0
            with ThreadPoolExecutor(max_workers=_tailor_workers()) as pool:
                futures = [
                    pool.submit(_build_tailored_cv, data, cv_text, candidate_name)
                    for data in job_data
                ]
                for future in as_completed(futures):
                    payload = future.result()
                    job = by_id[payload['job_id']]
                    _apply_tailored_cv(job, payload, cv_text)
                    job.save()
                    if job.tailored_pdf:
                        tailored += 1
                    done += 1
                    _phase_progress(
                        search_run, PHASE_TAILORING, done, len(job_data),
                    )
        _set_progress(search_run, PHASE_TAILORING[1])

        # ------------------------------------------------------------------
        # Phase: finalise (95 -> 100%)
        # ------------------------------------------------------------------
        # Sheets logging happens here rather than mid-loop, so every row is
        # written with its final match and ATS scores already in place.
        all_jobs = list(search_run.jobs.all())
        for i, job in enumerate(all_jobs, start=1):
            if sheets.enabled:
                sheets.log_job(job, candidate_name, cv_skills=cv_skills)
            _phase_progress(search_run, PHASE_FINALISE, i, len(all_jobs))
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
