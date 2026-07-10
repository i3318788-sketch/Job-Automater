"""Print a complete audit of the application's data.

Usage:
    python manage.py db_audit            # summary + recent rows
    python manage.py db_audit --full     # every row, all fields
    python manage.py db_audit --model jobs.Job
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import connection

from accounts.models import UserProfile
from jobs.models import CV, Job, SearchRun, UserPreferences


class Command(BaseCommand):
    help = 'Print a complete audit of all application data.'

    def add_arguments(self, parser):
        parser.add_argument('--full', action='store_true',
                            help='Show every row instead of the most recent few.')
        parser.add_argument('--limit', type=int, default=10,
                            help='Rows to show per table when not --full (default 10).')

    # -- helpers ------------------------------------------------------------
    def _h1(self, text):
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n{"=" * 70}\n{text}\n{"=" * 70}'))

    def _h2(self, text):
        self.stdout.write(self.style.HTTP_INFO(f'\n--- {text} ---'))

    def _rows(self, qs):
        return qs if self.full else qs[: self.limit]

    # -- main ---------------------------------------------------------------
    def handle(self, *args, **options):
        self.full = options['full']
        self.limit = options['limit']

        db = connection.settings_dict
        self._h1('DATABASE')
        self.stdout.write(f'Engine:   {db["ENGINE"]}')
        self.stdout.write(f'Name:     {db["NAME"]}')
        self.stdout.write(f'Host:     {db.get("HOST") or "(local)"}:{db.get("PORT") or ""}')

        self._h1('ROW COUNTS')
        for label, model in [
            ('Users', User), ('UserProfiles', UserProfile), ('CVs', CV),
            ('UserPreferences', UserPreferences), ('SearchRuns', SearchRun),
            ('Jobs', Job),
        ]:
            self.stdout.write(f'{label:<18} {model.objects.count()}')

        # -- Users + profiles ----------------------------------------------
        self._h1('USERS & PROFILES')
        for user in self._rows(User.objects.all().order_by('id')):
            profile = getattr(user, 'profile', None)
            role = profile.get_role_display() if profile else 'no profile'
            name = profile.candidate_name if profile else ''
            flags = []
            if user.is_superuser:
                flags.append('superuser')
            if user.is_staff:
                flags.append('staff')
            self.stdout.write(
                f'#{user.id} {user.username} <{user.email or "no-email"}> '
                f'| role={role} | name="{name}" '
                f'| {", ".join(flags) or "regular"} | joined {user.date_joined:%Y-%m-%d %H:%M}'
            )

        # -- CVs ------------------------------------------------------------
        self._h1('CVs')
        for cv in self._rows(CV.objects.select_related('user').all()):
            text_len = len(cv.parsed_text or '')
            self.stdout.write(
                f'#{cv.id} user={cv.user.username} | file={cv.original_file.name} '
                f'| parsed_text={text_len} chars | uploaded {cv.upload_date:%Y-%m-%d %H:%M}'
            )
            if self.full and cv.parsed_text:
                self.stdout.write(f'    text preview: {cv.parsed_text[:200]!r}')

        # -- Preferences ----------------------------------------------------
        self._h1('USER PREFERENCES')
        for pref in self._rows(UserPreferences.objects.select_related('user').all()):
            self.stdout.write(
                f'#{pref.id} user={pref.user.username} '
                f'| countries={pref.target_countries} | min_salary={pref.min_salary}'
            )

        # -- SearchRuns -----------------------------------------------------
        self._h1('SEARCH RUNS')
        for run in self._rows(SearchRun.objects.select_related('user').all()):
            job_count = run.jobs.count()
            line = (
                f'#{run.id} user={run.user.username} | status={run.status} '
                f'| progress={run.progress}% | jobs={job_count} '
                f'| countries={run.countries} | min_salary={run.min_salary} '
                f'| created {run.created_at:%Y-%m-%d %H:%M}'
            )
            self.stdout.write(line)
            if run.error_message:
                self.stdout.write(self.style.ERROR(f'    error: {run.error_message}'))

        # -- Jobs -----------------------------------------------------------
        self._h1('JOBS')
        job_qs = Job.objects.select_related('search_run').all().order_by('-id')
        if not self.full:
            self.stdout.write(f'(showing {self.limit} most recent; use --full for all)')
        for job in self._rows(job_qs):
            pdf = job.tailored_pdf.name if job.tailored_pdf else '—'
            self.stdout.write(
                f'#{job.id} run={job.search_run_id} | score={job.match_score} '
                f'| "{job.title}" @ {job.company} | {job.location} '
                f'| salary={job.salary or "—"} | sponsor={job.sponsorship_flag} '
                f'| pdf={pdf} | processed={job.processed}'
            )
            if self.full:
                self.stdout.write(f'    reason: {job.match_reason}')
                self.stdout.write(f'    link:   {job.application_link}')

        self.stdout.write(self.style.SUCCESS('\nAudit complete.'))
