# Job Application Automation System

A Django-based system for automating job applications. This repository currently
contains **Phase 1 — System Foundation**: project skeleton, data models,
authentication, CV upload & parsing, user preferences, and a basic dashboard.

## Tech stack

- Python 3.10+ (developed/tested on 3.13)
- Django 5.x
- SQLite by default (PostgreSQL supported via `DATABASE_URL`)
- `django-environ` for configuration
- `PyPDF2` / `python-docx` for CV text extraction

## Project layout

```
job_automation/     Project settings, root URLs, WSGI/ASGI
accounts/           User management: UserProfile, registration, auth
jobs/               CV, UserPreferences, SearchRun, Job models + views
templates/          Base template, auth pages, dashboard, forms
```

## Setup

### 1. Clone and create a virtual environment

```bash
python -m venv venv
# Windows (PowerShell):
venv\Scripts\Activate.ps1
# macOS/Linux:
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

Copy the example env file and adjust values:

```bash
cp .env.example .env
```

At minimum set a `SECRET_KEY`. Generate a URL-safe one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

> **Note:** Avoid `$` in any `.env` value (e.g. Django's
> `get_random_secret_key()` can emit `$`). Docker Compose interprets `$` as a
> variable reference, which corrupts the value and prints
> `The "..." variable is not set` warnings. The generator above is safe.

Available variables (see `.env.example`):

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Django secret key (required) |
| `DEBUG` | `True` for development |
| `ALLOWED_HOSTS` | Comma-separated hostnames |
| `DATABASE_URL` | Optional; omit for SQLite |
| `DEFAULT_MIN_SALARY` | System-wide default minimum salary |
| `APIFY_API_TOKEN` | Apify token for the job search actor |
| `OPENAI_API_KEY` | OpenAI key for matching & CV tailoring |
| `GOOGLE_SHEETS_CREDENTIALS_JSON` | Path to service-account JSON (Sheets logging) |
| `GOOGLE_SHEET_ID` | Target Google Sheet id (Sheets logging) |
| `MATCH_THRESHOLD` | Min score (default 75) for tailored-CV generation |
| `OPENAI_TAILOR_MODEL` | Model for CV tailoring (default `gpt-4o-mini`) |

### 4. Run migrations

```bash
python manage.py migrate
```

### 5. Create a superuser

```bash
python manage.py createsuperuser
```

### 6. Start the server

```bash
python manage.py runserver
```

Then visit:

- http://127.0.0.1:8000/ — dashboard (redirects to login)
- http://127.0.0.1:8000/accounts/register/ — register a new user
- http://127.0.0.1:8000/admin/ — Django admin

## Features (Phase 1)

- **Authentication**: register (captures full name), log in, log out. A
  `UserProfile` is auto-created for every user via a `post_save` signal.
- **CV upload**: upload a PDF or DOCX; text is extracted (`PyPDF2` /
  `python-docx`) and stored in `CV.parsed_text` / `CV.parsed_data`.
- **Preferences**: choose target countries (multi-select) and a minimum salary.
  The effective salary threshold falls back to the profile default, then the
  system-wide `DEFAULT_MIN_SALARY`.
- **Dashboard**: shows name/role, CV status, current preferences, and past
  search runs.
- **Admin**: all models (`UserProfile`, `CV`, `UserPreferences`, `SearchRun`,
  `Job`) are registered and editable.

## Phase 2 — Job search & matching

- **Apify integration** (`jobs/services/apify_service.py`): `search_jobs(country_list,
  min_salary=None, limit=200)` runs the UK jobs aggregator actor and returns
  normalized job dicts. Actor id configurable via `APIFY_JOBS_ACTOR`.
- **Matching engine** (`jobs/services/matching.py`):
  - `compute_match_score(cv_text, job_description)` → `{'score': int, 'reason': str}`
    via OpenAI (`gpt-4o-mini` by default, JSON response). Falls back to score 0
    if the key is missing or the call/parse fails.
  - `detect_sponsorship(text)` → `SPONSORED` / `NOT_MENTIONED` via keyword scan.
  - `parse_salary` / `salary_meets_threshold` for salary filtering.
- **Search workflow** (`start_search` view): creates a `SearchRun` (RUNNING),
  fetches jobs, and for each one: detects sponsorship, applies the salary filter
  (jobs below the minimum are kept but scored 0 with reason "Salary below
  minimum"), computes the match score, and stores a `Job`. On success the run is
  marked COMPLETED; any Apify/processing error marks it FAILED. Runs
  synchronously (Celery can be added later).
- **Results view** (`search_results`): per-run table of jobs sorted by match
  score, with sponsorship flag and apply links. Excel export is stubbed for
  Phase 3.

### Test commands (live API calls)

```bash
# Requires APIFY_API_TOKEN in .env
python manage.py test_apify --country "United Kingdom" --limit 5 --min-salary 30000

