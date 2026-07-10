"""CV tailoring via OpenAI.

Rewrites an existing CV to align with a specific job description, without
inventing any new experience. Falls back to the original CV text on any failure.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TAILOR_MODEL = getattr(settings, 'OPENAI_TAILOR_MODEL', None) or 'gpt-4o-mini'

SYSTEM_PROMPT = (
    'You are an expert CV writer. Rewrite the candidate\'s CV to match the given '
    'job description. Do NOT invent any new roles, skills, achievements, or '
    'metrics that are not already present in the original CV. You may rephrase, '
    'reorder, and reword existing content to better align with the job\'s '
    'language and requirements. Return the tailored CV as plain text with clear '
    'section headings (e.g., Summary, Experience, Education, Skills).'
)


def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


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
        # Truncate inputs to bound token usage/cost.
        user_prompt = (
            f'TARGET JOB TITLE: {job_title}\n'
            f'TARGET COMPANY: {company}\n\n'
            f'JOB DESCRIPTION:\n{(job_description or "")[:4000]}\n\n'
            f'ORIGINAL CV:\n{cv_text[:4000]}\n\n'
            'Rewrite the CV to best match this job, following the rules strictly.'
        )
        response = client.chat.completions.create(
            model=DEFAULT_TAILOR_MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ],
            temperature=0.3,
        )
        tailored = (response.choices[0].message.content or '').strip()
        return tailored or cv_text
    except Exception:  # pragma: no cover - network/dep dependent
        logger.exception('CV tailoring failed; returning original CV text.')
        return cv_text
