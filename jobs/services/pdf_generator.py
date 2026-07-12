"""Generate a professional UK-style CV PDF from tailored CV text (reportlab)."""
import logging
import re

from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# Canonical section order in the rendered CV.
SECTION_ORDER = [
    ('profile', 'PROFESSIONAL PROFILE'),
    ('skills', 'KEY SKILLS'),
    ('experience', 'PROFESSIONAL EXPERIENCE'),
    ('education', 'EDUCATION'),
    ('certifications', 'CERTIFICATIONS'),
    ('additional', 'ADDITIONAL INFORMATION'),
]

# Whole-line headings we recognise -> section key. Matching is done on the fully
# normalised line (never a substring), so body text such as "4+ years of
# experience" is not mistaken for the EXPERIENCE heading.
SECTION_ALIASES = {
    'professional profile': 'profile',
    'personal profile': 'profile',
    'personal statement': 'profile',
    'profile': 'profile',
    'professional summary': 'profile',
    'summary': 'profile',
    'objective': 'profile',
    'about me': 'profile',

    'key skills': 'skills',
    'core skills': 'skills',
    'core competencies': 'skills',
    'skills': 'skills',
    'technical skills': 'skills',
    'skills and competencies': 'skills',

    'professional experience': 'experience',
    'work experience': 'experience',
    'work history': 'experience',
    'employment history': 'experience',
    'employment': 'experience',
    'experience': 'experience',
    'career history': 'experience',

    'education': 'education',
    'education and qualifications': 'education',
    'qualifications': 'education',
    'academic background': 'education',

    'certifications': 'certifications',
    'certification': 'certifications',
    'certifications and professional development': 'certifications',
    'professional development': 'certifications',
    'courses': 'certifications',

    'additional information': 'additional',
    'additional': 'additional',
    'interests': 'additional',
    'languages': 'additional',
    'volunteer work': 'additional',
    'references': 'additional',
}

BULLET_PREFIXES = ('•', '-', '*', '–', '—', '·')

# "Jan 2022 - Present", "2017 – 2020", "01/2019 - 12/2020"
_DATE_RE = re.compile(
    r'^\s*('
    r'[A-Za-z]{3,9}\.?\s+\d{4}|\d{1,2}/\d{4}|\d{4}'
    r')\s*(?:[-–—]|to)\s*('
    r'present|current|now|[A-Za-z]{3,9}\.?\s+\d{4}|\d{1,2}/\d{4}|\d{4}'
    r')\s*$',
    re.IGNORECASE,
)


def _normalize_heading(line):
    """Strip markdown/punctuation and lowercase, for whole-line heading matching."""
    cleaned = line.strip().strip('#*_ ').rstrip(':').strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = cleaned.replace('&', 'and')
    return cleaned.lower()


def _is_bullet(line):
    return line.lstrip().startswith(BULLET_PREFIXES)


def _strip_bullet(line):
    text = line.lstrip()
    for prefix in BULLET_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def _is_date_line(line):
    return bool(_DATE_RE.match(line.strip()))


