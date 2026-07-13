"""The keyword contract: one shared set of terms for every stage of a job.

Matching, tailoring and ATS scoring used to mine their own keywords from a
hardcoded 115-word vocabulary. An advert asking for Snowflake, dbt, Airbyte,
Fivetran, Looker and Dagster yielded exactly one recognised skill (Terraform),
so the CV was tailored against the wrong words and then scored against the same
wrong words — reporting a comfortable 90 while a real ATS saw ~50.

The contract fixes that by extracting the job's terms *from the job*, once, and
handing the same object to every stage. Whatever the contract says is what the
CV is tailored towards and what it is scored against, so the score finally means
something outside this codebase.
"""
import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

MAX_HARD_SKILLS = 25
MAX_ACRONYMS = 12
MAX_SOFT_SKILLS = 8
MAX_TITLE_VARIANTS = 4

CONTRACT_PROMPT = """You extract the screening keywords an Applicant Tracking \
System would use for a job.

Return ONLY a JSON object with these keys:
- "job_title": the role's canonical title, as written in the advert.
- "title_variants": up to 4 equivalent titles a candidate might hold for this \
role (e.g. "Software Engineer" for "Developer"). Do not include seniority \
variations the advert does not ask for.
- "hard_skills": up to 25 concrete, screenable skills, tools, technologies, \
platforms and methodologies named in the advert, ranked most important first.
- "acronyms": up to 12 [acronym, expansion] pairs for any term the advert uses \
in one form that an ATS may look for in the other (e.g. ["ci/cd", "continuous \
integration"], ["k8s", "kubernetes"], ["ml", "machine learning"]).
- "soft_skills": up to 8 interpersonal skills the advert explicitly asks for.
- "must_have": the subset of hard_skills the advert states as mandatory \
("required", "essential", "must have"). Empty if the advert states none.

RULES:
- Use the advert's EXACT surface wording, lowercased. If it says "postgres", \
write "postgres" — not "postgresql".
- Prefer specific terms over generic ones: "dbt" and "snowflake", not "data \
tooling". Never emit vague words like "experience", "skills", "team" or "systems".
- Extract ONLY what the advert actually names. Do not add skills you would \
expect for this kind of role but which the advert does not mention.
- Keep every item to a 1-4 word phrase.
- Exclude benefits, company culture and legal boilerplate."""

# Acronym pairs used by the offline fallback and merged into every contract, so
# a CV saying "k8s" still matches an advert saying "kubernetes".
COMMON_ACRONYMS = [
    ('ci/cd', 'continuous integration'),
    ('k8s', 'kubernetes'),
    ('ml', 'machine learning'),
    ('ai', 'artificial intelligence'),
    ('nlp', 'natural language processing'),
    ('aws', 'amazon web services'),
    ('gcp', 'google cloud platform'),
    ('js', 'javascript'),
    ('ts', 'typescript'),
    ('seo', 'search engine optimisation'),
    ('ppc', 'pay per click'),
    ('crm', 'customer relationship management'),
    ('api', 'application programming interface'),
    ('sql', 'structured query language'),
    ('etl', 'extract transform load'),
    ('ux', 'user experience'),
    ('ui', 'user interface'),
    ('qa', 'quality assurance'),
    ('bi', 'business intelligence'),
    ('saas', 'software as a service'),
    ('tdd', 'test driven development'),
    ('oop', 'object oriented programming'),
]

# Words that are never screening keywords, however often an advert repeats them.
_NOISE = {
    'experience', 'skills', 'skill', 'team', 'teams', 'systems', 'system', 'work',
    'working', 'role', 'job', 'company', 'business', 'knowledge', 'understanding',
    'ability', 'strong', 'good', 'great', 'excellent', 'proven', 'solid', 'years',
    'year', 'environment', 'stack', 'tools', 'technologies', 'platform', 'people',
    'candidate', 'opportunity', 'salary', 'benefits', 'holiday', 'pension',
}


def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def _clean_terms(items, limit):
    """Lowercase, strip, drop noise and over-long phrases, de-duplicate."""
    seen, out = set(), []
    for item in items or []:
        term = re.sub(r'\s+', ' ', str(item)).strip().strip('.,;:').lower()
        if not term or term in seen or term in _NOISE:
            continue
        # 1-4 words, and no runaway strings: a screening keyword is a phrase, and
        # anything longer is the model dumping a sentence into the list.
        if len(term.split()) > 4 or not 2 <= len(term) <= 40:
            continue
        seen.add(term)
        out.append(term)
    return out[:limit]


def _clean_acronyms(pairs, limit):
    seen, out = set(), []
    for pair in pairs or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        acronym = str(pair[0]).strip().lower()
        expansion = str(pair[1]).strip().lower()
        if not (acronym and expansion) or acronym == expansion:
            continue
        if acronym in seen:
            continue
        seen.add(acronym)
        out.append([acronym, expansion])
    return out[:limit]


def _empty_contract(job_title=''):
    return {
        'job_title': job_title or '',
        'title_variants': [],
        'hard_skills': [],
        'acronyms': [],
        'soft_skills': [],
        'must_have': [],
        'source': 'empty',
    }


