"""Run a real Apify job search and print the results.

Usage:
    python manage.py test_apify --country "United Kingdom" --limit 5 --min-salary 30000
"""
from django.core.management.base import BaseCommand, CommandError

from jobs.services.apify_service import (
    ApifyConfigError,
    ApifySearchError,
    search_jobs,
)


class Command(BaseCommand):
    help = 'Run a live Apify job search and print the results.'

    def add_arguments(self, parser):
        parser.add_argument('--country', default='United Kingdom')
        parser.add_argument('--limit', type=int, default=5)
        parser.add_argument('--min-salary', type=int, default=None)

    def handle(self, *args, **options):
        country = options['country']
        limit = options['limit']
        min_salary = options['min_salary']

        self.stdout.write(
            f'Searching Apify for country="{country}", limit={limit}, '
            f'min_salary={min_salary} ...'
        )
        try:
            jobs = search_jobs([country], min_salary=min_salary, limit=limit)
        except ApifyConfigError as exc:
            raise CommandError(f'Apify not configured: {exc}')
        except ApifySearchError as exc:
            raise CommandError(f'Apify search failed: {exc}')

        self.stdout.write(self.style.SUCCESS(f'Fetched {len(jobs)} job(s):'))
        for i, job in enumerate(jobs, 1):
            self.stdout.write(
                f'\n{i}. {job["title"]} @ {job["company"]}\n'
                f'   Location: {job["location"]}\n'
                f'   Salary:   {job["salary"] or "(not listed)"}\n'
                f'   Posted:   {job["datePosted"] or "(unknown)"}\n'
                f'   Apply:    {job["applyLink"] or "(none)"}\n'
                f'   Desc:     {job["description"][:120]}...'
            )