def _escape(text):
    """Escape XML-special characters for reportlab Paragraph markup."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def parse_cv_sections(cv_text):
    """Parse tailored CV text into {name, contact, profile, skills, experience, ...}.

    Everything before the first recognised heading is treated as the header: the
    first non-empty line is the name, any remaining lines are contact details.
    """
    sections = {}
    header_lines = []
    current = None

    for raw_line in (cv_text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # A line is only a heading if the WHOLE line matches a known heading.
        key = SECTION_ALIASES.get(_normalize_heading(line))
        if key:
            current = key
            sections.setdefault(current, [])
            continue

        if current is None:
            header_lines.append(line)
        else:
            sections[current].append(line)

    if header_lines:
        sections['name'] = header_lines[0]
        contact = [_strip_bullet(l) if _is_bullet(l) else l for l in header_lines[1:]]
        if contact:
            sections['contact'] = ' • '.join(contact)

    return sections


def _build_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='CVName', parent=styles['Title'], fontName='Helvetica-Bold',
        fontSize=20, leading=24, alignment=TA_CENTER, spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name='CVContact', parent=styles['Normal'], fontName='Helvetica',
        fontSize=9.5, leading=12, alignment=TA_CENTER,
        textColor=HexColor('#333333'), spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name='CVHeading', parent=styles['Heading2'], fontName='Helvetica-Bold',
        fontSize=11.5, leading=14, textColor=HexColor('#1D4771'),
        spaceBefore=10, spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name='CVRole', parent=styles['Normal'], fontName='Helvetica-Bold',
        fontSize=10, leading=13, spaceBefore=6, spaceAfter=0,
    ))
    styles.add(ParagraphStyle(
        name='CVDate', parent=styles['Normal'], fontName='Helvetica-Oblique',
        fontSize=9, leading=12, textColor=HexColor('#555555'), spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name='CVBody', parent=styles['Normal'], fontName='Helvetica',
        fontSize=9.5, leading=13, spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        name='CVBullet', parent=styles['Normal'], fontName='Helvetica',
        fontSize=9.5, leading=13, leftIndent=10, bulletIndent=0, spaceAfter=2,
    ))
    return styles


def _section_heading(title, styles):
    """Heading with a thin rule under it, kept with the following content."""
    return [
        Paragraph(_escape(title), styles['CVHeading']),
        HRFlowable(width='100%', thickness=0.6, color=HexColor('#1D4771'),
                   spaceBefore=0, spaceAfter=5),
    ]


def _skills_table(lines, styles, doc_width):
    """Render skills as a two-column bullet list."""
    skills = [_strip_bullet(l) for l in lines if _strip_bullet(l)]
    if not skills:
        return []

    # Pair skills across each row (skill 1 | skill 2, skill 3 | skill 4, ...).
    if len(skills) % 2:
        skills.append('')
    rows = [
        [
            Paragraph(f'• {_escape(left)}', styles['CVBody']) if left else '',
            Paragraph(f'• {_escape(right)}', styles['CVBody']) if right else '',
        ]
        for left, right in zip(skills[0::2], skills[1::2])
    ]
    table = Table(rows, colWidths=[doc_width / 2.0] * 2)
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    return [table]


def _render_entry_lines(lines, styles):
    """Render experience/education style content: roles, dates, bullets, prose."""
    flowables = []
    for line in lines:
        if _is_bullet(line):
            flowables.append(
                Paragraph(_escape(_strip_bullet(line)), styles['CVBullet'], bulletText='•')
            )
        elif _is_date_line(line):
            flowables.append(Paragraph(_escape(line), styles['CVDate']))
        elif '|' in line:
            # "Job Title | Company | Location" -> bold the job title.
            parts = [p.strip() for p in line.split('|')]
            head = _escape(parts[0])
            rest = ' | '.join(_escape(p) for p in parts[1:])
            text = f'<b>{head}</b>' + (f' | {rest}' if rest else '')
            flowables.append(Paragraph(text, styles['CVRole']))
        else:
            flowables.append(Paragraph(_escape(line), styles['CVBody']))
    return flowables


def generate_tailored_pdf(cv_text, candidate_name, job_title, company, output_path):
    """Write a UK-format CV PDF of ``cv_text`` to ``output_path``.

    Layout: centered bold name, contact line, rule, then PROFESSIONAL PROFILE,
    KEY SKILLS (two columns), PROFESSIONAL EXPERIENCE, EDUCATION, CERTIFICATIONS.
    Falls back to plain paragraphs when the text has no recognisable sections.
    """
    styles = _build_styles()
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f'{candidate_name} - CV',
        author=candidate_name,
    )
    doc_width = doc.width

    sections = parse_cv_sections(cv_text)

    story = [
        Paragraph(_escape(sections.get('name') or candidate_name or 'Candidate'),
                  styles['CVName']),
    ]
    if sections.get('contact'):
        story.append(Paragraph(_escape(sections['contact']), styles['CVContact']))
    story.append(HRFlowable(width='100%', thickness=1, color=colors.black,
                            spaceBefore=3, spaceAfter=2))

    rendered_any = False
    for key, title in SECTION_ORDER:
        lines = sections.get(key)
        if not lines:
            continue
        rendered_any = True
        heading = _section_heading(title, styles)
        if key == 'skills':
            body = _skills_table(lines, styles, doc_width)
        else:
            body = _render_entry_lines(lines, styles)
        # Keep the heading with at least its first line of content.
        story.append(KeepTogether(heading + body[:1]))
        story.extend(body[1:])
        story.append(Spacer(1, 2))

    if not rendered_any:
        # No recognisable sections: emit the text as plain paragraphs.
        for line in (cv_text or '').splitlines():
            if not line.strip():
                continue
            if _is_bullet(line):
                story.append(Paragraph(_escape(_strip_bullet(line)),
                                       styles['CVBullet'], bulletText='•'))
            else:
                story.append(Paragraph(_escape(line.strip()), styles['CVBody']))

    if len(story) <= 3:
        story.append(Paragraph('(No CV content available.)', styles['CVBody']))

    doc.build(story)
    logger.info('Generated UK-format tailored PDF at %s', output_path)


def build_pdf_filename(candidate_name, job_title, company):
    """Build a filesystem-safe PDF name: CandidateName_JobTitle_Company.pdf."""
    def clean(part):
        part = (part or '').strip() or 'NA'
        return re.sub(r'[^a-zA-Z0-9_]', '_', part.replace(' ', '_'))

    return f'{clean(candidate_name)}_{clean(job_title)}_{clean(company)}.pdf'
