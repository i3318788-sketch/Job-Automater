"""Matching engine: OpenAI CV/job scoring, sponsorship detection, salary parsing."""
import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# Model kept small/cheap; override with OPENAI_MATCH_MODEL in .env if desired.
DEFAULT_MODEL = getattr(settings, 'OPENAI_MATCH_MODEL', None) or 'gpt-4o-mini'

SYSTEM_PROMPT = (
    'You are an experienced HR expert and technical recruiter. Compare the '
    "candidate's CV with the job description, focusing on:\n"
    '1. Skills overlap (technical and soft skills)\n'
    '2. Experience relevance (similar roles, industry, seniority)\n'
    '3. Education match\n'
    '4. Certifications match\n\n'
    'Be strict: a score of 75+ means the candidate is highly qualified and would '
    'realistically be shortlisted. Respond ONLY with a JSON object with exactly '
    'two keys: "score" (integer 0-100) and "reason" (1-2 sentences explaining the '
    'match). Do not include any text outside the JSON object.'
)

ATS_PROMPT = (
    'You are an Applicant Tracking System (ATS) simulator. Estimate how well the '
    'given CV would score in an ATS screening for the given job description, '
    'considering keyword coverage, relevant job titles, skills, and formatting of '
    'the content. Respond ONLY with a JSON object with exactly two keys: "score" '
    '(integer 0-100) and "reason" (one short sentence).'
)

SPONSORSHIP_KEYWORDS = [
    'visa sponsorship',
    'sponsorship offered',
    'can sponsor',
    'sponsor visa',
    'work visa',
    'tier 2',
    'skilled worker',
]

# ---------------------------------------------------------------------------
# Sponsorship detection
# ---------------------------------------------------------------------------

def detect_sponsorship(text):
    """Return 'SPONSORED' if any sponsorship keyword appears, else 'NOT_MENTIONED'."""
    if not text:
        return 'NOT_MENTIONED'
    lowered = text.lower()
    for keyword in SPONSORSHIP_KEYWORDS:
        if keyword in lowered:
            return 'SPONSORED'
    return 'NOT_MENTIONED'


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------
# Matches numbers like 40,000 / 40000 / 40k / £40K, capturing the number and an
# optional 'k' multiplier.
_SALARY_RE = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*([kK])?')


def parse_salary(salary_text):
    """Extract the highest plausible annual salary figure from free text.

    Returns a float, or None if no salary figure can be parsed. Values are
    interpreted with a 'k' suffix as thousands. Very small numbers (< 1000,
    without a 'k') are ignored as they are unlikely to be annual salaries
    (e.g. hours, counts).
    """
    if not salary_text:
        return None
    values = []
    for number, k_suffix in _SALARY_RE.findall(str(salary_text)):
        try:
            value = float(number.replace(',', ''))
        except ValueError:
            continue
        if k_suffix:
            value *= 1000
        elif value < 1000:
            # Skip stray small numbers unless explicitly in thousands.
            continue
        values.append(value)
    if not values:
        return None
    # Use the highest figure (best-case / upper bound of a range).
    return max(values)


def salary_meets_threshold(salary_text, min_salary):
    """Decide whether a job's salary meets the minimum.

    Returns (meets: bool, parsed: float|None). When salary can't be parsed,
    ``meets`` is True (we don't exclude jobs with unknown salary).
    """
    within, parsed, _ = salary_within_range(salary_text, min_salary, None)
    return within, parsed


def salary_within_range(salary_text, min_salary, max_salary):
    """Decide whether a job's salary falls within [min_salary, max_salary].

    ``max_salary`` may be None (no upper limit). Returns
    ``(within: bool, parsed: float|None, reason: str)`` where ``reason`` explains
    an out-of-range result ('Salary below minimum' / 'Salary above maximum').
    Unknown salary is treated as within-range (we don't drop jobs without data).
    """
    parsed = parse_salary(salary_text)
    if parsed is None:
        return True, None, ''  # unknown salary -> include (flagged elsewhere)
    if min_salary is not None and parsed < float(min_salary):
        return False, parsed, 'Salary below minimum'
    if max_salary is not None and parsed > float(max_salary):
        return False, parsed, 'Salary above maximum'
    return True, parsed, ''


# ---------------------------------------------------------------------------
# OpenAI matching
# ---------------------------------------------------------------------------

def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def compute_match_score(cv_text, job_description):
    """Score a CV against a job description using OpenAI.

    Returns a dict: {'score': int, 'reason': str}. On any failure (missing key,
    API error, unparsable response) returns a safe default score of 0.
    """
    if not _openai_configured():
        return {'score': 0, 'reason': 'OpenAI not configured'}
    if not (cv_text and job_description):
        return {'score': 0, 'reason': 'Missing CV text or job description'}

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        # Truncate to keep token usage/cost bounded.
        user_prompt = (
            f'CANDIDATE CV:\n{cv_text[:6000]}\n\n'
            f'JOB DESCRIPTION:\n{job_description[:6000]}'
        )
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ],
            response_format={'type': 'json_object'},
            temperature=0,
        )
        content = response.choices[0].message.content
        return _parse_match_response(content)
    except Exception as exc:  # pragma: no cover - network/dep dependent
        logger.exception('OpenAI match scoring failed')
        return {'score': 0, 'reason': f'Unable to compute: {exc}'}


def compute_ats_score(tailored_cv_text, job_description):
    """Estimate the ATS score (0-100) of a tailored CV against a job description.

    Returns None when OpenAI isn't configured or the call fails, so callers can
    distinguish "not scored" from a genuine low score.
    """
    if not _openai_configured():
        return None
    if not (tailored_cv_text and job_description):
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {'role': 'system', 'content': ATS_PROMPT},
                {
                    'role': 'user',
                    'content': (
                        f'TAILORED CV:\n{tailored_cv_text[:6000]}\n\n'
                        f'JOB DESCRIPTION:\n{job_description[:4000]}'
                    ),
                },
            ],
            response_format={'type': 'json_object'},
            temperature=0,
        )
        return _parse_match_response(response.choices[0].message.content)['score']
    except Exception:  # pragma: no cover - network dependent
        logger.exception('ATS scoring failed')
        return None


def _parse_match_response(content):
    """Parse the model's JSON response into a validated {score, reason} dict."""
    try:
        data = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {'score': 0, 'reason': 'Unable to compute'}

    raw_score = data.get('score', 0)
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    reason = str(data.get('reason', '')).strip() or 'No reason provided'
    return {'score': score, 'reason': reason}
