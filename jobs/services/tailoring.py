"""CV tailoring via OpenAI.

Rewrites an existing CV to align with a specific job description, without
inventing any new experience. Falls back to the original CV text on any failure.
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