def extract_job_keywords(job_description, job_title='', use_openai=True):
    """Return the keyword contract for a job. One OpenAI call. Never raises.

    ``use_openai=False`` forces the deterministic path. That is how the search
    stays affordable across a couple of hundred jobs: the best-ranked jobs get a
    model-extracted contract, the long tail gets the vocabulary one. Both are
    real, per-job contracts — the difference is precision, not the presence of a
    score.

    Falls back to vocabulary matching when OpenAI is unavailable, so the pipeline
    degrades rather than breaking.
    """
    if not (job_description or '').strip():
        return _empty_contract(job_title)

    if use_openai and _openai_configured():
        try:
            return _extract_via_openai(job_description, job_title)
        except Exception:  # pragma: no cover - network dependent
            logger.exception('Keyword contract extraction failed; using fallback.')

    return _fallback_contract(job_description, job_title)


def _extract_via_openai(job_description, job_title):
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=getattr(settings, 'OPENAI_MATCH_MODEL', None) or 'gpt-4o-mini',
        messages=[
            {'role': 'system', 'content': CONTRACT_PROMPT},
            {
                'role': 'user',
                'content': (
                    f'JOB TITLE: {job_title}\n\n'
                    f'JOB ADVERT:\n{job_description[:6000]}'
                ),
            },
        ],
        response_format={'type': 'json_object'},
        temperature=0,
    )
    data = json.loads(response.choices[0].message.content)
    contract = _normalise(data, job_title, source='openai')

    if not contract['hard_skills']:
        # A contract with no skills screens for nothing — the fallback at least
        # recognises the vocabulary it knows.
        logger.info('Contract had no hard skills; falling back to vocabulary.')
        return _fallback_contract(job_description, job_title)
    return contract


def _normalise(data, job_title, source):
    """Validate and clean a raw contract dict from the model."""
    hard = _clean_terms(data.get('hard_skills'), MAX_HARD_SKILLS)
    # must_have is only meaningful as a subset of hard_skills.
    must = [t for t in _clean_terms(data.get('must_have'), MAX_HARD_SKILLS) if t in hard]

    acronyms = _clean_acronyms(data.get('acronyms'), MAX_ACRONYMS)
    known = {a for a, _ in acronyms}
    # Merge in any common pair whose either side the job actually asks for, so
    # "kubernetes" in the advert still credits a CV that says "k8s".
    hard_set = set(hard)
    for acronym, expansion in COMMON_ACRONYMS:
        if len(acronyms) >= MAX_ACRONYMS or acronym in known:
            continue
        if acronym in hard_set or expansion in hard_set:
            acronyms.append([acronym, expansion])
            known.add(acronym)

    return {
        'job_title': (str(data.get('job_title') or job_title or '')).strip(),
        'title_variants': _clean_terms(data.get('title_variants'), MAX_TITLE_VARIANTS),
        'hard_skills': hard,
        'acronyms': acronyms,
        'soft_skills': _clean_terms(data.get('soft_skills'), MAX_SOFT_SKILLS),
        'must_have': must,
        'source': source,
    }


def _fallback_contract(job_description, job_title):
    """Deterministic contract: vocabulary + noun-ish phrases. Used without OpenAI.

    Weaker than the model — it can only recognise skills it already knows about —
    but it never raises and never returns nothing.
    """
    from .keyword_extractor import SKILL_VOCAB

    lowered = (job_description or '').lower()
    hard = [s for s in SKILL_VOCAB if term_present(s, lowered)]

    # Anything the advert emphasises that the vocabulary missed. Crude, but it is
    # how a Snowflake or a dbt gets seen at all without the model.
    from .ats_checker import SOFT_SKILLS, _noun_phrases, requirement_text

    soft_set = {s.lower() for s in SOFT_SKILLS}
    phrases = _noun_phrases(requirement_text(job_description or ''))
    extra = [
        term for term, count in sorted(phrases.items(), key=lambda kv: -kv[1])
        if count >= 2 and term not in _NOISE and term not in soft_set
        and term not in hard and len(term.split()) <= 3
    ]

    hard = _clean_terms(hard + extra, MAX_HARD_SKILLS)
    soft = [s for s in SOFT_SKILLS if term_present(s, lowered)][:MAX_SOFT_SKILLS]
    acronyms = [
        [a, e] for a, e in COMMON_ACRONYMS
        if term_present(a, lowered) or term_present(e, lowered)
    ][:MAX_ACRONYMS]

    # Without the model we cannot tell mandatory from desirable, so we claim
    # nothing rather than guessing and knocking candidates out on a guess.
    return {
        'job_title': job_title or '',
        'title_variants': [],
        'hard_skills': hard,
        'acronyms': acronyms,
        'soft_skills': [s.lower() for s in soft],
        'must_have': [],
        'source': 'fallback',
    }


# ---------------------------------------------------------------------------
# Helpers shared by every stage
# ---------------------------------------------------------------------------

def term_present(term, text_lower):
    """Word-boundary presence test that tolerates c++, c#, ci/cd, node.js."""
    if not (term and text_lower):
        return False
    pattern = r'(?<!\w)' + re.escape(str(term).lower()) + r'(?!\w)'
    return re.search(pattern, text_lower) is not None


def all_contract_terms(contract):
    """Every surface form the contract screens for, as one set.

    Both sides of each acronym are included: a CV may satisfy an advert's "ci/cd"
    by writing "continuous integration", and either should count.
    """
    if not contract:
        return set()
    terms = set(contract.get('hard_skills') or [])
    for pair in contract.get('acronyms') or []:
        terms.update(t.lower() for t in pair if t)
    return {t for t in terms if t}


def contract_summary(contract):
    """One-line description, for logs."""
    if not contract:
        return 'no contract'
    return (
        f'{len(contract.get("hard_skills") or [])} hard skills, '
        f'{len(contract.get("must_have") or [])} must-have, '
        f'{len(contract.get("acronyms") or [])} acronyms '
        f'({contract.get("source", "?")})'
    )
