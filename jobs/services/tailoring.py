"""CV tailoring via OpenAI.

Rewrites an existing CV to align with a specific job description, without
inventing any new experience. Falls back to the original CV text on any failure.

``tailor_cv_for_job_with_ats`` additionally scores each draft with the offline
ATS checker and, when it falls short of the target, re-runs the rewrite with the
checker's specific findings fed back in.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TAILOR_MODEL = getattr(settings, 'OPENAI_TAILOR_MODEL', None) or 'gpt-4o-mini'

SYSTEM_PROMPT = """You are an expert UK CV writer. Rewrite the candidate's CV to \
match the given job description, following UK CV standards.

Structure the CV EXACTLY as follows, using these exact section headings on their \
own line, in CAPITALS:

<Full Name on the very first line>
<Contact line: phone | email | location — use ONLY details found in the original CV>

PROFESSIONAL PROFILE
A concise 3-5 sentence summary of the candidate's experience, key skills and career \
goals, tailored to the job description.

KEY SKILLS
8-12 skills, one per line, each starting with "- ". Short phrases only (2-4 words).

PROFESSIONAL EXPERIENCE
For each role (maximum 3-4 most recent roles):
Job Title | Company Name | Location
Month Year - Month Year
- 3-5 achievement-focused bullet points, each starting with "- "
- Start each bullet with a strong action verb (Led, Managed, Developed, Increased, \
Optimised)
- Include quantifiable results ONLY where they already exist in the original CV

EDUCATION
Qualification | Institution | Location
Year - Year
- Grade/classification if present in the original CV

CERTIFICATIONS
- One certification per line, starting with "- " (omit this section entirely if the \
original CV lists none)

IMPORTANT RULES:
- Do NOT invent any new roles, employers, dates, skills, achievements, metrics or \
certifications. Do NOT fabricate numbers or percentages.
- If the original CV has no contact details, omit the contact line rather than \
inventing one.
- You may rephrase, reorder and reword existing content to align with the job.
- Keep the CV to 1-2 pages.
- Use UK spelling (e.g. "organise" not "organize", "analysed" not "analyzed").
- Use professional, confident language.
- Tailor the profile and bullet points to the job description's keywords.
- Do NOT include a photo, date of birth, nationality or marital status.

Return only the CV text, with no commentary, preamble or markdown fences."""


# Appended to the prompt on a retry. The constraint against inventing content is
# repeated here deliberately: the model is being told to raise a keyword score,
# which is exactly the situation where it is most tempted to fabricate skills the
# candidate does not have.
ATS_FEEDBACK_PROMPT = """Your previous draft scored {score}/100 in an ATS \
screening for this job. Revise it to score higher.

ATS findings to address:
{findings}

Keywords the job asks for that your draft is missing:
{missing}

HOW TO FIX THIS — read carefully:
- Only add a keyword if the ORIGINAL CV shows the candidate genuinely has that \
skill or experience. If the original CV gives no evidence for a keyword, LEAVE IT \
OUT. A CV that wins an ATS screen on skills the candidate does not have will fail \
at interview, and that is a worse outcome than a lower score.
- Prefer re-surfacing what is already there: use the job's exact wording for a \
skill the candidate already demonstrates ("project management" rather than \
"managed projects"), move genuinely relevant skills into the profile and the most \
recent role, and make sure skills named in KEY SKILLS also appear in the \
experience bullets that evidence them.
- Do not repeat a keyword more than 2-3 times; stuffing is penalised.
- Do NOT invent roles, employers, dates, metrics, certifications or qualifications.

