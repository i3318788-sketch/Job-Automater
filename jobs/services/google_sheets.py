"""Log jobs to a Google Sheet, with one worksheet (tab) per candidate.

Uses a service account. Best-effort throughout: if credentials are missing or the
API fails, the error is logged and the caller continues — a search must never
fail because of logging.
"""
import datetime
import logging
import os
import re

from django.conf import settings

logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

HEADERS = [
    'Date', 'Job Title', 'Company', 'Location', 'Salary',
    'Match Score', 'Match Reason', 'Sponsorship Flag',
    'Application Link', 'Tailored CV Filename',
    'CV Parsed Skills', 'Job Required Skills',
    'Missing Skills', 'ATS Score', 'Status',
]

# Characters Google Sheets disallows in a worksheet title.
_INVALID_TAB_CHARS = re.compile(r"[\[\]:*?/\\]")


def sanitize_tab_name(name):
    """Make a candidate name safe for use as a worksheet title."""
    cleaned = _INVALID_TAB_CHARS.sub('-', str(name or '').strip())
    return (cleaned or 'Candidate')[:50]


def _join(values):
    """Render a list of skills as a comma-separated cell value."""
    if not values:
        return ''
    if isinstance(values, str):
        return values
    return ', '.join(str(v) for v in values)


class GoogleSheetsLogger:
    """Appends job rows to a per-candidate tab in a single Google Sheet."""

    def __init__(self, credentials_path=None, sheet_id=None):
        self.client = None
        self.sheet = None
        self.credentials_path = credentials_path or getattr(
            settings, 'GOOGLE_SHEETS_CREDENTIALS_JSON', ''
        )
        self.sheet_id = sheet_id or getattr(settings, 'GOOGLE_SHEET_ID', '')
        self._initialize()

    @property
    def enabled(self):
        return self.sheet is not None

    def _initialize(self):
        if not self.credentials_path or not self.sheet_id:
            logger.info('Google Sheets not configured; skipping.')
            return
        if not os.path.exists(self.credentials_path):
            logger.warning(
                'Google credentials file not found at %s', self.credentials_path
            )
            return
        try:
            import gspread
            from google.oauth2.service_account import Credentials

            creds = Credentials.from_service_account_file(
                self.credentials_path, scopes=SCOPES
            )
            self.client = gspread.authorize(creds)
            self.sheet = self.client.open_by_key(self.sheet_id)
            logger.info('Google Sheets initialised for sheet %s', self.sheet_id)
        except Exception:
            logger.exception('Failed to initialise Google Sheets')
            self.client = None
            self.sheet = None

    def get_or_create_worksheet(self, candidate_name):
        """Return the candidate's tab, creating it (with headers) if needed."""
        if not self.sheet:
            return None
        title = sanitize_tab_name(candidate_name)
        try:
            import gspread

            try:
                return self.sheet.worksheet(title)
            except gspread.exceptions.WorksheetNotFound:
                worksheet = self.sheet.add_worksheet(
                    title=title, rows=1000, cols=len(HEADERS)
                )
                worksheet.append_row(HEADERS, value_input_option='USER_ENTERED')
                logger.info('Created Google Sheets tab "%s"', title)
                return worksheet
        except Exception:
            logger.exception('Could not get/create worksheet "%s"', title)
            return None

    def build_row(self, job, cv_skills=None):
        """Build the row values for a Job, in HEADERS order."""
        tailored_name = (
            os.path.basename(job.tailored_pdf.name) if job.tailored_pdf else ''
        )
        return [
            datetime.date.today().isoformat(),
            job.title,
            job.company,
            job.location,
            job.salary or '',
            job.match_score if job.match_score is not None else 0,
            job.match_reason or '',
            job.get_sponsorship_flag_display(),
            job.application_link or '',
            tailored_name,
            _join(cv_skills),
            _join(job.job_skills),
            _join(job.missing_skills),
            job.ats_score if job.ats_score is not None else '',
            'Active',
        ]

    def log_job(self, job, candidate_name, cv_skills=None):
        """Append one job row to the candidate's tab. Returns True on success."""
        if not self.enabled:
            return False
        worksheet = self.get_or_create_worksheet(candidate_name)
        if worksheet is None:
            return False
        try:
            worksheet.append_row(
                self.build_row(job, cv_skills), value_input_option='USER_ENTERED'
            )
            logger.info(
                'Logged job %s to Google Sheets tab "%s"',
                job.pk, sanitize_tab_name(candidate_name),
            )
            return True
        except Exception:
            logger.exception('Failed to log job %s to Google Sheets', job.pk)
            return False


def log_job_to_sheet(job, candidate_name=None, cv_skills=None):
    """Backwards-compatible helper: log a single job. Never raises."""
    try:
        name = candidate_name or (
            job.search_run.cv.display_name if job.search_run.cv
            else job.search_run.user.username
        )
        return GoogleSheetsLogger().log_job(job, name, cv_skills=cv_skills)
    except Exception:
        logger.exception('Google Sheets logging failed for job %s', getattr(job, 'pk', '?'))
        return False