# Requires OPENAI_API_KEY in .env (falls back gracefully if absent)
python manage.py test_openai
```

## Phase 3 — Tailoring, PDF, Excel & Google Sheets

- **CV tailoring** (`jobs/services/tailoring.py`): `tailor_cv_for_job()` rewrites
  the CV to match a job via OpenAI (`OPENAI_TAILOR_MODEL`, default `gpt-4o-mini`),
  strictly without inventing content. Returns the original CV on any failure.
- **PDF generation** (`jobs/services/pdf_generator.py`): `generate_tailored_pdf()`
  builds a clean, single-page Helvetica PDF (centered bold name, rule, then
  heading-detected sections). `build_pdf_filename()` produces the safe name
  `CandidateName_JobTitle_Company.pdf`.
- **Workflow integration**: in `start_search`, any job scoring `>= MATCH_THRESHOLD`
  (default 75) gets a tailored CV + PDF saved to `media/tailored_cvs/` and linked
  on `Job.tailored_pdf` (tailored text stored on `Job.tailored_text`). Failures
  are logged and noted in `match_reason` without aborting the search.
- **Excel export** (`jobs/services/excel_export.py` + `export_excel` view):
  downloads `search_results_<id>.xlsx` with all fields, frozen header, autofilter,
  bold grey header, and a colour-coded Match Score column (green ≥75, yellow
  50–74, red <50). Available only for COMPLETED runs.
- **Google Sheets** (`jobs/services/google_sheets.py`): `log_job_to_sheet()`
  appends one row per job via a service account. Best-effort — missing
  credentials or API errors are logged, never fatal.

### Setting up Google Sheets (optional)

1. In Google Cloud, create a **service account** and download its JSON key.
2. Enable the **Google Sheets API** for the project.
3. Create a Google Sheet and **share it with the service-account email**
   (found in the JSON as `client_email`) with Editor access.
4. In `.env`, set `GOOGLE_SHEETS_CREDENTIALS_JSON` to the JSON key path and
   `GOOGLE_SHEET_ID` to the sheet id (the long token in the sheet URL).

If these are unset, searches still work — Sheets logging is simply skipped.

## Phase 4 — Async processing & deployment

Job searches now run **asynchronously** via Celery + Redis so the request
returns immediately and long OpenAI/Apify work happens in the background.

### How the async flow works

1. `start_search` creates a `SearchRun` (status `PENDING`) and calls
   `process_job_search.delay(run_id)`, then redirects to the dashboard.
2. The Celery task (`jobs/tasks.py`) sets the run to `RUNNING`, performs the full
   workflow (Apify → matching → tailoring → PDF → Excel-ready data → Google
   Sheets), updating `SearchRun.progress` as it goes, and finishes as
   `COMPLETED` or `FAILED` (with `error_message`).
3. The dashboard polls `GET /search/<id>/status/` every 5s (JSON) and refreshes
   when a run finishes.

### Running locally with Celery

You need Redis running (e.g. via Docker: `docker run -p 6379:6379 redis`), then:

```bash
# Terminal 1 — web
python manage.py runserver

# Terminal 2 — Celery worker
celery -A job_automation worker --loglevel=info
```

**No Redis?** Set `CELERY_TASK_ALWAYS_EAGER=True` in `.env` to run tasks inline
in the web process (fine for local testing, not for production).

### Running with Docker Compose (recommended for production-like)

```bash
cp .env.example .env          # set SECRET_KEY, API keys, DEBUG=False, etc.
docker compose up --build
```

Services started: **web** (Gunicorn), **db** (PostgreSQL), **redis**,
**celery-worker**, **celery-beat**. Media is bind-mounted to `./media` (so nginx
can serve uploaded files/PDFs in production); Google credentials are mounted
read-only from `./credentials/` (place your service-account JSON there and point
`GOOGLE_SHEETS_CREDENTIALS_JSON` at `/app/credentials/<file>.json`). Only the
`web` service runs migrations (`RUN_MIGRATIONS=0` on the workers).

**Deploying to a VPS (nginx + HTTPS):** see [deploy/README.md](deploy/README.md)
for a full production runbook, and `deploy/nginx.conf` for the reverse-proxy
config. Note: over plain HTTP, keep `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`
`False` (the defaults) or login will fail; flip them to `True` once HTTPS is on.

### Production settings

`DEBUG=False` enables secure cookies, `SECURE_CONTENT_TYPE_NOSNIFF`, and honours
`CSRF_TRUSTED_ORIGINS`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`. Static
files are served by WhiteNoise (hashed/compressed manifest storage), collected
during the Docker build. Set a strong `SECRET_KEY`, real `ALLOWED_HOSTS`, and a
`DATABASE_URL` (Compose sets it to the Postgres service automatically).

### Logging & monitoring

Logs go to the console (for Docker) and a rotating file at `logs/app.log`
(`LOG_LEVEL`, default INFO). Set `SENTRY_DSN` to enable Sentry error tracking.

### Optional extras

- **Completion emails**: set `SEND_COMPLETION_EMAIL=True` and configure the
  `EMAIL_*` vars (defaults to the console backend).
- **Cost control**: `OPENAI_MAX_SCORED_JOBS` (default 50) caps how many jobs are
  sent to OpenAI per run; the rest are stored unscored with a note.
- **Retries**: the Celery task is configured with `max_retries=2` for transient
  failures.

## Running tests

```bash
python manage.py test
```

Celery tasks are tested via the plain `run_job_search` function and the task's
`.run()` (with `CELERY_TASK_ALWAYS_EAGER`), so no broker is needed for tests.

## Notes

- Default minimum salary is configured via `DEFAULT_MIN_SALARY` in `.env`
  (default `30000`) and read in `settings.py`. Per-user overrides live on
  `UserPreferences.min_salary` and `UserProfile.default_min_salary`.
- CV parsing is intentionally simple in this phase (raw text plus empty
  `skills`/`experience`/`education` lists); it will be refined later.

## Status

The system is feature-complete and production-ready: registration/auth, CV
upload & parsing, preferences, asynchronous job search with OpenAI matching,
sponsorship detection, salary filtering, CV tailoring + single-page PDFs, Excel
export, Google Sheets logging, and a Dockerised deployment with Celery/Redis and
PostgreSQL.
