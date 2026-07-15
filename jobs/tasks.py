"""Celery tasks and the core asynchronous job-search workflow.

``run_job_search`` holds the plain (non-Celery) logic so it can be unit-tested
directly. ``process_job_search`` is the Celery task wrapper invoked via
``.delay(search_run_id)`` from the view.
"""
import logging
import os
import random
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from celery import shared_task
from django.conf import settings
from django.core.files import File
from django.core.mail import send_mail
from django.utils import timezone

from .models import ATSReport, CV, Job, SearchRun
from .services.apify_service import ApifyConfigError, ApifySearchError, search_jobs
from .services.ats_checker import score_cv_against_contract
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
from .services.matching import detect_sponsorship, parse_salary
from .services.pdf_generator import build_pdf_filename, generate_tailored_pdf
from .services.tailoring import tailor_cv_for_job_with_ats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Demo scores
# ---------------------------------------------------------------------------
# Set DEMO_SCORES=1 to run a search that *presents* scores in a healthy band
# instead of measuring them, so a client can see what a good result looks like
# on the dashboard without waiting for a real search to turn one up.
#
# It is off unless the environment variable is set, and it can never engage by
# accident. What it produces is illustrative, not an assessment: a demo run says
# nothing about whether a real CV matches a real advert, and its numbers must not
# be presented as if it did. Every demo score is flagged in the stored report
# (``demo_scores``) and in the match reason, so a demo row stays recognisable
# afterwards rather than quietly becoming part of the record.
DEMO_ATS_RANGE = (81, 97)
DEMO_MATCH_RANGE = (75, 90)
DEMO_MATCH_REASON = 'Illustrative demo score — DEMO_SCORES is on, not a real assessment.'


def _demo_scores_enabled():
    return os.getenv('DEMO_SCORES', '').strip().lower() in ('1', 'true', 'yes', 'on')


def _demo_score(seed, low, high):
    """A stable score in [low, high] for ``seed``.

    Seeded on the job itself, so the same job shows the same figure on every
    reload — a demo whose numbers reshuffle each refresh reads as broken.
    """
    return random.Random(str(seed)).randint(low, high)


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
    """Persist the full report and mirror its headline figures onto the job.

    The status is a description of the score and nothing more. There is no
    rejected outcome: a job the CV scores badly against is still a job the
    candidate gets to see, decide on, and apply to if they want to.
    """
    _check_duplicate_application(job, report)

    if _demo_scores_enabled():
        # Presentation value, not a measurement. Flagged in the stored report so
        # nothing downstream — and no one reading the record later — can mistake
        # it for a real one.
        report = dict(report)
        report['overall_score'] = _demo_score(f'ats:{job.pk}', *DEMO_ATS_RANGE)
        report['pass'] = True
        report['demo_scores'] = True
        logger.warning(
            'DEMO_SCORES is on: job %s shows an illustrative ATS score of %s, not '
            'a real one.', job.pk, report['overall_score'],
        )

    job.ats_score = report['overall_score']
    job.ats_status = Job.ATS_PASSED if report['pass'] else Job.ATS_BELOW_THRESHOLD

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


NOT_SCORED_REASON = (
    'Not scored: this advert carried no readable description, so there is nothing '
    'to match your CV against.'
)


def _match_reason(coverage, contract):
    """A one-line explanation of a match score, from the coverage that produced it."""
    if not coverage.get('scorable'):
        return NOT_SCORED_REASON

    hard = contract.get('hard_skills') or []
    found = len(coverage.get('found_hard') or [])
    missing = coverage.get('missing_hard') or []
    must_missing = coverage.get('missing_must') or []

    parts = []
    if hard:
        parts.append(f'Covers {found}/{len(hard)} of the skills this job asks for')
    if must_missing:
        parts.append('missing must-haves: ' + ', '.join(must_missing[:4]))
    elif missing:
        parts.append('missing: ' + ', '.join(missing[:4]))
    if coverage.get('title_ok'):
        parts.append('job title matches')
    return '. '.join(parts) + '.' if parts else 'Matched on job title only.'


def _apply_salary_preference(salary_text, min_salary, max_salary, reason,
                             title='', run_id=None):
    """Compare a job's pay against the candidate's ceiling. Returns (over_max, reason).

    The only optional filter left in the pipeline, and even it does not filter by
    default: a job above the stated maximum is *flagged*, not hidden. An advert's
    figure is a range and an opening position, and a candidate who capped their
    preference at £70k still wants to see the £80k role. Dropping it needs
    SALARY_HARD_FILTER explicitly on.

    A salary below the minimum is not acted on at all here: the minimum is already
    passed to the Apify actor, and re-applying it as a client-side filter is how
    "Salary below minimum" ended up prefixed onto jobs whose pay simply wasn't
    parseable.

    The comparison is logged either way, because a job silently flagged on an
    unparseable salary string is the bug you cannot find without it.
    """
    if max_salary is None:
        return False, reason

    parsed = parse_salary(salary_text)
    if parsed is None:
        logger.debug(
            'Search %s: salary "%s" on "%s" is not parseable — no comparison made.',
            run_id, salary_text, title,
        )
        return False, reason

    over = parsed > float(max_salary)
    logger.info(
        'Search %s: salary check on "%s" — advert "%s" parsed as %s vs ceiling %s '
        '-> %s',
        run_id, title, salary_text, parsed, max_salary,
        'above preference' if over else 'within preference',
    )
    if over:
        return True, f'Above your salary preference ({salary_text}). {reason}'
    return False, reason


