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


@override_settings(OPENAI_API_KEY='')
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


class KeywordExtractorTests(TestCase):
    def test_extract_skills_from_text(self):
        from jobs.services.keyword_extractor import extract_skills_from_text
        skills = extract_skills_from_text('Experienced in Python, Django and AWS. Also SEO.')
        self.assertIn('python', skills)
        self.assertIn('django', skills)
        self.assertIn('aws', skills)
        self.assertIn('seo', skills)
        # Word-boundary matching: "r" shouldn't match inside other words.
        self.assertNotIn('java', extract_skills_from_text('I use javascript only'))

    def test_search_keywords_prefer_job_titles(self):
        from jobs.services.keyword_extractor import extract_search_keywords
        data = {'job_titles': ['SEO Executive', 'SEO Specialist'], 'skills': ['seo']}
        self.assertEqual(extract_search_keywords(data), ['SEO Executive', 'SEO Specialist'])

    def test_search_keywords_fallback_to_raw_text(self):
        from jobs.services.keyword_extractor import extract_search_keywords
        kws = extract_search_keywords({'raw_text': 'I am a seo executive with 5 years'})
        self.assertIn('seo executive', kws)

    def test_keyword_match_score_and_missing(self):
        from jobs.services.keyword_extractor import keyword_match_score, missing_skills
        # CV covers 2 of the job's 4 required skills -> 50%.
        self.assertEqual(keyword_match_score(['python', 'sql'], ['python', 'sql', 'aws', 'go']), 50)
        # Neutral when either side has no skills (so nothing gets unfairly filtered).
        self.assertEqual(keyword_match_score([], ['python']), 50)
        self.assertEqual(missing_skills(['python'], ['python', 'aws']), ['aws'])

    def test_role_salary_range(self):
        from jobs.services.keyword_extractor import get_salary_range
        self.assertEqual(get_salary_range(['Junior Developer'])[0], 35000)  # 'developer' wins
        self.assertEqual(get_salary_range(['SEO Executive'])[0], 25000)
        self.assertEqual(get_salary_range(['Director of Ops'])[0], 60000)
        self.assertEqual(get_salary_range(['Unknown Role'], default_min=27000)[0], 27000)


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


@override_settings(MEDIA_ROOT=_TEST_MEDIA, OPENAI_API_KEY='')
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


@override_settings(MEDIA_ROOT=_TEST_MEDIA, OPENAI_API_KEY='')
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


@override_settings(MEDIA_ROOT=_TEST_MEDIA, OPENAI_API_KEY='')
class RunJobSearchTests(TestCase):
    """Exercises the async workflow function directly (no Celery/broker needed)."""

    def setUp(self):
        self.user = User.objects.create_user(username='erin', password='pw12345!')
        _make_cv_for(self.user)
        self.run = SearchRun.objects.create(
            user=self.user, countries=['United Kingdom'], min_salary=30000,
            status=SearchRun.STATUS_PENDING,
        )

    @mock.patch('jobs.tasks.GoogleSheetsLogger')
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
        # Every job is logged to the candidate's Google Sheets tab.
        self.assertEqual(mock_sheet.return_value.log_job.call_count, 2)

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
    @mock.patch('jobs.tasks.GoogleSheetsLogger')
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


@override_settings(MEDIA_ROOT=_TEST_MEDIA, CELERY_TASK_ALWAYS_EAGER=True, OPENAI_API_KEY='')
class CeleryTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='oscar', password='pw12345!')
        _make_cv_for(self.user)
        self.run = SearchRun.objects.create(
            user=self.user, countries=['United Kingdom'], status=SearchRun.STATUS_PENDING,
        )

    @mock.patch('jobs.tasks.GoogleSheetsLogger')
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


UK_CV_TEXT = """AMANDA TURNER
07123 456789 | amanda@example.com | London, UK

PROFESSIONAL PROFILE
Results-driven SEO Executive with 4+ years of experience in organic search and
content strategy, seeking a role in a dynamic agency.

KEY SKILLS
- Technical SEO
- Google Analytics
- Keyword Research
- SEMrush & Ahrefs

PROFESSIONAL EXPERIENCE
SEO Executive | XYZ Agency | London, UK
Jan 2022 - Present
- Led SEO strategy for 25+ clients, increasing organic traffic by 150%.
- Managed a team of 3 junior SEOs.

EDUCATION
BSc Digital Marketing | University of Manchester | Manchester, UK
2017 - 2020
- 2:1 (Upper Second Class Honours)

CERTIFICATIONS
- Google Analytics Individual Qualification
"""


