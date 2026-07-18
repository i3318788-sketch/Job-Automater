"""CV tailoring via OpenAI.

Rewrites an existing CV to align with a specific job description, without
inventing any new experience. Falls back to the original CV text on any failure.

``tailor_cv_for_job_with_ats`` additionally scores each draft with the offline
ATS checker and, when it falls short of the target, re-runs the rewrite with the
checker's specific findings fed back in.
"""
import json
import logging
import re

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
For EVERY role in the original CV — never drop one, however old or however \
irrelevant it looks to this job — most recent first:
Job Title | Company Name | Location
Month Year - Month Year
- 3-5 achievement-focused bullet points, each starting with "- "
- Give the most space to the roles most relevant to this job; an older or less \
relevant role may be a single line, but it must still be there with its real title, \
employer and dates.
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

ATS RULES — these decide whether a human ever sees this CV:

1. KEYWORDS (the largest single part of the score)
- You are given the exact list of keywords the ATS screens for, with how many \
times each should appear. Use every keyword the candidate genuinely has, at \
roughly that frequency.
- Use the advert's EXACT wording. If it says "project management", write "project \
management" — not "managed projects". An ATS matches the phrase, not the meaning.
- Put the most important keywords in the PROFESSIONAL PROFILE and in KEY SKILLS. \
The same word carries far more weight there than buried in a role from 2015.
- Also work each key skill into the PROFESSIONAL EXPERIENCE bullet that evidences \
it. A skill listed but never evidenced scores poorly. The words of a multi-word \
requirement ("financial forecasting") must appear TOGETHER in a single bullet.
- Never repeat a keyword more than 3 times — stuffing is detected and penalised.

2. EXPERIENCE & TITLES
- Start every bullet with an action verb (Led, Managed, Developed, Increased, \
Delivered, Optimised, Implemented).
- Keep quantified results (numbers, %, £) prominent; they carry extra weight. Use \
ONLY figures already present in the original CV.
- Most recent role first, always.

3. STRUCTURE
- Single column, plain text. No tables, columns, graphics or symbols.
- Contact details on line 2 — never in a header or footer.
- Use the exact section headings given above, and no others.

IMPORTANT RULES — THESE OVERRIDE THE ATS RULES ABOVE:

THE FACTUAL RECORD IS FIXED. These are matters of fact, not presentation, and you \
must copy them across EXACTLY as they appear in the original CV:
- Education: every degree, qualification, subject, grade/classification, \
university/institution name and year. Do not upgrade a 2:2 to a 2:1, do not change \
the subject to match the job, do not swap the institution, do not add a degree the \
candidate does not hold, and do not remove one.
- Employment: every employer name, job title, and start/end date. Do not invent a \
role, do not move a date to close a gap, do not silently drop a real role, and do \
not re-title a role into something the candidate never held.
- If the job asks for a qualification or experience the candidate does not have, \
LEAVE THE GAP. A CV that scores 70 honestly is worth more than one that scores 95 \
by lying — the second one fails at interview, and the candidate pays for it.

- Do NOT invent any new roles, employers, dates, skills, achievements, metrics or \
certifications. Do NOT fabricate numbers or percentages.
- Only use a keyword if the ORIGINAL CV shows the candidate genuinely has that \
skill or experience. If a requested keyword has no support in the original CV, \
LEAVE IT OUT and accept the lower score. A CV that passes the ATS on skills the \
candidate does not have just fails at interview instead — and it is the candidate \
who pays for that.
- Job titles are FACTS, not wording. Copy every job title across exactly as the \
original CV states it. Do not re-title a role to match the advert, not even \
slightly — "Developer" does not become "Senior Software Engineer". If you want the \
target title's wording on the CV, put it in the PROFESSIONAL PROFILE as what the \
candidate is looking for, never in the role line.
- If the original CV has no contact details, omit the contact line rather than \
inventing one.
- You may rephrase, reorder and reword existing content to align with the job.
- Keep the CV to 1-2 pages.
- Use UK spelling (e.g. "organise" not "organize", "analysed" not "analyzed").
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