Return only the revised CV text."""


def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def _build_user_prompt(cv_text, job_description, job_title, company):
    # Truncate inputs to bound token usage/cost.
    return (
        f'TARGET JOB TITLE: {job_title}\n'
        f'TARGET COMPANY: {company}\n\n'
        f'JOB DESCRIPTION:\n{(job_description or "")[:4000]}\n\n'
        f'ORIGINAL CV:\n{cv_text[:4000]}\n\n'
        'Rewrite the CV to best match this job, following the rules strictly.'
    )


def tailor_cv_for_job(cv_text, job_description, job_title, company):
    """Return a tailored version of ``cv_text`` aligned to the given job.

    On any failure (missing key, API error, empty input) the original CV text is
    returned so downstream PDF generation still has content to work with.
    """
    if not cv_text:
        return cv_text
    if not _openai_configured():
        logger.info('OpenAI not configured; returning original CV for tailoring.')
        return cv_text

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=DEFAULT_TAILOR_MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': _build_user_prompt(
                        cv_text, job_description, job_title, company
                    ),
                },
            ],
            temperature=0.3,
        )
        tailored = (response.choices[0].message.content or '').strip()
        return tailored or cv_text
    except Exception:  # pragma: no cover - network/dep dependent
        logger.exception('CV tailoring failed; returning original CV text.')
        return cv_text


def tailor_cv_for_job_with_ats(cv_text, job_description, job_title, company,
                               job_location='', target_score=None, max_attempts=None):
    """Tailor the CV, then keep improving it until it clears the ATS target.

    Each draft is scored by the offline ATS checker (free and deterministic), and
    a draft that falls short is re-generated with that checker's specific findings
    fed back to the model. The best-scoring draft wins.

    Returns ``(tailored_text, ats_report, attempts)``. The report is the full dict
    from ``ATSChecker.get_detailed_report``. When the target is never reached the
    best attempt is returned anyway — with its true (lower) score, not a
    flattering one.
    """
    from .ats_checker import check_cv_against_job

    if target_score is None:
        target_score = getattr(settings, 'ATS_TARGET_SCORE', 90)
    if max_attempts is None:
        max_attempts = getattr(settings, 'ATS_MAX_TAILOR_ATTEMPTS', 2)
    max_attempts = max(1, int(max_attempts))

    def score(text):
        return check_cv_against_job(text, job_description, job_title, job_location)

    tailored = tailor_cv_for_job(cv_text, job_description, job_title, company)
    report = score(tailored)
    best = (tailored, report)
    attempts = 1

    # Without OpenAI there is nothing to iterate on — the "tailored" CV is the
    # original, and re-running would just burn cycles producing the same text.
    if not _openai_configured():
        return best[0], best[1], attempts

    while attempts < max_attempts and best[1]['overall_score'] < target_score:
        attempts += 1
        try:
            tailored = _retry_with_feedback(
                cv_text, job_description, job_title, company, best[0], best[1]
            )
        except Exception:  # pragma: no cover - network dependent
            logger.exception('ATS-guided retailoring failed; keeping best draft.')
            break

        report = score(tailored)
        logger.info(
            'ATS retailor attempt %s for "%s": %s -> %s',
            attempts, job_title, best[1]['overall_score'], report['overall_score'],
        )
        if report['overall_score'] > best[1]['overall_score']:
            best = (tailored, report)

    if best[1]['overall_score'] < target_score:
        logger.info(
            'Tailored CV for "%s" reached %s/100 after %s attempt(s), short of the '
            '%s target.', job_title, best[1]['overall_score'], attempts, target_score,
        )
    return best[0], best[1], attempts


def _retry_with_feedback(cv_text, job_description, job_title, company,
                         previous_draft, report):
    """One more tailoring pass, with the ATS checker's findings in the prompt."""
    from openai import OpenAI

    phase3 = report['phases'].get('phase3_keyword', {})
    missing = phase3.get('hard_skills_missing') or phase3.get('missing_keywords') or []
    findings = report.get('recommendations') or ['No specific findings.']

    feedback = ATS_FEEDBACK_PROMPT.format(
        score=report['overall_score'],
        findings='\n'.join(f'- {f}' for f in findings[:8]),
        missing=', '.join(missing[:12]) or '(none)',
    )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=DEFAULT_TAILOR_MODEL,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {
                'role': 'user',
                'content': _build_user_prompt(
                    cv_text, job_description, job_title, company
                ),
            },
            {'role': 'assistant', 'content': previous_draft},
            {'role': 'user', 'content': feedback},
        ],
        temperature=0.3,
    )
    return (response.choices[0].message.content or '').strip() or previous_draft
