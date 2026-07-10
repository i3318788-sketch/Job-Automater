"""Generate a clean, single-page tailored-CV PDF using reportlab."""
import logging
import re

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

logger = logging.getLogger(__name__)

# Common CV section headings we try to detect for styling.
KNOWN_HEADINGS = {
    'summary', 'profile', 'objective', 'about',
    'experience', 'work experience', 'employment', 'professional experience',
    'education', 'qualifications',
    'skills', 'technical skills', 'core skills',
    'projects', 'certifications', 'achievements', 'awards',
    'languages', 'interests', 'references', 'contact',
}


def _build_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name='CandidateName', parent=styles['Title'], fontName='Helvetica-Bold',
        fontSize=20, alignment=TA_CENTER, spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name='SectionHeading', parent=styles['Heading2'], fontName='Helvetica-Bold',
        fontSize=11, textColor=HexColor('#1f2937'), spaceBefore=8, spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        name='Body', parent=styles['Normal'], fontName='Helvetica',
        fontSize=9, leading=12, spaceAfter=3,
    ))
    return styles


def _is_heading(line):
    """Heuristic: a short line matching a known CV heading (with/without colon)."""
    stripped = line.strip().rstrip(':').strip()
    if not stripped or len(stripped) > 40:
        return False
    normalized = stripped.lower()
    if normalized in KNOWN_HEADINGS:
        return True
    # ALL-CAPS short lines are commonly headings too.
    if stripped.isupper() and len(stripped.split()) <= 4:
        return True
    return False


def _escape(text):
    """Escape XML-special characters for reportlab Paragraph markup."""
    return (
        text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    )


def generate_tailored_pdf(cv_text, candidate_name, job_title, company, output_path):
    """Write a single-page PDF of ``cv_text`` to ``output_path``.

    The candidate's name is centered and bold at the top, followed by a subtle
    rule, then the CV body split into styled sections when headings are detected.
    """
    styles = _build_styles()
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f'{candidate_name} - CV',
    )

    story = [
        Paragraph(_escape(candidate_name or 'Candidate'), styles['CandidateName']),
        HRFlowable(width='100%', thickness=0.8, color=HexColor('#9ca3af'),
                   spaceBefore=2, spaceAfter=8),
    ]

    lines = (cv_text or '').splitlines()
    has_headings = any(_is_heading(ln) for ln in lines)

    if not has_headings:
        # No detectable structure: emit non-empty lines as body paragraphs.
        for line in lines:
            if line.strip():
                story.append(Paragraph(_escape(line.strip()), styles['Body']))
    else:
        for line in lines:
            if not line.strip():
                continue
            if _is_heading(line):
                story.append(Paragraph(
                    _escape(line.strip().rstrip(':').strip()),
                    styles['SectionHeading'],
                ))
            else:
                story.append(Paragraph(_escape(line.strip()), styles['Body']))

    if len(story) <= 2:
        story.append(Paragraph('(No CV content available.)', styles['Body']))

    doc.build(story)
    logger.info('Generated tailored PDF at %s', output_path)


def build_pdf_filename(candidate_name, job_title, company):
    """Build a filesystem-safe PDF name: CandidateName_JobTitle_Company.pdf."""
    def clean(part):
        part = (part or '').strip() or 'NA'
        return re.sub(r'[^a-zA-Z0-9_]', '_', part.replace(' ', '_'))

    return f'{clean(candidate_name)}_{clean(job_title)}_{clean(company)}.pdf'