# The model returns STRUCTURED JSON, not free-form text. Rendering from structure
# removes the fragile text-reparsing step (parse_cv_sections) that mis-detected
# headers when a source CV extracted messily (dates hoisted above the name, a date
# fused onto the name line). A fixed schema also guarantees one identical UK format
# for every CV, whatever layout the original was in.
JSON_SYSTEM_PROMPT = """You are an expert UK CV writer. You are given a candidate's \
ORIGINAL CV text — which was extracted from a PDF/DOCX and MAY BE OUT OF ORDER \
(dates hoisted above the name, a date fused onto the name line, sidebar content \
interleaved) — and a target JOB DESCRIPTION. Reconstruct the CV as STRUCTURED DATA \
and tailor it to the job.

Return ONLY a JSON object with EXACTLY these keys:
{
  "name": "the candidate's real full name (NEVER a date, a job title or an address)",
  "contact": "one line: phone | email | location, using ONLY details in the CV",
  "profile": "a 3-5 sentence professional summary, tailored to the job description",
  "skills": ["8-12 skills, standard capitalisation, e.g. JavaScript, SEO, Excel"],
  "experience": [
    {"title": "role title", "company": "employer", "location": "city",
     "dates": "e.g. Mar 2024 - Present", "bullets": ["3-5 achievement bullets"]}
  ],
  "education": [
    {"title": "qualification", "institution": "school/university",
     "dates": "e.g. 2017 - 2020", "detail": "grade / note, or empty string"}
  ],
  "certifications": ["one per entry, or empty list"],
  "additional": ["languages / interests, or empty list"]
}

SEMANTIC RECONSTRUCTION (critical — the source text may be scrambled):
- Put the person's REAL full name in "name". A date range, a job title, an email or \
an address is NEVER the name.
- Attach every date range to the CORRECT role in experience[].dates. NEVER let a \
date float to the top, into "name", or into "contact".
- Group each role's title, company, location, dates and bullets together, most \
recent first.

TAILORING RULES (do not break):
- DO NOT change job titles, companies/employers, or dates — copy them EXACTLY as in \
the original CV.
- DO NOT invent roles, employers, dates or experience; keep the same real work. \
Never drop a real role.
- You MAY rephrase the profile and the existing bullets to use the job \
description's wording/terminology, while still describing the SAME real work the \
person already did.
- Add the skills the job requires (from the keyword list and the JD) into "skills", \
and weave them into the relevant existing bullet where they genuinely fit. If the \
candidate has NEVER done a required skill, add it to "skills" ONLY — do not \
fabricate an experience bullet claiming they did it.
- Keep quantified results (numbers, %, £) only where they already exist. Use UK \
spelling. No photo, DOB, nationality or marital status.

Return ONLY the JSON object — no commentary, no markdown fences."""


# The fixed field order used both for the plain-text rendering (ATS/tailored_text)
# and, in pdf_generator, for the PDF layout.
_EMPTY_DATA = {
    'name': '', 'contact': '', 'profile': '', 'skills': [],
    'experience': [], 'education': [], 'certifications': [], 'additional': [],
}

_DATE_ONLY_RE = re.compile(
    r'^[\s\-–—]*(?:'
    r'(?:[A-Za-z]{3,9}\.?\s+)?\d{4}'          # 2020, March 2024
    r'|\d{1,2}[/-]\d{2,4}'                     # 03/2024
    r'|present|current|now'
    r')(?:[\s\-–—to]+.*)?$',
    re.IGNORECASE,
)


def _s(value):
    return str(value).strip() if value is not None else ''


def _empty_data():
    return {k: ([] if isinstance(v, list) else '') for k, v in _EMPTY_DATA.items()}


def _normalise_data(data, cv_text=''):
    """Coerce a model/JSON dict into the exact schema, with safe types and a guard
    that a date never ends up as the candidate's name."""
    data = data if isinstance(data, dict) else {}

    experience = []
    for entry in (data.get('experience') or []):
        if not isinstance(entry, dict):
            continue
        experience.append({
            'title': _s(entry.get('title')),
            'company': _s(entry.get('company')),
            'location': _s(entry.get('location')),
            'dates': _s(entry.get('dates')),
            'bullets': [_s(b) for b in (entry.get('bullets') or []) if _s(b)],
        })

    education = []
    for entry in (data.get('education') or []):
        if not isinstance(entry, dict):
            continue
        education.append({
            'title': _s(entry.get('title')),
            'institution': _s(entry.get('institution')),
            'location': _s(entry.get('location')),
            'dates': _s(entry.get('dates')),
            'detail': _s(entry.get('detail')),
        })

    result = {
        'name': _s(data.get('name')),
        'contact': _s(data.get('contact')),
        'profile': _s(data.get('profile')),
        'skills': [_s(x) for x in (data.get('skills') or []) if _s(x)],
        'experience': experience,
        'education': education,
        'certifications': [_s(x) for x in (data.get('certifications') or []) if _s(x)],
        'additional': [_s(x) for x in (data.get('additional') or []) if _s(x)],
    }
    # Never let a bare date sit in the name slot.
    if not result['name'] or _DATE_ONLY_RE.match(result['name']):
        result['name'] = _guess_name(cv_text) or result['name']
    return result