def _score_job(job_data, cv_text, use_openai):
    """Score ONE job against the candidate's real CV. Runs on a worker thread.

    The score is the CV's coverage of this job's own keyword contract, so it is
    genuinely per-job and varies with the advert. It is measured against the
    ORIGINAL CV, never the tailored one: the question this answers is "how well
    does this candidate already fit this job", which is what decides whether the
    job is worth pursuing at all. (The tailored CV's coverage is a different
    number, stored separately as ats_score.)

    ``score`` is None when the advert yielded nothing to screen against — an empty
    or unreadable description. None is not zero and it is certainly not 100: it
    means "we don't know", the UI says exactly that, and the job sorts last.

    Touches no DB and no model instance, so it is safe to run concurrently.
    """
    result = {'position': job_data['position'], 'contract': None,
              'score': None, 'reason': NOT_SCORED_REASON}
    description = job_data['description'] or ''
    if not description.strip():
        logger.warning(
            'Job "%s" at position %s arrived with an empty description — not scored.',
            job_data['title'], job_data['position'],
        )
        return result

    try:
        contract = extract_job_keywords(
            description, job_data['title'], use_openai=use_openai,
        )
        coverage = score_cv_against_contract(cv_text, contract)
        result.update({
            'contract': contract,
            'coverage': coverage,
            # None when the contract had nothing job-specific in it. Passed
            # straight through: inventing a number here is what produced the
            # 100/100 "no screenable skills could be mined" jobs.
            'score': coverage['score'],
            'reason': _match_reason(coverage, contract),
        })
        if coverage['score'] is None:
            logger.warning(
                'Job "%s" (%s chars of description) yielded no screenable '
                'contract — not scored.',
                job_data['title'], len(description),
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception('Match scoring failed for job at %s', job_data['position'])
        result['reason'] = f'Not scored: {exc}'
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
    # The one optional filter left, and it is off by default: even a job over the
    # candidate's ceiling is worth seeing (adverts state ranges, and pay is
    # negotiable), so by default it is flagged rather than hidden.
    salary_hard_filter = getattr(settings, 'SALARY_HARD_FILTER', False)

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

    # The pre-rank decides nothing about WHETHER a job is scored or shown — only
    # how precisely it is scored. Every fetched job is scored and every fetched
    # job appears in the results; the best-ranked ones simply get a model-extracted
    # keyword contract, and the long tail gets the deterministic one, because the
    # OpenAI budget is finite. Nothing is filtered out here.
    ranked = sorted(prescores, key=lambda i: prescores[i]['score'], reverse=True)
    precise = set(ranked[:max_scored])
    logger.info(
        'Search %s: %d jobs fetched, all of them scored and shown; the top %d by '
        'pre-rank get a model-extracted contract, the rest the deterministic one',
        search_run.pk, len(raw_jobs), len(precise),
    )

    # Record the fetched total so the UI can show "processing X of Y".
    search_run.total_jobs = len(raw_jobs)
    search_run.save(update_fields=['total_jobs'])

    # One Sheets client per run (authenticating per job would be very slow).
    sheets = GoogleSheetsLogger()

    created = 0
    tailored = 0
    not_scored = 0
    over_budget = 0
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

        # Create the Job rows (main thread, so DB access stays single-threaded).
        # EVERY fetched job becomes a row: nothing is knocked out, nothing is
        # hidden. The score decides what the candidate is shown first, not whether
        # they are shown it at all.
        jobs_to_tailor = []
        contracts = {}
        for position, raw in enumerate(raw_jobs):
            description = raw.get('description', '')
            sponsorship = detect_sponsorship(f"{description} {raw.get('title', '')}")

            result = scores.get(position) or {}
            contract = result.get('contract')
            coverage = result.get('coverage') or {}

            # None means "not scored" — the advert gave us nothing to measure. It
            # is never silently turned into a number.
            score = result.get('score')
            reason = result.get('reason') or NOT_SCORED_REASON

            if _demo_scores_enabled():
                # Replace the measurement with a presentation figure, and say so in
                # the reason: the number is on screen next to it, so a demo row can
                # never be read as a real match.
                score = _demo_score(
                    f"match:{position}:{raw.get('title', '')}", *DEMO_MATCH_RANGE,
                )
                reason = DEMO_MATCH_REASON

            if score is None:
                not_scored += 1

            over_max, reason = _apply_salary_preference(
                raw.get('salary', ''), min_salary, max_salary, reason,
                title=raw.get('title', ''), run_id=search_run.pk,
            )
            if over_max:
                over_budget += 1
                if salary_hard_filter:
                    # Opt-in only (SALARY_HARD_FILTER). Off by default: the job is
                    # kept and flagged instead.
                    logger.info(
                        'Search %s: dropping "%s" — over the salary ceiling and '
                        'SALARY_HARD_FILTER is on.',
                        search_run.pk, raw.get('title', ''),
                    )
                    continue

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
                above_salary_preference=over_max,
                application_link=raw.get('applyLink', '')[:500],
            )
            created += 1

            # Tailoring is the one thing a score still gates, and only because a
            # tailored CV costs several OpenAI calls. A job the candidate does not
            # match is still shown — it just doesn't get a bespoke CV written for
            # it. Being over the salary ceiling does not block tailoring: the job
            # is in the results, so it must be applicable to.
            if score is not None and score >= match_threshold:
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
        'Search %s completed: %d jobs shown, %d tailored, %d not scored (empty '
        'description), %d flagged above the salary preference',
        search_run.pk, created, tailored, not_scored, over_budget,
    )
    _notify_user(search_run, created, tailored)
    return {
        'status': 'COMPLETED', 'created': created, 'tailored': tailored,
        'not_scored': not_scored, 'above_salary_preference': over_budget,
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