class UkCvFormatTests(TestCase):
    def test_parses_all_sections(self):
        from jobs.services.pdf_generator import parse_cv_sections
        s = parse_cv_sections(UK_CV_TEXT)
        self.assertEqual(s['name'], 'AMANDA TURNER')
        self.assertIn('amanda@example.com', s['contact'])
        self.assertEqual(len(s['skills']), 4)
        self.assertIn('SEO Executive | XYZ Agency | London, UK', s['experience'])
        self.assertTrue(s['education'])
        self.assertTrue(s['certifications'])

    def test_body_text_containing_section_words_is_not_a_heading(self):
        """Regression: 'years of experience' must not be read as the EXPERIENCE heading."""
        from jobs.services.pdf_generator import parse_cv_sections
        s = parse_cv_sections(UK_CV_TEXT)
        profile = ' '.join(s['profile'])
        # The whole profile survives, including the sentence after 'experience'.
        self.assertIn('seeking a role in a dynamic agency', profile)
        # And the experience section holds only real roles, not profile prose.
        self.assertNotIn('Results-driven', ' '.join(s['experience']))

    def test_heading_aliases_and_markdown(self):
        from jobs.services.pdf_generator import parse_cv_sections
        s = parse_cv_sections(
            'Bob\n\n## Personal Statement\nHi.\n\n**Work History**\nAcme | Dev\n'
        )
        self.assertIn('Hi.', s['profile'])
        self.assertIn('Acme | Dev', s['experience'])

    def test_renders_uk_pdf_with_sections(self):
        from PyPDF2 import PdfReader
        path = os.path.join(_TEST_MEDIA, 'uk.pdf')
        generate_tailored_pdf(UK_CV_TEXT, 'Amanda Turner', 'SEO Executive', 'XYZ', path)
        text = PdfReader(path).pages[0].extract_text()
        for needle in ['AMANDA TURNER', 'PROFESSIONAL PROFILE', 'KEY SKILLS',
                       'PROFESSIONAL EXPERIENCE', 'EDUCATION', 'CERTIFICATIONS',
                       'Technical SEO', 'Upper Second Class']:
            self.assertIn(needle, text)


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
            job_skills=['python', 'sql'], missing_skills=['sql'], ats_score=92,
        )

    def test_disabled_when_not_configured(self):
        from jobs.services.google_sheets import GoogleSheetsLogger
        with override_settings(GOOGLE_SHEET_ID='', GOOGLE_SHEETS_CREDENTIALS_JSON=''):
            sheets = GoogleSheetsLogger()
        self.assertFalse(sheets.enabled)
        self.assertFalse(sheets.log_job(self.job, 'Gina'))

    def test_sanitize_tab_name(self):
        from jobs.services.google_sheets import sanitize_tab_name
        self.assertEqual(sanitize_tab_name('Haseeb Ijaz'), 'Haseeb Ijaz')
        self.assertEqual(sanitize_tab_name('A/B:C[D]'), 'A-B-C-D-')
        self.assertEqual(sanitize_tab_name(''), 'Candidate')
        self.assertEqual(len(sanitize_tab_name('x' * 80)), 50)

    def test_build_row_matches_headers(self):
        from jobs.services.google_sheets import HEADERS, GoogleSheetsLogger
        with override_settings(GOOGLE_SHEET_ID='', GOOGLE_SHEETS_CREDENTIALS_JSON=''):
            sheets = GoogleSheetsLogger()
        row = sheets.build_row(self.job, cv_skills=['python', 'django'])
        self.assertEqual(len(row), len(HEADERS))
        self.assertIn('Dev', row)
        self.assertEqual(row[HEADERS.index('Match Score')], 90)
        self.assertEqual(row[HEADERS.index('ATS Score')], 92)
        self.assertEqual(row[HEADERS.index('CV Parsed Skills')], 'python, django')
        self.assertEqual(row[HEADERS.index('Job Required Skills')], 'python, sql')
        self.assertEqual(row[HEADERS.index('Missing Skills')], 'sql')

    @mock.patch('jobs.services.google_sheets.os.path.exists', return_value=True)
    def test_creates_tab_per_candidate_and_appends(self, _exists):
        from jobs.services.google_sheets import GoogleSheetsLogger, HEADERS
        import gspread

        with override_settings(GOOGLE_SHEET_ID='sheet123',
                               GOOGLE_SHEETS_CREDENTIALS_JSON='/fake/creds.json'):
            with mock.patch('gspread.authorize') as authorize, \
                 mock.patch('google.oauth2.service_account.Credentials.from_service_account_file'):
                sheet = authorize.return_value.open_by_key.return_value
                # Candidate has no tab yet -> a new one is created with headers.
                sheet.worksheet.side_effect = gspread.exceptions.WorksheetNotFound('nope')
                new_tab = sheet.add_worksheet.return_value

                sheets = GoogleSheetsLogger()
                self.assertTrue(sheets.enabled)
                self.assertTrue(sheets.log_job(self.job, 'Haseeb Ijaz', cv_skills=['python']))

        sheet.add_worksheet.assert_called_once()
        self.assertEqual(sheet.add_worksheet.call_args.kwargs['title'], 'Haseeb Ijaz')
        # First append writes headers, second writes the job row.
        first_call = new_tab.append_row.call_args_list[0].args[0]
        second_call = new_tab.append_row.call_args_list[1].args[0]
        self.assertEqual(first_call, HEADERS)
        self.assertIn('Dev', second_call)