def _guess_name(cv_text):
    """Best-effort real name: the first line that is not a date/contact/heading."""
    for raw in (cv_text or '').splitlines():
        line = raw.strip()
        if not line or _DATE_ONLY_RE.match(line):
            continue
        if '@' in line or 'http' in line.lower() or re.search(r'\d{5,}', line):
            continue
        if len(line) > 60:
            continue
        return line
    return ''


def cv_data_to_text(data):
    """Flatten the structured CV dict into clean plain text.

    Used for ATS scoring and for ``Job.tailored_text``. Emits the same section
    headings the ATS checker/text parser understand, in the canonical order.
    """
    data = _normalise_data(data)
    lines = []
    if data['name']:
        lines.append(data['name'])
    if data['contact']:
        lines.append(data['contact'])
    lines.append('')

    if data['profile']:
        lines += ['PROFESSIONAL PROFILE', data['profile'], '']
    if data['skills']:
        lines.append('KEY SKILLS')
        lines += [f'- {s}' for s in data['skills']]
        lines.append('')
    if data['experience']:
        lines.append('PROFESSIONAL EXPERIENCE')
        for e in data['experience']:
            head = ' | '.join(p for p in (e['title'], e['company'], e['location']) if p)
            if head:
                lines.append(head)
            if e['dates']:
                lines.append(e['dates'])
            lines += [f'- {b}' for b in e['bullets']]
            lines.append('')
    if data['education']:
        lines.append('EDUCATION')
        for ed in data['education']:
            head = ' | '.join(p for p in (ed['title'], ed['institution'], ed['location']) if p)
            if head:
                lines.append(head)
            if ed['dates']:
                lines.append(ed['dates'])
            if ed['detail']:
                lines.append(f'- {ed["detail"]}')
            lines.append('')
    if data['certifications']:
        lines.append('CERTIFICATIONS')
        lines += [f'- {c}' for c in data['certifications']]
        lines.append('')
    if data['additional']:
        lines.append('ADDITIONAL INFORMATION')
        lines += [f'- {a}' for a in data['additional']]
    return '\n'.join(lines).strip()


def _strip_lead_bullet(line):
    return line.strip().lstrip('-•*–—· ').strip()


def _parse_entries(lines, education=False):
    """Best-effort grouping of section lines into structured entries.

    Only used as a FALLBACK (when OpenAI is unavailable or errors); the normal path
    reconstructs structure semantically via the model.
    """
    date_re = re.compile(r'\b\d{4}\b|present|current', re.IGNORECASE)
    entries, cur = [], None
    for raw in lines or []:
        line = raw.strip()
        if not line:
            continue
        bulleted = line[:1] in '-•*–—·'
        if '|' in line and not bulleted:
            if cur:
                entries.append(cur)
            parts = [p.strip() for p in line.split('|')]
            if education:
                cur = {'title': parts[0],
                       'institution': parts[1] if len(parts) > 1 else '',
                       'location': parts[2] if len(parts) > 2 else '',
                       'dates': '', 'detail': ''}
            else:
                cur = {'title': parts[0],
                       'company': parts[1] if len(parts) > 1 else '',
                       'location': parts[2] if len(parts) > 2 else '',
                       'dates': '', 'bullets': []}
        elif cur and not bulleted and not cur['dates'] and date_re.search(line) and len(line) < 40:
            cur['dates'] = line
        elif cur:
            content = _strip_lead_bullet(line)
            if education:
                cur['detail'] = (cur['detail'] + ' ' + content).strip() if cur['detail'] else content
            else:
                cur['bullets'].append(content)
    if cur:
        entries.append(cur)
    return entries


