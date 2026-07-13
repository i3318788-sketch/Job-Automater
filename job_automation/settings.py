"""
Django settings for job_automation project.

Environment variables are loaded from a .env file at the project root using
django-environ. See .env.example for the required keys.
"""
from pathlib import Path

import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
env = environ.Env(
    DEBUG=(bool, False),
)

# Read the .env file at the project root (if present).
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY', default='django-insecure-change-me-in-production')

DEBUG = env.bool('DEBUG', default=True)

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# Third-party API credentials (placeholders — used in later phases).
APIFY_API_TOKEN = env('APIFY_API_TOKEN', default='')
OPENAI_API_KEY = env('OPENAI_API_KEY', default='')
GOOGLE_SHEETS_CREDENTIALS_JSON = env('GOOGLE_SHEETS_CREDENTIALS_JSON', default='')
GOOGLE_SHEET_ID = env('GOOGLE_SHEET_ID', default='')

# System-wide default minimum salary. Admins may override per-user via
# UserPreferences.min_salary; this is the fallback when none is set.
DEFAULT_MIN_SALARY = env.int('DEFAULT_MIN_SALARY', default=30000)

# Apify actor used by the job search service.
APIFY_JOBS_ACTOR = env('APIFY_JOBS_ACTOR', default='memo23/apify-uk-jobs-aggregator')

# Keyword/role the Apify jobs actor searches for (required by some actors).
APIFY_SEARCH_KEYWORD = env('APIFY_SEARCH_KEYWORD', default='software engineer')

# OpenAI model used for CV/job match scoring (kept small/cheap).
OPENAI_MATCH_MODEL = env('OPENAI_MATCH_MODEL', default='gpt-4o-mini')

# OpenAI model used for CV tailoring.
OPENAI_TAILOR_MODEL = env('OPENAI_TAILOR_MODEL', default='gpt-4o-mini')

# Jobs scoring at or above this threshold get a tailored CV + PDF generated.
MATCH_THRESHOLD = env.int('MATCH_THRESHOLD', default=75)

# Max jobs processed per search (bounds task time & API cost).
MAX_JOBS_PER_SEARCH = env.int('MAX_JOBS_PER_SEARCH', default=200)

# Cap the number of jobs sent to OpenAI for scoring (cost control). Jobs beyond
# this limit are stored unscored with a note.
OPENAI_MAX_SCORED_JOBS = env.int('OPENAI_MAX_SCORED_JOBS', default=50)

# Stage-1 filter: a job needs at least this % keyword overlap with the CV before
# we spend an OpenAI call on it. Lower it if too few jobs reach OpenAI scoring.
KEYWORD_PRESCORE_THRESHOLD = env.int('KEYWORD_PRESCORE_THRESHOLD', default=60)

# ---------------------------------------------------------------------------
# ATS checker
# ---------------------------------------------------------------------------
# A tailored CV scoring below this is reported as "below threshold".
ATS_THRESHOLD = env.int('ATS_THRESHOLD', default=75)

# When True, a job whose tailored CV lands below ATS_THRESHOLD is rejected
# outright rather than merely flagged. Jobs that fail a *hard* filter (phase 1
# parsing or a phase 2 knock-out) are rejected regardless of this setting.
ATS_STRICT_MODE = env.bool('ATS_STRICT_MODE', default=False)

# Tailoring aims for this ATS score, re-running with targeted feedback when the
# first attempt falls short.
ATS_TARGET_SCORE = env.int('ATS_TARGET_SCORE', default=90)

# How many extra tailoring passes to spend chasing ATS_TARGET_SCORE. Each pass
# is one more OpenAI call per job, so keep this low.
ATS_MAX_TAILOR_ATTEMPTS = env.int('ATS_MAX_TAILOR_ATTEMPTS', default=2)

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'crispy_forms',
    'crispy_bootstrap5',

    # Local apps
    'accounts',
    'jobs',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves static files under Gunicorn in production.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'job_automation.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'jobs.context_processors.cv_profiles',
            ],
        },
    },
]

WSGI_APPLICATION = 'job_automation.wsgi.application'

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Defaults to SQLite. Set DATABASE_URL in .env to use PostgreSQL, e.g.
#   DATABASE_URL=postgres://user:pass@localhost:5432/job_automation
DATABASES = {
    'default': env.db(
        'DATABASE_URL',
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
    ),
}

# ---------------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media files
# ---------------------------------------------------------------------------
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# WhiteNoise static storage: hashed/compressed manifest in production only
# (manifest storage requires collectstatic, so keep it simple in DEBUG).
_staticfiles_backend = (
    'django.contrib.staticfiles.storage.StaticFilesStorage'
    if DEBUG
    else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
)
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': _staticfiles_backend},
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# ---------------------------------------------------------------------------
# Crispy forms
# ---------------------------------------------------------------------------
CRISPY_ALLOWED_TEMPLATE_PACKS = 'bootstrap5'
CRISPY_TEMPLATE_PACK = 'bootstrap5'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Celery / Redis
# ---------------------------------------------------------------------------
REDIS_URL = env('REDIS_URL', default='redis://localhost:6379/0')
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default=REDIS_URL)
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default=REDIS_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
# When True, tasks run inline (no worker/broker needed) — handy for local dev/tests.
CELERY_TASK_ALWAYS_EAGER = env.bool('CELERY_TASK_ALWAYS_EAGER', default=False)
CELERY_TASK_EAGER_PROPAGATES = True

# ---------------------------------------------------------------------------
# Email (search-completion notifications)
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.console.EmailBackend',
)
EMAIL_HOST = env('EMAIL_HOST', default='localhost')
EMAIL_PORT = env.int('EMAIL_PORT', default=25)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=False)
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', default='no-reply@job-automation.local')
# Whether to email users when their search completes.
SEND_COMPLETION_EMAIL = env.bool('SEND_COMPLETION_EMAIL', default=False)

# ---------------------------------------------------------------------------
# Logging (console for Docker + rotating file)
# ---------------------------------------------------------------------------
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)
LOG_LEVEL = env('LOG_LEVEL', default='INFO')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} [{levelname}] {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(LOG_DIR / 'app.log'),
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 3,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': LOG_LEVEL,
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'jobs': {
            'handlers': ['console', 'file'],
            'level': LOG_LEVEL,
            'propagate': False,
        },
    },
}

# ---------------------------------------------------------------------------
# Production hardening (applied when DEBUG is False)
# ---------------------------------------------------------------------------
if not DEBUG:
    # Trust the reverse proxy's scheme header (nginx sets X-Forwarded-Proto).
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # IMPORTANT: these default to False so the app works over plain HTTP (e.g. a
    # fresh IP-based deploy). Once HTTPS is set up, set them to True in .env:
    #   SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True, SECURE_SSL_REDIRECT=True
    SESSION_COOKIE_SECURE = env.bool('SESSION_COOKIE_SECURE', default=False)
    CSRF_COOKIE_SECURE = env.bool('CSRF_COOKIE_SECURE', default=False)
    SECURE_SSL_REDIRECT = env.bool('SECURE_SSL_REDIRECT', default=False)
    SECURE_HSTS_SECONDS = env.int('SECURE_HSTS_SECONDS', default=0)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[])

# ---------------------------------------------------------------------------
# Sentry (optional error tracking)
# ---------------------------------------------------------------------------
SENTRY_DSN = env('SENTRY_DSN', default='')
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            traces_sample_rate=0.0,
            send_default_pii=False,
        )
    except ImportError:
        pass
