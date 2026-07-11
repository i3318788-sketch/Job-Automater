import io
import os
import tempfile
from unittest import mock

import docx
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CV, Job, SearchRun, UserPreferences
from .services.matching import (
    detect_sponsorship,
    parse_salary,
    salary_meets_threshold,
    _parse_match_response,
)
from .services.pdf_generator import build_pdf_filename, generate_tailored_pdf
from .services.tailoring import tailor_cv_for_job

# Isolate uploaded/generated media into a temp dir for the whole test module.
_TEST_MEDIA = tempfile.mkdtemp(prefix='ja_test_media_')


def build_docx_bytes():
    document = docx.Document()
    document.add_paragraph('Jane Doe - Software Engineer')
    document.add_paragraph('Skills: Python, Django, SQL')
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


class PreferencesViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='carol', password='pw12345!')

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_edit_preferences(self):
        self.client.login(username='carol', password='pw12345!')
        response = self.client.post(
            reverse('edit_preferences'),
            {'target_countries': ['United Kingdom', 'Remote'], 'salary_min': '45000',
             'salary_max': '80000', 'currency': 'GBP'},
        )
        self.assertEqual(response.status_code, 302)
        prefs = UserPreferences.objects.get(user=self.user)
        self.assertEqual(prefs.target_countries, ['United Kingdom', 'Remote'])
        self.assertEqual(str(prefs.salary_min), '45000.00')
        self.assertEqual(str(prefs.salary_max), '80000.00')


class CVUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dave', password='pw12345!')
        self.client.login(username='dave', password='pw12345!')

    def test_upload_docx_extracts_text(self):
        upload = SimpleUploadedFile(
            'jane.docx',
            build_docx_bytes(),
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        response = self.client.post(reverse('upload_cv'), {'original_file': upload})
        self.assertEqual(response.status_code, 302)
        cv = CV.objects.get(user=self.user)
        self.assertIn('Jane Doe', cv.parsed_text)
        self.assertIn('Python, Django, SQL', cv.parsed_text)
        self.assertEqual(cv.parsed_data['raw_text'], cv.parsed_text)

    def test_upload_rejects_unsupported_type(self):
        upload = SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain')
        response = self.client.post(reverse('upload_cv'), {'original_file': upload})
        self.assertEqual(response.status_code, 200)  # re-renders with error
        self.assertFalse(CV.objects.filter(user=self.user).exists())


class ApifyInputTests(TestCase):
    def test_build_actor_input_shape(self):
        from jobs.services.apify_service import _build_actor_input
        inp = _build_actor_input('United Kingdom', 30000, 50)
        # searchTerms must be a list (the actor rejects a string).
        self.assertIsInstance(inp['searchTerms'], list)
        # Country name mapped to the actor's enum code.
        self.assertEqual(inp['country'], 'uk')
        # Correct field names / minimums for this actor.
        self.assertEqual(inp['salary_min'], 30000)
        self.assertGreaterEqual(inp['max_results'], 100)
        self.assertIn('keyword', inp)
        self.assertEqual(inp['custom_location'], 'United Kingdom')
        self.assertNotIn('minSalary', inp)
        self.assertNotIn('maxItems', inp)

    def test_country_mapping_defaults_to_uk(self):
        from jobs.services.apify_service import _build_actor_input
        self.assertEqual(_build_actor_input('Narnia', None, 100)['country'], 'uk')
        self.assertEqual(_build_actor_input('United States', None, 100)['country'], 'us')

    def test_normalize_job_handles_apply_url_variant(self):
        from jobs.services.apify_service import normalize_job
        job = normalize_job({'title': 'Dev', 'company': 'Acme', 'applyUrl': 'https://x/1'})
        self.assertEqual(job['applyLink'], 'https://x/1')
        self.assertEqual(job['title'], 'Dev')


class MatchingHelperTests(TestCase):
    def test_detect_sponsorship_positive(self):
        text = 'We offer visa sponsorship for skilled worker candidates.'
        self.assertEqual(detect_sponsorship(text), 'SPONSORED')

    def test_detect_sponsorship_negative(self):
        self.assertEqual(detect_sponsorship('Great team, free coffee.'), 'NOT_MENTIONED')
        self.assertEqual(detect_sponsorship(''), 'NOT_MENTIONED')

    def test_parse_salary_variants(self):
        self.assertEqual(parse_salary('£40,000 - £50,000 per annum'), 50000)
        self.assertEqual(parse_salary('45k'), 45000)
        self.assertEqual(parse_salary('Competitive'), None)
        self.assertEqual(parse_salary(''), None)

    def test_salary_threshold(self):
        # Below threshold
        meets, parsed = salary_meets_threshold('£25,000', 30000)
        self.assertFalse(meets)
        self.assertEqual(parsed, 25000)
        # Above threshold
        meets, _ = salary_meets_threshold('£60,000', 30000)
        self.assertTrue(meets)
        # Unknown salary -> included
        meets, parsed = salary_meets_threshold('Competitive', 30000)
        self.assertTrue(meets)
        self.assertIsNone(parsed)

    def test_salary_within_range(self):
        from jobs.services.matching import salary_within_range
        # Below minimum
        within, _p, reason = salary_within_range('£25,000', 30000, 60000)
        self.assertFalse(within)
        self.assertEqual(reason, 'Salary below minimum')
        # Above maximum
        within, _p, reason = salary_within_range('£90,000', 30000, 60000)
        self.assertFalse(within)
        self.assertEqual(reason, 'Salary above maximum')
        # Within range
        within, _p, reason = salary_within_range('£45,000', 30000, 60000)
        self.assertTrue(within)
        self.assertEqual(reason, '')
        # No upper limit
        within, _p, _r = salary_within_range('£200,000', 30000, None)
        self.assertTrue(within)
        # Unknown salary -> included
        within, parsed, _r = salary_within_range('Competitive', 30000, 60000)
        self.assertTrue(within)
        self.assertIsNone(parsed)

    def test_parse_match_response_clamps_and_defaults(self):
        self.assertEqual(
            _parse_match_response('{"score": 150, "reason": "great"}'),
            {'score': 100, 'reason': 'great'},
        )
        self.assertEqual(
            _parse_match_response('not json'),
            {'score': 0, 'reason': 'Unable to compute'},
        )


def _make_cv_for(user, name='Test Profile'):
    return CV.objects.create(
        user=user,
        name=name,
        original_file=SimpleUploadedFile('cv.docx', build_docx_bytes()),
        parsed_text='Python Django engineer',
    )


TWO_RAW_JOBS = [
    {
        'title': 'Backend Engineer', 'company': 'Acme', 'location': 'London, UK',
        'datePosted': '2026-07-01', 'employmentType': 'Full-time',
        'seniorityLevel': 'Mid', 'salary': '£60,000',
        'description': 'Django REST APIs. Visa sponsorship available.',
        'applyLink': 'https://example.com/job/1',
    },
    {
        'title': 'Junior Dev', 'company': 'Beta', 'location': 'Leeds, UK',
        'datePosted': '', 'employmentType': '', 'seniorityLevel': '',
        'salary': '£20,000', 'description': 'Entry role.', 'applyLink': '',
    },
]


@override_settings(MEDIA_ROOT=_TEST_MEDIA)
class StartSearchViewTests(TestCase):
    """The view only enqueues a task; the workflow itself is tested separately."""

    def setUp(self):
        self.user = User.objects.create_user(username='erin', password='pw12345!')
        self.client.login(username='erin', password='pw12345!')

    def test_search_requires_cv(self):
        with mock.patch('jobs.views.process_job_search.delay') as delay:
            response = self.client.post(reverse('start_search'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SearchRun.objects.count(), 0)
        delay.assert_not_called()

    @mock.patch('jobs.views.process_job_search.delay')
    def test_search_enqueues_task_and_creates_pending_run(self, delay):
        cv = _make_cv_for(self.user)
        response = self.client.post(reverse('start_search'))
        # Redirects to dashboard, not results (async).
        self.assertRedirects(response, reverse('dashboard'))
        run = SearchRun.objects.get(user=self.user)
        self.assertEqual(run.status, SearchRun.STATUS_PENDING)
        self.assertEqual(run.cv, cv)  # search is tied to the active profile
        delay.assert_called_once_with(run.pk)

    def test_start_search_rejects_get(self):
        response = self.client.get(reverse('start_search'))
        self.assertEqual(response.status_code, 405)  # @require_POST

    def test_dashboard_renders_search_button_with_cv(self):
        _make_cv_for(self.user)
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Start New Search')

    def test_search_results_404_for_other_users_run(self):
        other = User.objects.create_user(username='mallory', password='pw12345!')
        run = SearchRun.objects.create(user=other)
        response = self.client.get(reverse('search_results', args=[run.pk]))
        self.assertEqual(response.status_code, 404)


class SearchStatusViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='nate', password='pw12345!')
        self.client.login(username='nate', password='pw12345!')

    def test_status_returns_json(self):
        run = SearchRun.objects.create(
            user=self.user, status=SearchRun.STATUS_RUNNING, progress=40,
        )
        response = self.client.get(reverse('search_status', args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'RUNNING')
        self.assertEqual(data['progress'], 40)
        self.assertEqual(data['id'], run.pk)

    def test_status_scoped_to_owner(self):
        other = User.objects.create_user(username='eve', password='pw12345!')
        run = SearchRun.objects.create(user=other)
        response = self.client.get(reverse('search_status', args=[run.pk]))
        self.assertEqual(response.status_code, 404)


@override_settings(MEDIA_ROOT=_TEST_MEDIA)
class ProfileManagementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='pat', password='pw12345!')
        self.client.login(username='pat', password='pw12345!')

    def test_empty_state_when_no_profiles(self):
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'No CVs found')

    def test_create_profile(self):
        response = self.client.post(reverse('create_profile'), {'name': 'John Doe'})
        self.assertRedirects(response, reverse('dashboard'))
        cv = CV.objects.get(user=self.user)
        self.assertEqual(cv.name, 'John Doe')
        self.assertFalse(cv.has_file)
        # New profile becomes the active tab.
        self.assertEqual(self.client.session['active_cv_id'], cv.pk)

    def test_create_profile_rejects_blank_name(self):
        response = self.client.post(reverse('create_profile'), {'name': '   '})
        self.assertRedirects(response, reverse('dashboard'))
        self.assertEqual(CV.objects.filter(user=self.user).count(), 0)

    def test_tabs_rendered_and_active_switches_via_query(self):
        a = _make_cv_for(self.user, name='Profile A')
        b = _make_cv_for(self.user, name='Profile B')
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'Profile A')
        self.assertContains(response, 'Profile B')
        # Switching via ?cv_id= sets the active profile in the session.
        self.client.get(reverse('dashboard') + f'?cv_id={b.pk}')
        self.assertEqual(self.client.session['active_cv_id'], b.pk)

    def test_delete_cv_switches_active_and_removes_record(self):
        a = _make_cv_for(self.user, name='Profile A')
        b = _make_cv_for(self.user, name='Profile B')
        # Make B active, then delete it -> falls back to remaining profile.
        self.client.get(reverse('dashboard') + f'?cv_id={b.pk}')
        response = self.client.post(reverse('delete_cv', args=[b.pk]))
        self.assertRedirects(response, reverse('dashboard'))
        self.assertFalse(CV.objects.filter(pk=b.pk).exists())
        self.assertEqual(self.client.session['active_cv_id'], a.pk)

    def test_delete_other_users_cv_forbidden(self):
        other = User.objects.create_user(username='intruder', password='pw12345!')
        cv = _make_cv_for(other, name='Secret')
        response = self.client.post(reverse('delete_cv', args=[cv.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CV.objects.filter(pk=cv.pk).exists())

    def test_upload_targets_active_profile(self):
        cv = _make_cv_for(self.user, name='Profile A')
        cv.original_file.delete(save=False)  # empty the profile
        cv.original_file = ''
        cv.parsed_text = ''
        cv.save()
        self.client.get(reverse('dashboard') + f'?cv_id={cv.pk}')  # make active

        upload = SimpleUploadedFile('resume.docx', build_docx_bytes())
        response = self.client.post(reverse('upload_cv'), {'original_file': upload})
        self.assertRedirects(response, reverse('dashboard'))
        # Same profile updated, not a new CV created.
        self.assertEqual(CV.objects.filter(user=self.user).count(), 1)
        cv.refresh_from_db()
        self.assertTrue(cv.has_file)
        self.assertIn('Jane Doe', cv.parsed_text)


class PreferencesCurrencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cara', password='pw12345!')
        self.client.login(username='cara', password='pw12345!')

    def test_currency_saved_and_default_gbp(self):
        response = self.client.post(
            reverse('edit_preferences'),
            {'target_countries': ['United Kingdom'], 'salary_min': '50000', 'currency': 'USD'},
        )
        self.assertRedirects(response, reverse('dashboard'))
        prefs = UserPreferences.objects.get(user=self.user)
        self.assertEqual(prefs.currency, 'USD')
        self.assertEqual(prefs.currency_symbol, '$')

    def test_salary_max_must_exceed_min(self):
        response = self.client.post(
            reverse('edit_preferences'),
            {'target_countries': [], 'salary_min': '60000', 'salary_max': '40000', 'currency': 'GBP'},
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with error
        self.assertFalse(UserPreferences.objects.filter(user=self.user, salary_min=60000).exists())


class ClearHistoryAndStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='hank', password='pw12345!')
        self.client.login(username='hank', password='pw12345!')

    def test_clear_search_history(self):
        SearchRun.objects.create(user=self.user, status=SearchRun.STATUS_COMPLETED)
        SearchRun.objects.create(user=self.user, status=SearchRun.STATUS_FAILED)
        other = User.objects.create_user(username='someoneelse', password='pw12345!')
        keep = SearchRun.objects.create(user=other)

        response = self.client.post(reverse('clear_search_history'))
        self.assertRedirects(response, reverse('dashboard'))
        self.assertEqual(SearchRun.objects.filter(user=self.user).count(), 0)
        self.assertTrue(SearchRun.objects.filter(pk=keep.pk).exists())  # other user's kept

    def test_clear_history_rejects_get(self):
        response = self.client.get(reverse('clear_search_history'))
        self.assertEqual(response.status_code, 405)

    def test_status_returns_total_and_processed(self):
        run = SearchRun.objects.create(
            user=self.user, status=SearchRun.STATUS_RUNNING, progress=50, total_jobs=10,
        )
        Job.objects.create(search_run=run, title='A', company='X', location='L',
                           application_link='https://x/1')
        data = self.client.get(reverse('search_status', args=[run.pk])).json()
        self.assertEqual(data['total'], 10)
        self.assertEqual(data['processed'], 1)
        self.assertEqual(data['progress'], 50)


@override_settings(MEDIA_ROOT=_TEST_MEDIA)
class RunJobSearchTests(TestCase):
    """Exercises the async workflow function directly (no Celery/broker needed)."""

    def setUp(self):
        self.user = User.objects.create_user(username='erin', password='pw12345!')
        _make_cv_for(self.user)
        self.run = SearchRun.objects.create(
            user=self.user, countries=['United Kingdom'], min_salary=30000,
            status=SearchRun.STATUS_PENDING,
        )

    @mock.patch('jobs.tasks.log_job_to_sheet')
    @mock.patch('jobs.tasks.compute_match_score')
    @mock.patch('jobs.tasks.search_jobs')
    def test_workflow_creates_jobs_and_completes(self, mock_search, mock_score, mock_sheet):
        from jobs.tasks import run_job_search
        mock_search.return_value = TWO_RAW_JOBS
        mock_score.return_value = {'score': 85, 'reason': 'Strong match'}

        result = run_job_search(self.run.pk)
        self.run.refresh_from_db()

        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.run.status, SearchRun.STATUS_COMPLETED)
        self.assertEqual(self.run.progress, 100)

        jobs = {j.title: j for j in self.run.jobs.all()}
        self.assertEqual(jobs['Backend Engineer'].match_score, 85)
        self.assertEqual(jobs['Backend Engineer'].sponsorship_flag, 'SPONSORED')
        self.assertTrue(jobs['Backend Engineer'].tailored_pdf)
        self.assertTrue(jobs['Backend Engineer'].processed)
        self.assertEqual(jobs['Junior Dev'].match_score, 0)
        self.assertEqual(jobs['Junior Dev'].match_reason, 'Salary below minimum')
        self.assertFalse(jobs['Junior Dev'].tailored_pdf)
        self.assertEqual(mock_score.call_count, 1)
        self.assertEqual(mock_sheet.call_count, 2)

    @mock.patch('jobs.tasks.search_jobs')
    def test_workflow_marks_failed_on_apify_error(self, mock_search):
        from jobs.services.apify_service import ApifySearchError
        from jobs.tasks import run_job_search
        mock_search.side_effect = ApifySearchError('boom')

        result = run_job_search(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(self.run.status, SearchRun.STATUS_FAILED)
        self.assertIn('boom', self.run.error_message)
        self.assertEqual(self.run.jobs.count(), 0)

    @override_settings(OPENAI_MAX_SCORED_JOBS=1)
    @mock.patch('jobs.tasks.log_job_to_sheet')
    @mock.patch('jobs.tasks.compute_match_score')
    @mock.patch('jobs.tasks.search_jobs')
    def test_scoring_cap_respected(self, mock_search, mock_score, mock_sheet):
        from jobs.tasks import run_job_search
        # Two salary-qualifying jobs, but cap allows scoring only one.
        mock_search.return_value = [
            {'title': 'A', 'company': 'X', 'location': 'London', 'salary': '£60,000',
             'description': 'Role A', 'applyLink': '', 'datePosted': '',
             'employmentType': '', 'seniorityLevel': ''},
            {'title': 'B', 'company': 'Y', 'location': 'London', 'salary': '£60,000',
             'description': 'Role B', 'applyLink': '', 'datePosted': '',
             'employmentType': '', 'seniorityLevel': ''},
        ]
        mock_score.return_value = {'score': 50, 'reason': 'ok'}

        run_job_search(self.run.pk)
        self.assertEqual(mock_score.call_count, 1)  # capped at 1
        reasons = [j.match_reason for j in self.run.jobs.all()]
        self.assertIn('Not scored (scoring limit reached)', reasons)


@override_settings(MEDIA_ROOT=_TEST_MEDIA, CELERY_TASK_ALWAYS_EAGER=True)
class CeleryTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='oscar', password='pw12345!')
        _make_cv_for(self.user)
        self.run = SearchRun.objects.create(
            user=self.user, countries=['United Kingdom'], status=SearchRun.STATUS_PENDING,
        )

    @mock.patch('jobs.tasks.log_job_to_sheet')
    @mock.patch('jobs.tasks.compute_match_score')
    @mock.patch('jobs.tasks.search_jobs')
    def test_task_runs_workflow(self, mock_search, mock_score, mock_sheet):
        from jobs.tasks import process_job_search
        mock_search.return_value = [TWO_RAW_JOBS[0]]
        mock_score.return_value = {'score': 80, 'reason': 'ok'}

        # Call the task function directly (equivalent to eager execution).
        result = process_job_search.run(self.run.pk)
        self.assertEqual(result['status'], 'COMPLETED')
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, SearchRun.STATUS_COMPLETED)

    def test_task_handles_missing_run(self):
        from jobs.tasks import process_job_search
        result = process_job_search.run(999999)
        self.assertEqual(result['status'], 'FAILED')


