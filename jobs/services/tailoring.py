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
- Do NOT invent any new roles, employers, dates, skills, achievements, metrics or \
certifications. Do NOT fabricate numbers or percentages.
- Only use a keyword if the ORIGINAL CV shows the candidate genuinely has that \
skill or experience. If a requested keyword has no support in the original CV, \
LEAVE IT OUT and accept the lower score. A CV that passes the ATS on skills the \
candidate does not have just fails at interview instead — and it is the candidate \
who pays for that.
- Where the candidate's real job title is close to the target title, you may adopt \
the target title's wording. If their actual title differs materially, keep the real \
one — never claim a role they did not hold.
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


def _openai_configured():
    return bool(getattr(settings, 'OPENAI_API_KEY', ''))


def _keyword_brief(job_description):
    """The exact keywords the ATS will screen for, with the density each needs.

    Handing the model the checker's own target list is the single highest-leverage
    part of this prompt: it removes the guesswork about which words matter, so the
    rewrite optimises for what is actually measured instead of what reads well.
    """
    from .ats_checker import _expected_frequency, extract_jd_keywords

    keywords = extract_jd_keywords(job_description)
    if not keywords:
        return ''

    lines = []
    for group, label in (('hard', 'ESSENTIAL SKILLS'),
                         ('certification', 'CERTIFICATIONS'),
                         ('general', 'ROLE TERMS'),
                         ('soft', 'SOFT SKILLS')):
        terms = [k for k in keywords if k['type'] == group]
        if not terms:
            continue
        rendered = ', '.join(
            f'"{k["term"]}" (x{_expected_frequency(k["jd_count"])})' for k in terms
        )
        lines.append(f'{label}: {rendered}')

    return (
        'ATS KEYWORDS TO INCLUDE — use the exact wording, at roughly the frequency '
        'shown in brackets, but ONLY where the original CV shows the candidate '
        'genuinely has that skill:\n' + '\n'.join(lines) + '\n\n'
    )


def _build_user_prompt(cv_text, job_description, job_title, company):
    # Truncate inputs to bound token usage/cost.
    return (
        f'TARGET JOB TITLE: {job_title}\n'
        f'TARGET COMPANY: {company}\n\n'
        f'{_keyword_brief(job_description)}'
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
    from .ats_checker import (
        check_cv_against_job,
        fabricated_metrics,
        unsupported_claims,
    )

    if target_score is None:
        target_score = getattr(settings, 'ATS_TARGET_SCORE', 90)
    if max_attempts is None:
        max_attempts = getattr(settings, 'ATS_MAX_TAILOR_ATTEMPTS', 2)
    max_attempts = max(1, int(max_attempts))

    def assess(text):
        """Score a draft, and check it invented neither skills nor metrics."""
        report = check_cv_against_job(text, job_description, job_title, job_location)
        report['unsupported_claims'] = unsupported_claims(
            cv_text, text, job_description
        )
        report['fabricated_metrics'] = fabricated_metrics(cv_text, text)
        report['honest'] = not (
            report['unsupported_claims'] or report['fabricated_metrics']
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

    tailored = tailor_cv_for_job(cv_text, job_description, job_title, company)
    best = (tailored, assess(tailored))
    attempts = 1

    # Without OpenAI there is nothing to iterate on — the "tailored" CV is the
    # original, and re-running would just burn cycles producing the same text.
    if not _openai_configured():
        return best[0], best[1], attempts

    def done():
        return best[1]['overall_score'] >= target_score and best[1]['honest']

    while attempts < max_attempts and not done():
        attempts += 1
        try:
            tailored = _retry_with_feedback(
                cv_text, job_description, job_title, company, best[0], best[1]
            )
        except Exception:  # pragma: no cover - network dependent
            logger.exception('ATS-guided retailoring failed; keeping best draft.')
            break

        report = assess(tailored)
        logger.info(
            'ATS retailor attempt %s for "%s": %s -> %s (invented skills: %s; '
            'invented metrics: %s)',
            attempts, job_title, best[1]['overall_score'], report['overall_score'],
            report['unsupported_claims'] or 'none',
            report['fabricated_metrics'] or 'none',
        )
        if better((tailored, report), best):
            best = (tailored, report)

    if not best[1]['honest']:
        # Every draft invented something. Keep the best one, but say so loudly:
        # this CV must not go out claiming things the candidate cannot back up.
        logger.warning(
            'Tailored CV for "%s" still contains unverifiable content — skills: %s; '
            'metrics: %s. Flagged for human review.',
            job_title,
            ', '.join(best[1]['unsupported_claims']) or 'none',
            ', '.join(best[1]['fabricated_metrics']) or 'none',
        )
    elif best[1]['overall_score'] < target_score:
        logger.info(
            'Tailored CV for "%s" reached %s/100 after %s attempt(s), short of the '
            '%s target — the candidate does not evidence the remaining keywords.',
            job_title, best[1]['overall_score'], attempts, target_score,
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

    invented_skills = report.get('unsupported_claims') or []
    invented_metrics = report.get('fabricated_metrics') or []
    if invented_skills or invented_metrics:
        # Non-negotiable, and stated first: the previous draft made things up.
        # Fixing that outranks the score, and the model is told so explicitly.
        problems = []
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
