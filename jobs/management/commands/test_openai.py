"""Test the OpenAI matching function with sample or provided text.

Usage:
    python manage.py test_openai
    python manage.py test_openai --cv path/to/cv.txt --job path/to/job.txt
"""
from django.core.management.base import BaseCommand

from jobs.services.matching import compute_match_score, detect_sponsorship

SAMPLE_CV = (
    'Software Engineer with 5 years of experience in Python, Django, and '
    'PostgreSQL. Built REST APIs and led a team of three. BSc Computer Science.'
)
SAMPLE_JOB = (
    'We are hiring a Backend Python Developer to build Django REST APIs backed '
    'by PostgreSQL. 3+ years experience required. Visa sponsorship available '
    'for skilled worker candidates.'
)


class Command(BaseCommand):
    help = 'Run the OpenAI match scoring on sample or provided CV/job text.'

    def add_arguments(self, parser):
        parser.add_argument('--cv', default=None, help='Path to a CV text file.')
        parser.add_argument('--job', default=None, help='Path to a job description text file.')

    def _read(self, path, fallback):
        if not path:
            return fallback
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read()

    def handle(self, *args, **options):
        cv_text = self._read(options['cv'], SAMPLE_CV)
        job_text = self._read(options['job'], SAMPLE_JOB)

        self.stdout.write('Computing match score via OpenAI ...')
        result = compute_match_score(cv_text, job_text)
        sponsorship = detect_sponsorship(job_text)

        self.stdout.write(self.style.SUCCESS(f'Score:  {result["score"]}'))
        self.stdout.write(f'Reason: {result["reason"]}')
        self.stdout.write(f'Sponsorship: {sponsorship}')
