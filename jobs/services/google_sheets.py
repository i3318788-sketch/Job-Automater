"""Log jobs to a Google Sheet, with one worksheet (tab) per candidate.

Uses a service account. Best-effort throughout: if credentials are missing or the
API fails, the error is logged and the caller continues — a search must never
fail because of logging.
"""
import logging
import os
import re

from django.conf import settings

logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

# The sheet's columns, A-L, in order. build_row() must return values in exactly
# this order and of exactly this length — the two are checked against each other
# on every write, because a row that is one item short silently shifts every
# value after it into the wrong column.
HEADERS = [
    'Job Title',                # A
    'Company Name',             # B
    'Location',                 # C
    'Date Posted',              # D
    'Employment Type',          # E
    'Seniority Level',          # F
    'Salary',                   # G
    'Sponsorship Flag',         # H
    'Match Score',              # I
    'Match Reason',             # J
    'Direct Application Link',  # K
    'Tailored CV Filename',     # L
]

# Column L. Derived, so adding a header cannot leave this stale.
LAST_COLUMN = chr(ord('A') + len(HEADERS) - 1)

# Row 1 is the header row. Data starts at row 2 and never above it.
FIRST_DATA_ROW = 2

# Characters Google Sheets disallows in a worksheet title.
_INVALID_TAB_CHARS = re.compile(r"[\[\]:*?/\\]")


def sanitize_tab_name(name):
    """Make a candidate name safe for use as a worksheet title."""
    cleaned = _INVALID_TAB_CHARS.sub('-', str(name or '').strip())
    return (cleaned or 'Candidate')[:50]


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
                # Headers go in row 1 of the new tab, by explicit range. An
                # existing tab is never touched — its row 1 is left exactly as
                # the candidate set it up.
                worksheet.update(
                    range_name=f'A1:{LAST_COLUMN}1', values=[HEADERS],
                    value_input_option='USER_ENTERED',
                )
                logger.info('Created Google Sheets tab "%s"', title)
                return worksheet
        except Exception:
            logger.exception('Could not get/create worksheet "%s"', title)
            return None

    def build_row(self, job, cv_skills=None):
        """Build one job's values for columns A-L, in HEADERS order.

        ``cv_skills`` is accepted and ignored: the sheet has no skills columns.
        The parameter stays so callers do not have to change.
        """
        tailored_name = (
            os.path.basename(job.tailored_pdf.name) if job.tailored_pdf else ''
        )
        row = [
            job.title or '',                                # A Job Title
            job.company or '',                              # B Company Name
            job.location or '',                             # C Location
            job.date_posted or '',                          # D Date Posted
            job.employment_type or '',                      # E Employment Type
            job.seniority_level or '',                      # F Seniority Level
            job.salary or '',                               # G Salary
            job.get_sponsorship_flag_display(),             # H Sponsorship Flag
            # Blank, not 0: an unscored job has no score, and writing a zero into
            # the sheet would read as "terrible match" rather than "not measured".
            job.match_score if job.match_score is not None else '',   # I Match Score
            job.match_reason or '',                         # J Match Reason
            job.application_link or '',                     # K Direct Application Link
            tailored_name,                                  # L Tailored CV Filename
        ]
        if len(row) != len(HEADERS):  # pragma: no cover - guards a coding error
            raise ValueError(
                f'build_row produced {len(row)} values for {len(HEADERS)} columns; '
                'a mismatch shifts every later value into the wrong column.'
            )
        return row

    def next_data_row(self, worksheet):
        """The first free row, never above the header row.

        ``append_row`` is not used here. It appends after whatever gspread decides
        the last row of the "table" is, which is why rows landed above the headers
        when the sheet's first rows were not what it expected. Reading the used
        range and writing to an explicit A-L range removes the guesswork: the
        target cells are chosen here, not inferred by the API.
        """
        existing = worksheet.get_all_values()
        return max(len(existing) + 1, FIRST_DATA_ROW)

    def log_job(self, job, candidate_name, cv_skills=None):
        """Write one job into columns A-L of the next free row. True on success."""
        if not self.enabled:
            return False
        worksheet = self.get_or_create_worksheet(candidate_name)
        if worksheet is None:
            return False
        try:
            row = self.build_row(job)
            index = self.next_data_row(worksheet)
            target = f'A{index}:{LAST_COLUMN}{index}'
            worksheet.update(
                range_name=target, values=[row], value_input_option='USER_ENTERED',
            )
            logger.info(
                'Logged job %s to Google Sheets tab "%s" at %s',
                job.pk, sanitize_tab_name(candidate_name), target,
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