def _text_to_data(cv_text):
    """Reconstruct the structured dict from raw CV text (OpenAI-free fallback)."""
    from .pdf_generator import parse_cv_sections

    sections = parse_cv_sections(cv_text or '')
    return _normalise_data({
        'name': sections.get('name', ''),
        'contact': sections.get('contact', ''),
        'profile': ' '.join(sections.get('profile') or []),
        'skills': [_strip_lead_bullet(l) for l in (sections.get('skills') or [])],
        'experience': _parse_entries(sections.get('experience') or []),
        'education': _parse_entries(sections.get('education') or [], education=True),
        'certifications': [_strip_lead_bullet(l) for l in (sections.get('certifications') or [])],
        'additional': [_strip_lead_bullet(l) for l in (sections.get('additional') or [])],
    }, cv_text)


def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def _keyword_brief(job_description, contract=None):
    """The exact terms the CV will be scored against, rendered for the prompt.

    Handing the model the same contract the scorer uses is the highest-leverage
    part of this prompt: tailoring and scoring optimise for the same words, so a
    high score means the CV genuinely covers what the job asked for — rather than
    what our vocabulary happened to recognise.
    """
    from .ats_checker import _expected_frequency, extract_jd_keywords

    if contract and (contract.get('hard_skills') or contract.get('must_have')):
        lines = []
        if contract.get('must_have'):
            lines.append(
                'MANDATORY (the advert states these are required): '
                + ', '.join(f'"{t}"' for t in contract['must_have'])
            )
        if contract.get('hard_skills'):
            lines.append(
                'SKILLS THE ADVERT NAMES (most important first): '
                + ', '.join(f'"{t}"' for t in contract['hard_skills'])
            )
        if contract.get('acronyms'):
            lines.append(
                'WRITE BOTH FORMS where the candidate genuinely has the skill, '
                'since different ATS platforms look for different forms: '
                + ', '.join(
                    f'"{a}" / "{e}"' for a, e in contract['acronyms']
                )
            )
        if contract.get('soft_skills'):
            lines.append(
                'SOFT SKILLS: ' + ', '.join(f'"{t}"' for t in contract['soft_skills'])
            )
        titles = [contract.get('job_title')] + (contract.get('title_variants') or [])
        titles = [t for t in titles if t]
        if titles:
            lines.append('TARGET TITLE (and accepted variants): '
                         + ', '.join(f'"{t}"' for t in titles))

        return (
            'ATS KEYWORD CONTRACT — the CV is scored on how many of these terms it '
            'contains, using EXACTLY this wording. Include every term the original '
            'CV shows the candidate GENUINELY has. Omit the rest: a term the '
            'candidate cannot evidence must not appear, whatever it costs the '
            'score.\n' + '\n'.join(lines) + '\n\n'
        )

    # No contract (OpenAI unavailable): fall back to the built-in extractor.
    keywords = extract_jd_keywords(job_description)
    if not keywords:
        return ''
    lines = []
    for group, label in (('hard', 'ESSENTIAL SKILLS'),
                         ('certification', 'CERTIFICATIONS'),
                         ('general', 'ROLE TERMS'),
                         ('soft', 'SOFT SKILLS')):
        terms = [k for k in keywords if k['type'] == group]
        if terms:
            lines.append(f'{label}: ' + ', '.join(
                f'"{k["term"]}" (x{_expected_frequency(k["jd_count"])})' for k in terms
            ))
    return (
        'ATS KEYWORDS TO INCLUDE — use the exact wording, but ONLY where the '
        'original CV shows the candidate genuinely has that skill:\n'
        + '\n'.join(lines) + '\n\n'
    )


def _recovery_brief(missing_terms):
    """Second-pass instruction: recover genuine coverage the first draft dropped."""
    if not missing_terms:
        return ''
    return (
        'TERMS THE PREVIOUS DRAFT DID NOT COVER: '
        + ', '.join(f'"{t}"' for t in missing_terms)
        + '\nFor EACH one, re-read the original CV. If the candidate genuinely did '
        'this — even if the original CV words it differently ("helped move apps to '
        'the cloud" is genuine evidence for "cloud migration") — restate that real '
        'experience using the advert\'s wording above.\n'
        'If the original CV contains NO evidence for a term, LEAVE IT OUT. Do not '
        'add it to the skills list, do not imply it, do not invent a project for '
        'it. Missing terms are expected and acceptable; invented ones are not.\n\n'
    )


