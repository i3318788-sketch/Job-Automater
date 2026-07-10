"""Celery application for job_automation.

Started separately from the web process, e.g.:
    celery -A job_automation worker --loglevel=info
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'job_automation.settings')

app = Celery('job_automation')

# Read config from Django settings, using the CELERY_ namespace.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks.py in installed apps.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
