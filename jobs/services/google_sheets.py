"""Append job rows to a Google Sheet via a service account.

Best-effort: if credentials are missing or the API call fails, the error is
logged and the caller continues (search must not fail because of logging).
"""
import datetime
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Column order — mirrors the Excel export headers.
SHEET_HEADERS = [
    'Timestamp', 'Job Title', 'Company', 'Location', 'Date Posted',
    'Employment Type', 'Seniority Level', 'Salary', 'Sponsorship Flag',
    'Match Score', 'Match Reason', 'Application Link', 'Tailored CV Filename',
]


def job_to_row(job):
    """Convert a Job instance to a list of cell values in SHEET_HEADERS order."""
    tailored_name = ''
    if job.tailored_pdf:
        tailored_name = os.path.basename(job.tailored_pdf.name)
    return [
        datetime.datetime.utcnow().isoformat(timespec='seconds'),
        job.title,
        job.company,
        job.location,
        job.date_posted,
        job.employment_type,
        job.seniority_level,
        job.salary,
        job.get_sponsorship_flag_display(),
        job.match_score if job.match_score is not None else '',
        job.match_reason,
        job.application_link,
        tailored_name,
    ]


def _get_service(credentials_path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES,
    )
    return build('sheets', 'v4', credentials=credentials, cache_discovery=False)


def log_job_to_sheet(job, sheet_id=None, credentials_path=None):
    """Append one row describing ``job`` to the configured Google Sheet.

    Returns True on success, False if skipped or failed. Never raises.
    """
    sheet_id = sheet_id or getattr(settings, 'GOOGLE_SHEET_ID', '')
    credentials_path = credentials_path or getattr(
        settings, 'GOOGLE_SHEETS_CREDENTIALS_JSON', ''
    )

    if not sheet_id or not credentials_path:
        logger.info('Google Sheets not configured; skipping log for job %s', job.pk)
        return False
    if not os.path.exists(credentials_path):
        logger.warning('Google credentials file not found at %s', credentials_path)
        return False

    try:
        service = _get_service(credentials_path)
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range='A1',
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': [job_to_row(job)]},
        ).execute()
        return True
    except Exception:
        logger.exception('Failed to log job %s to Google Sheets', job.pk)
        return False
