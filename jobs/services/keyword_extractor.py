"""Extract skills and search keywords from CVs and job descriptions.

Uses OpenAI when configured (better quality), with a deterministic
vocabulary-based fallback so the system still works without an API key.
"""
import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# Vocabulary used by the offline fallback and for job-description skill mining.
SKILL_VOCAB = [
    # Engineering / data
    'python', 'java', 'javascript', 'typescript', 'c#', 'c++', 'go', 'rust', 'php',
    'ruby', 'kotlin', 'swift', 'scala', 'r', 'matlab',
    'django', 'flask', 'fastapi', 'spring', 'node.js', 'react', 'angular', 'vue',
    'next.js', 'express', '.net', 'laravel', 'rails',
    'sql', 'postgresql', 'mysql', 'mongodb', 'redis', 'elasticsearch', 'oracle',
    'aws', 'azure', 'gcp', 'docker', 'kubernetes', 'terraform', 'ansible', 'jenkins',
    'ci/cd', 'git', 'linux', 'bash', 'devops', 'microservices', 'rest', 'graphql',
    'machine learning', 'deep learning', 'nlp', 'pandas', 'numpy', 'tensorflow',
    'pytorch', 'scikit-learn', 'spark', 'hadoop', 'airflow', 'etl', 'tableau',
    'power bi', 'excel', 'data analysis', 'data science', 'statistics',
    # Marketing / SEO / content
    'seo', 'sem', 'ppc', 'google ads', 'google analytics', 'content marketing',
    'copywriting', 'social media', 'email marketing', 'hubspot', 'wordpress',
    'keyword research', 'link building', 'on-page seo', 'off-page seo', 'ahrefs',
    'semrush', 'crm', 'salesforce',
    # Business / ops / finance
    'project management', 'agile', 'scrum', 'kanban', 'jira', 'stakeholder management',
    'budgeting', 'forecasting', 'accounting', 'financial analysis', 'sap',
    'business analysis', 'product management', 'risk management', 'compliance',
    # Design
    'figma', 'sketch', 'adobe xd', 'photoshop', 'illustrator', 'ui design', 'ux design',
    # Soft skills
    'communication', 'leadership', 'teamwork', 'problem solving', 'negotiation',
]

# Role words used to build search terms in the offline fallback.
ROLE_WORDS = [
    'software engineer', 'software developer', 'backend developer', 'frontend developer',
    'full stack developer', 'data scientist', 'data analyst', 'data engineer',
    'devops engineer', 'cloud engineer', 'qa engineer', 'test engineer',
    'seo executive', 'seo specialist', 'seo manager', 'digital marketing executive',
    'marketing manager', 'content writer', 'copywriter', 'social media manager',
    'project manager', 'product manager', 'business analyst', 'consultant',
    'accountant', 'financial analyst', 'sales executive', 'account manager',
    'ui designer', 'ux designer', 'graphic designer', 'hr manager', 'recruiter',
    'operations manager', 'developer', 'engineer', 'analyst', 'manager',
]

# Role -> (min, max) salary defaults used when the user sets no minimum.
ROLE_SALARY_RANGES = {
    'intern': (18000, 25000),
    'junior': (20000, 30000),
    'graduate': (20000, 30000),
    'executive': (25000, 45000),
    'assistant': (22000, 32000),
    'analyst': (28000, 50000),
    'specialist': (30000, 50000),
    'developer': (35000, 65000),
    'engineer': (35000, 70000),
    'consultant': (40000, 70000),
    'manager': (40000, 65000),
    'senior': (45000, 75000),
    'lead': (55000, 85000),
    'principal': (65000, 100000),
    'head': (60000, 95000),
    'director': (60000, 90000),
}

DEFAULT_KEYWORDS = ['software engineer', 'developer', 'IT']


def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def _normalize(items, limit=None):
    """Lowercase, strip, de-duplicate (order-preserving), optionally truncate."""
    seen, out = set(), []
    for item in items or []:
        value = str(item).strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# Vocabulary-based extraction (offline fallback / job descriptions)
# ---------------------------------------------------------------------------

def extract_skills_from_text(text, vocab=None, limit=30):
    """Return vocabulary skills that appear in ``text`` (word-boundary matched)."""
    if not text:
        return []
    lowered = text.lower()
    found = []
    for skill in (vocab or SKILL_VOCAB):
        # Escape regex metachars (c++, c#, node.js, ci/cd ...).
        pattern = r'(?<!\w)' + re.escape(skill) + r'(?!\w)'
        if re.search(pattern, lowered):
            found.append(skill)
    return found[:limit]