class TailoringTests(TestCase):
    def test_returns_original_when_openai_not_configured(self):
        # With no OPENAI_API_KEY configured, tailoring falls back to the original.
        with override_settings(OPENAI_API_KEY=''):
            result = tailor_cv_for_job('My CV text', 'Job desc', 'Engineer', 'Acme')
        self.assertEqual(result, 'My CV text')

    def test_empty_cv_returns_empty(self):
        self.assertEqual(tailor_cv_for_job('', 'Job desc', 'Engineer', 'Acme'), '')


class PdfGeneratorTests(TestCase):
    def test_build_pdf_filename_sanitizes(self):
        name = build_pdf_filename('Jane Doe', 'Senior Dev/Eng', 'Acme, Inc.')
        self.assertEqual(name, 'Jane_Doe_Senior_Dev_Eng_Acme__Inc_.pdf')

    def test_generates_valid_pdf_file(self):
        path = os.path.join(_TEST_MEDIA, 'out.pdf')
        cv_text = (
            'Summary\nExperienced engineer.\n'
            'Experience\nBuilt APIs at Acme.\n'
            'Skills\nPython, Django.'
        )
        generate_tailored_pdf(cv_text, 'Jane Doe', 'Engineer', 'Acme', path)
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 0)
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(5), b'%PDF-')  # PDF magic bytes

    def test_generates_pdf_without_headings(self):
        path = os.path.join(_TEST_MEDIA, 'plain.pdf')
        generate_tailored_pdf('Just some plain text.\nNo headings here.',
                              'John', 'Dev', 'Beta', path)
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(5), b'%PDF-')