def _build_user_prompt(cv_text, job_description, job_title, company,
                       contract=None, missing_terms=None):
    # Truncate inputs to bound token usage/cost.
    return (
        f'TARGET JOB TITLE: {job_title}\n'
        f'TARGET COMPANY: {company}\n\n'
        f'{_keyword_brief(job_description, contract)}'
        f'{_recovery_brief(missing_terms)}'
        f'JOB DESCRIPTION:\n{(job_description or "")[:4000]}\n\n'
        f'ORIGINAL CV:\n{cv_text[:4000]}\n\n'
        'Rewrite the CV to best match this job, following the rules strictly.'
    )


def tailor_cv_for_job(cv_text, job_description, job_title, company,
                      contract=None, missing_terms=None):
    """Return a STRUCTURED CV dict (see ``JSON_SYSTEM_PROMPT``) tailored to the job.

    The model runs in JSON mode and reconstructs the CV's fields SEMANTICALLY from
    the (possibly out-of-order) ``cv_text``, so a messy PDF extraction can no longer
    scramble the rendered layout. ``contract`` targets the exact scored terms;
    ``missing_terms`` drives a recovery pass.

    On any failure (missing key, API error, empty input, unparsable JSON) a dict is
    reconstructed from the original ``cv_text`` so downstream rendering still works.
    """
    if not cv_text:
        return _empty_data()
    if not _openai_configured():
        logger.info('OpenAI not configured; building CV data from the original text.')
        return _text_to_data(cv_text)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=DEFAULT_TAILOR_MODEL,
            messages=[
                {'role': 'system', 'content': JSON_SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': _build_user_prompt(
                        cv_text, job_description, job_title, company,
                        contract, missing_terms,
                    ),
                },
            ],
            response_format={'type': 'json_object'},
            temperature=0.3,
        )
        raw = response.choices[0].message.content or ''
        return _normalise_data(json.loads(raw), cv_text)
    except Exception:  # pragma: no cover - network/dep dependent
        logger.exception('CV tailoring failed; building CV data from original text.')
        return _text_to_data(cv_text)