def _fallback_search_keywords(raw_text, limit=5):
    """Pick likely role titles from the CV text (longest/most specific first)."""
    if not raw_text:
        return list(DEFAULT_KEYWORDS)
    lowered = raw_text.lower()
    # Longer phrases first so "seo executive" wins over bare "executive".
    matches = [
        role for role in sorted(ROLE_WORDS, key=len, reverse=True)
        if role in lowered
    ]
    return _normalize(matches, limit) or list(DEFAULT_KEYWORDS)


# ---------------------------------------------------------------------------
# CV profile extraction (skills + job titles to search for)
# ---------------------------------------------------------------------------

CV_PROFILE_PROMPT = (
    'You are a recruitment assistant. From the CV text, extract:\n'
    '1. "skills": the candidate\'s concrete skills (technical + tools + domain), '
    'up to 20, lowercase.\n'
    '2. "job_titles": up to 6 job titles this candidate should search for on job '
    'boards, ordered best-first. Use realistic job-board titles (e.g. "SEO '
    'Executive", "Digital Marketing Executive"), not sentences.\n'
    'Respond ONLY with a JSON object with keys "skills" and "job_titles".'
)


def extract_cv_profile(cv_text):
    """Return {'skills': [...], 'job_titles': [...]} for a CV.

    Uses OpenAI when configured; otherwise falls back to vocabulary matching.
    Never raises.
    """
    if not cv_text:
        return {'skills': [], 'job_titles': list(DEFAULT_KEYWORDS)}

    if _openai_configured():
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            response = client.chat.completions.create(
                model=getattr(settings, 'OPENAI_MATCH_MODEL', 'gpt-4o-mini'),
                messages=[
                    {'role': 'system', 'content': CV_PROFILE_PROMPT},
                    {'role': 'user', 'content': cv_text[:6000]},
                ],
                response_format={'type': 'json_object'},
                temperature=0,
            )
            data = json.loads(response.choices[0].message.content)
            skills = _normalize(data.get('skills'), 20)
            titles = _normalize(data.get('job_titles'), 6)
            if skills or titles:
                return {
                    'skills': skills or extract_skills_from_text(cv_text),
                    'job_titles': titles or _fallback_search_keywords(cv_text),
                }
        except Exception:  # pragma: no cover - network dependent
            logger.exception('OpenAI CV profile extraction failed; using fallback.')

    return {
        'skills': extract_skills_from_text(cv_text),
        'job_titles': _fallback_search_keywords(cv_text),
    }


def extract_search_keywords(cv_parsed_data, limit=5):
    """Return the search terms to send to the jobs actor for this CV."""
    data = cv_parsed_data or {}
    titles = _normalize(data.get('job_titles'), limit)
    if titles:
        return titles
    skills = _normalize(data.get('skills'), limit)
    if skills:
        return skills
    return _fallback_search_keywords(data.get('raw_text', ''), limit)


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def keyword_match_score(cv_skills, job_skills):
    """Percentage of the job's required skills that the CV covers (0-100).

    Returns a neutral 50 when either side has no extractable skills, so jobs
    aren't unfairly penalised by a thin job description.
    """
    cv_set = {str(s).lower() for s in (cv_skills or [])}
    job_set = {str(s).lower() for s in (job_skills or [])}
    if not cv_set or not job_set:
        return 50
    overlap = len(cv_set & job_set)
    return int(round(overlap / len(job_set) * 100))


def missing_skills(cv_skills, job_skills):
    """Skills the job asks for that the CV does not mention."""
    cv_set = {str(s).lower() for s in (cv_skills or [])}
    return [s for s in (job_skills or []) if str(s).lower() not in cv_set]


def get_salary_range(keywords, default_min=25000, default_max=None):
    """Derive a (min, max) salary range from role keywords.

    Used only as the default when the user has not set their own minimum.
    """
    text = ' '.join(str(k) for k in (keywords or [])).lower()
    # Most senior match wins (iterate by descending minimum).
    best = None
    for key, (lo, hi) in ROLE_SALARY_RANGES.items():
        if key in text and (best is None or lo > best[0]):
            best = (lo, hi)
    return best if best else (default_min, default_max)