@override_settings(MEDIA_ROOT=_TEST_MEDIA)
class ExcelExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='frank', password='pw12345!')
        self.client.login(username='frank', password='pw12345!')

    def _run_with_job(self, status=SearchRun.STATUS_COMPLETED, score=85):
        run = SearchRun.objects.create(user=self.user, status=status)
        Job.objects.create(
            search_run=run, title='Backend Engineer', company='Acme',
            location='London', salary='£60,000', match_score=score,
            match_reason='Strong match', application_link='https://x.com/1',
        )
        return run

    def test_export_returns_xlsx(self):
        run = self._run_with_job()
        response = self.client.get(reverse('export_excel', args=[run.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        self.assertIn(f'search_results_{run.pk}.xlsx', response['Content-Disposition'])

        # Load the returned bytes back into openpyxl and verify content.
        from openpyxl import load_workbook
        content = b''.join(response.streaming_content)
        wb = load_workbook(io.BytesIO(content))
        ws = wb['Jobs']
        self.assertEqual(ws['A1'].value, 'Job Title')
        self.assertEqual(ws['I1'].value, 'Match Score')
        self.assertEqual(ws['A2'].value, 'Backend Engineer')
        self.assertEqual(ws['I2'].value, 85)
        self.assertEqual(ws.freeze_panes, 'A2')

    def test_export_blocked_when_not_completed(self):
        run = self._run_with_job(status=SearchRun.STATUS_RUNNING)
        response = self.client.get(reverse('export_excel', args=[run.pk]))
        self.assertEqual(response.status_code, 302)  # redirected with error


class GoogleSheetsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='gina', password='pw12345!')
        self.run = SearchRun.objects.create(user=self.user)
        self.job = Job.objects.create(
            search_run=self.run, title='Dev', company='Acme', location='London',
            match_score=90, application_link='https://x.com/1',
        )

    def test_skips_when_not_configured(self):
        from jobs.services.google_sheets import log_job_to_sheet
        with override_settings(GOOGLE_SHEET_ID='', GOOGLE_SHEETS_CREDENTIALS_JSON=''):
            self.assertFalse(log_job_to_sheet(self.job))

    @mock.patch('jobs.services.google_sheets.os.path.exists', return_value=True)
    @mock.patch('jobs.services.google_sheets._get_service')
    def test_appends_row_when_configured(self, mock_service, _exists):
        from jobs.services.google_sheets import log_job_to_sheet
        append = mock_service.return_value.spreadsheets.return_value.values.return_value.append
        append.return_value.execute.return_value = {}
        with override_settings(GOOGLE_SHEET_ID='sheet123',
                               GOOGLE_SHEETS_CREDENTIALS_JSON='/fake/creds.json'):
            self.assertTrue(log_job_to_sheet(self.job))
        append.assert_called_once()
        # The appended row carries the job's values.
        body = append.call_args.kwargs['body']
        self.assertIn('Dev', body['values'][0])