def tailor_cv_for_job_with_ats(cv_text, job_description, job_title, company,
                               job_location='', target_score=None, max_attempts=None,
                               contract=None):
    """Tailor the CV, then keep improving it until it clears the ATS target.

    Each draft is scored by the offline ATS checker (free and deterministic), and
    a draft that falls short is re-generated with that checker's specific findings
    fed back to the model. The best-scoring draft wins.

    Returns ``(cv_data, ats_report, attempts)`` where ``cv_data`` is the STRUCTURED
    CV dict (rendered to the PDF from structure). The report is the full dict from
    ``ATSChecker.get_detailed_report``. When the target is never reached the best
    attempt is returned anyway — with its true (lower) score, not a flattering one.
    """
    from .ats_checker import (
        altered_facts,
        check_cv_against_job,
        claim_evidence,
        claims_needing_review,
        fabricated_metrics,
        genuine_missing_terms,
        score_cv_against_contract,
        unsupported_claims,
    )

    if target_score is None:
        target_score = getattr(settings, 'ATS_TARGET_SCORE', 90)
    if max_attempts is None:
        max_attempts = getattr(settings, 'ATS_MAX_TAILOR_ATTEMPTS', 2)
    max_attempts = max(1, int(max_attempts))

    def assess(text):
        """Score a draft against the contract, and check it invented nothing.

        The headline score comes from the contract when there is one: that is the
        set of terms the job actually asked for, so it is the number that agrees
        with an external ATS. The seven-phase report is kept alongside it as the
        diagnostic detail.
        """
        report = check_cv_against_job(text, job_description, job_title, job_location)
        if contract and contract.get('hard_skills'):
            coverage = score_cv_against_contract(text, contract)
            report['contract_coverage'] = coverage
            report['phase_score'] = report['overall_score']
            report['overall_score'] = coverage['score']
            report['ats_score'] = coverage['score']
        # Two different things, treated differently: an outright invention blocks
        # the draft; a reworded-but-grounded claim is surfaced for the candidate
        # to sanity-check, without holding the CV back.
        report['unsupported_claims'] = unsupported_claims(
            cv_text, text, job_description, contract
        )
        review = claims_needing_review(cv_text, text, job_description, contract)
        report['claims_needing_review'] = review
        # Each amber claim ships with the CV line that grounds it. A warning with
        # no receipt is one the user learns to click past; with the source line
        # attached, checking "is this fair?" takes seconds.
        # A list, not a dict: Django templates cannot index a dict by a variable
        # key, and the template should iterate this directly.
        report['claim_evidence'] = [
            {'term': term, 'lines': claim_evidence(cv_text, term)}
            for term in review
        ]
        report['fabricated_metrics'] = fabricated_metrics(cv_text, text)
        # Degrees, grades, institutions and dates are facts, not presentation.
        # Any change to them — in either direction — invalidates the draft.
        report['altered_facts'] = altered_facts(cv_text, text)
        report['honest'] = not (
            report['unsupported_claims']
            or report['fabricated_metrics']
            or report['altered_facts']
        )
        return report

    def better(candidate, incumbent):
        """A draft that invented anything never wins, however well it scores.

        Score is only the tiebreak once both drafts are honest — otherwise the
        loop would happily converge on the best-scoring fabrication, which is the
        exact failure the loop exists to prevent.
        """
        if candidate[1]['honest'] != incumbent[1]['honest']:
            return candidate[1]['honest']
        return candidate[1]['overall_score'] > incumbent[1]['overall_score']

    data = tailor_cv_for_job(
        cv_text, job_description, job_title, company, contract=contract,
    )
    best = (data, assess(cv_data_to_text(data)))
    attempts = 1

    # Without OpenAI there is nothing to iterate on — the "tailored" CV is the
    # original reconstructed into structure, and re-running would just repeat it.
    if not _openai_configured():
        return best[0], best[1], attempts

    def done():
        return best[1]['overall_score'] >= target_score and best[1]['honest']

    while attempts < max_attempts and not done():
        attempts += 1
        try:
            if best[1]['honest'] and best[1].get('contract_coverage'):
                # Honest but short of target: the gap is coverage, so name the
                # exact terms to recover from the candidate's real experience.
                missing = genuine_missing_terms(best[1]['contract_coverage'])
                data = tailor_cv_for_job(
                    cv_text, job_description, job_title, company,
                    contract=contract, missing_terms=missing,
                )
            else:
                # Dishonest, or no contract: feed back the checker's findings.
                data = _retry_with_feedback(
                    cv_text, job_description, job_title, company, best[0], best[1],
                    contract=contract,
                )
        except Exception:  # pragma: no cover - network dependent
            logger.exception('ATS-guided retailoring failed; keeping best draft.')
            break

        report = assess(cv_data_to_text(data))
        logger.info(
            'ATS retailor attempt %s for "%s": %s -> %s (invented skills: %s; '
            'invented metrics: %s)',
            attempts, job_title, best[1]['overall_score'], report['overall_score'],
            report['unsupported_claims'] or 'none',
            report['fabricated_metrics'] or 'none',
        )
        if better((data, report), best):
            best = (data, report)

    if not best[1]['honest']:
        # Every draft made something up, so there is no honest draft to ship. Fall
        # back to the original CV, which is true, and report ITS real score.
        #
        # This used to keep the best-scoring dishonest draft and merely log a
        # warning. That is the worst possible outcome: asked to tailor a backend
        # engineer's CV for a Salesforce architect role, the model invented six
        # Salesforce skills, scored a perfect 100, and the CV shipped — a document
        # that wins the ATS screen and then collapses at interview, with the
        # candidate paying for it. A warning in a log file nobody reads is not a
        # safeguard. The score a fabrication earns is not a score.
        reasons = (
            [f'altered fact: {f}' for f in best[1].get('altered_facts') or []]
            + [f'invented skill: {s}' for s in best[1].get('unsupported_claims') or []]
            + [f'invented figure: {m}' for m in best[1].get('fabricated_metrics') or []]
        )
        logger.error(
            'Every tailored draft for "%s" contained content the CV cannot support '
            '(%s). Falling back to the untailored CV and keeping its true score.',
            job_title, '; '.join(reasons),
        )
        original_report = assess(cv_text)
        original_report['fell_back_to_original'] = True
        original_report['fabrication_rejected'] = reasons
        if best[1].get('altered_facts'):
            original_report['altered_facts_rejected'] = best[1]['altered_facts']
        # The original's honest score IS the ceiling for this job: the candidate
        # does not have what this advert asks for, and no rewrite can change that.
        original_report['below_target_honestly'] = True
        original_report['honest_ceiling'] = original_report['overall_score']
        original_report['ceiling_reason'] = (
            'Your experience does not cover what this job asks for. Every rewrite '
            'that reached a higher score did so by claiming skills your CV does not '
            'evidence, so we kept your real CV and its true score instead.'
        )
        # Ship the ORIGINAL content, but still as structured data so it renders in
        # the same clean UK format as everything else.
        return _text_to_data(cv_text), original_report, attempts

    if best[1]['overall_score'] < target_score:
        # The honest ceiling for this job. Recorded on the report rather than only
        # logged, so the UI can say "this is as high as your real experience goes
        # for this advert" instead of silently showing a number below target and
        # leaving the candidate to wonder whether the tailoring simply failed.
        best[1]['below_target_honestly'] = True
        best[1]['honest_ceiling'] = best[1]['overall_score']
        missing = genuine_missing_terms(best[1].get('contract_coverage') or {}, limit=8)
        best[1]['ceiling_reason'] = (
            'This is the highest honest score for this job: the CV covers every '
            'keyword your experience genuinely evidences'
            + ('. Still missing, and not claimed: ' + ', '.join(missing)
               if missing else '')
            + '.'
        )
        logger.info(
            'Tailored CV for "%s" reached %s/100 after %s attempt(s), short of the '
            '%s target — the candidate does not evidence the remaining keywords '
            '(%s). Keeping the true score.',
            job_title, best[1]['overall_score'], attempts, target_score,
            ', '.join(missing) or 'none identified',
        )
    return best[0], best[1], attempts


