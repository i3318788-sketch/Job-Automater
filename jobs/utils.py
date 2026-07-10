"""Helpers for extracting text from uploaded CV files."""
import os

from django.core.exceptions import ValidationError

ALLOWED_EXTENSIONS = {'.pdf', '.docx'}


def validate_cv_extension(filename):
    """Raise ValidationError if the file is not a PDF or DOCX."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            'Unsupported file type "%(ext)s". Please upload a PDF or DOCX file.',
            params={'ext': ext or '(none)'},
        )
    return ext


def extract_text_from_pdf(file_obj):
    """Extract text from a PDF file-like object using PyPDF2."""
    from PyPDF2 import PdfReader

    reader = PdfReader(file_obj)
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or '')
        except Exception:
            # Skip pages that fail to extract rather than aborting the upload.
            continue
    return '\n'.join(parts).strip()


def extract_text_from_docx(file_obj):
    """Extract text from a DOCX file-like object using python-docx."""
    import docx

    document = docx.Document(file_obj)
    paragraphs = [p.text for p in document.paragraphs]
    # Include table cell text too, since CVs often use tables for layout.
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    paragraphs.append(cell.text)
    return '\n'.join(paragraphs).strip()


def extract_cv_text(uploaded_file):
    """Return extracted text for an uploaded PDF or DOCX file."""
    ext = validate_cv_extension(uploaded_file.name)
    # Rewind in case the file has been read during validation.
    uploaded_file.seek(0)
    if ext == '.pdf':
        return extract_text_from_pdf(uploaded_file)
    return extract_text_from_docx(uploaded_file)
