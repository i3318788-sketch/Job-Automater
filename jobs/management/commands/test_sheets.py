"""Verify the Google Sheets connection end-to-end.

Usage:
    python manage.py test_sheets                       # connect + list tabs
    python manage.py test_sheets --write "Test User"   # also append a test row
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from jobs.services.google_sheets import HEADERS, GoogleSheetsLogger


class Command(BaseCommand):
    help = 'Check the Google Sheets service-account connection.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--write', metavar='CANDIDATE',
            help='Append a dummy row to this candidate\'s tab (creates it if needed).',
        )

    def handle(self, *args, **options):
        creds = settings.GOOGLE_SHEETS_CREDENTIALS_JSON
        sheet_id = settings.GOOGLE_SHEET_ID
        self.stdout.write(f'Credentials: {creds or "(not set)"}')
        self.stdout.write(f'Sheet ID:    {sheet_id or "(not set)"}')

        if not creds:
            raise CommandError('GOOGLE_SHEETS_CREDENTIALS_JSON is not set in .env')
        if not sheet_id:
            raise CommandError('GOOGLE_SHEET_ID is not set in .env')

        sheets = GoogleSheetsLogger()
        if not sheets.enabled:
            raise CommandError(
                'Could not open the sheet. Common causes:\n'
                '  - The sheet is not shared with the service account email '
                '(open the JSON, copy "client_email", share the sheet with it as Editor).\n'
                '  - GOOGLE_SHEET_ID is wrong (it is the long token in the sheet URL '
                'between /d/ and /edit).\n'
                '  - Google Sheets API and/or Google Drive API are not enabled for the project.\n'
                'See the logs above for the underlying error.'
            )

        self.stdout.write(self.style.SUCCESS(f'Connected to sheet: "{sheets.sheet.title}"'))
        tabs = [ws.title for ws in sheets.sheet.worksheets()]
        self.stdout.write(f'Existing tabs: {", ".join(tabs) or "(none)"}')

        candidate = options.get('write')
        if candidate:
            worksheet = sheets.get_or_create_worksheet(candidate)
            if worksheet is None:
                raise CommandError(f'Could not create/open tab for "{candidate}".')
            row = ['(test row)'] + [''] * (len(HEADERS) - 1)
            worksheet.append_row(row, value_input_option='USER_ENTERED')
            self.stdout.write(self.style.SUCCESS(
                f'Wrote a test row to tab "{worksheet.title}". '
                'Check the sheet, then delete the row.'
            ))