def _retry_with_feedback(cv_text, job_description, job_title, company,
                         previous_data, report, contract=None):
    """One more tailoring pass (JSON mode), with the ATS checker's findings in the
    prompt. ``previous_data`` is the last structured dict; returns a new dict."""
    from openai import OpenAI

    coverage = report.get('contract_coverage') or {}
    phase3 = report['phases'].get('phase3_keyword', {})
    missing = (
        coverage.get('missing_must')
        or coverage.get('missing_hard')
        or phase3.get('hard_skills_missing')
        or phase3.get('missing_keywords')
        or []
    )
    findings = report.get('recommendations') or ['No specific findings.']

    feedback = ATS_FEEDBACK_PROMPT.format(
        score=report['overall_score'],
        findings='\n'.join(f'- {f}' for f in findings[:8]),
        missing=', '.join(missing[:12]) or '(none)',
    )

    invented_skills = report.get('unsupported_claims') or []
    invented_metrics = report.get('fabricated_metrics') or []
    changed_facts = report.get('altered_facts') or []
    if invented_skills or invented_metrics or changed_facts:
        # Non-negotiable, and stated first: the previous draft made things up.
        # Fixing that outranks the score, and the model is told so explicitly.
        problems = []
        if changed_facts:
            problems.append(
                'The FACTUAL RECORD was changed. Restore the education and '
                'employment history to exactly what the original CV says — '
                + '; '.join(changed_facts)
            )
        if invented_skills:
            problems.append(
                'SKILLS the original CV contains no evidence for: '
                + ', '.join(invented_skills)
            )
        if invented_metrics:
            problems.append(
                'NUMBERS that appear nowhere in the original CV: '
                + ', '.join(invented_metrics)
            )
        feedback = (
            'STOP. Your previous draft invented content the candidate cannot back '
            'up:\n- ' + '\n- '.join(problems)
            + '\n\nRemove every one of them — from the profile, the skills list and '
            'the experience bullets. Do not substitute a synonym, and do not replace '
            'an invented number with a different invented number: state the '
            'achievement without a figure instead ("Optimised query performance", '
            'not "Optimised query performance by 30%").\n\nIt is correct and expected '
            'for the ATS score to fall as a result. A lower score is far better than '
            'a CV that lies about what the candidate has done — the candidate is the '
            'one who has to sit the interview.\n\n'
            + feedback
        )

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=DEFAULT_TAILOR_MODEL,
        messages=[
            {'role': 'system', 'content': JSON_SYSTEM_PROMPT},
            {
                'role': 'user',
                'content': _build_user_prompt(
                    cv_text, job_description, job_title, company, contract,
                ),
            },
            {'role': 'assistant', 'content': cv_data_to_text(previous_data)},
            {'role': 'user',
             'content': feedback + '\n\nReturn the corrected CV as the same JSON object.'},
        ],
        response_format={'type': 'json_object'},
        temperature=0.3,
    )
    try:
        return _normalise_data(json.loads(response.choices[0].message.content or ''), cv_text)
    except Exception:  # pragma: no cover - defensive
        return previous_data
