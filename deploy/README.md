# Production Deployment (OVHcloud VPS)

Runbook for deploying the Job Automation System to an Ubuntu VPS with Docker,
Celery/Redis, PostgreSQL, and nginx. Commands are run as `root` on the server.

> This corrects three issues that would otherwise break a fresh HTTP deploy:
> secure cookies over plain HTTP (login fails), media files 404ing, and nginx
> static/media path mismatches. See notes inline.

## 1. Server prep

```bash
apt update && apt upgrade -y
# Docker Engine + Compose v2 plugin (preferred over the old docker-compose):
apt install -y docker.io docker-compose-plugin git nginx
systemctl enable --now docker

# Firewall: allow SSH + HTTP/HTTPS
ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw --force enable
```

Use `docker compose` (v2, space) below. If you only have the old `docker-compose`
(hyphen) binary, the same subcommands work.

## 2. Clone & configure

```bash
mkdir -p /opt/job_automation && cd /opt/job_automation
git clone https://github.com/i3318788-sketch/Job-Automater.git .
mkdir -p media credentials            # media is bind-mounted & served by nginx
```

Create `.env` (never commit it). Generate a URL-safe SECRET_KEY — **do not use a
key with `$` in it**, Docker Compose will mangle it:

```bash
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(64))"
nano .env
```

Minimum `.env`:

```env
SECRET_KEY=<paste the generated key>
DEBUG=False
ALLOWED_HOSTS=51.79.55.83,localhost,jobautomation.yourdomain.com

# DATABASE_URL and REDIS_URL are set automatically by docker-compose (pointing
# at the db/redis services). To customise Postgres credentials, set these — the
# compose file uses them for BOTH the database and the app connection URL:
POSTGRES_DB=job_automation
POSTGRES_USER=jobuser
POSTGRES_PASSWORD=<choose-a-strong-password>

APIFY_API_TOKEN=<your apify token>
APIFY_JOBS_ACTOR=doggo/uk-jobs-board-scraper
MAX_JOBS_PER_SEARCH=50

OPENAI_API_KEY=<your openai key>
OPENAI_MATCH_MODEL=gpt-4o-mini
OPENAI_TAILOR_MODEL=gpt-4o-mini
MATCH_THRESHOLD=75
OPENAI_MAX_SCORED_JOBS=50

# Leave these False until HTTPS is configured (step 6). Over plain HTTP,
# secure cookies would make login impossible.
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
SECURE_SSL_REDIRECT=False
```

## 3. Build & start

```bash
docker compose up --build -d
docker compose ps            # all services Up; db & redis "healthy"
docker compose logs -f web   # migrations run automatically on start
```

Migrations run automatically (the web container's entrypoint). Create an admin:

```bash
docker compose exec web python manage.py createsuperuser
```

`collectstatic` already ran during the image build and static is served by
WhiteNoise — no manual step needed.

## 4. Validate the external APIs BEFORE relying on searches

The Apify actor (`doggo/uk-jobs-board-scraper`) may expect different input/output
fields than our default mapping. Test it directly first:

```bash
docker compose exec web python manage.py test_apify --country "United Kingdom" --limit 3
docker compose exec web python manage.py test_openai
```

If `test_apify` returns rows but titles/companies look empty, the actor's field
names differ — tell me the raw output and I'll adjust `jobs/services/apify_service.py`.

## 5. nginx reverse proxy

```bash
cp deploy/nginx.conf /etc/nginx/sites-available/job_automation
ln -sf /etc/nginx/sites-available/job_automation /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
```

The app is now reachable at **http://51.79.55.83**. (Static served by WhiteNoise
through the proxy; `/media/` served directly by nginx from `/opt/job_automation/media`.)

## 6. HTTPS (once a domain points at the VPS)

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d jobautomation.yourdomain.com
```

Then harden cookies in `.env` and restart the web service:

```env
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_SSL_REDIRECT=True
CSRF_TRUSTED_ORIGINS=https://jobautomation.yourdomain.com
```
```bash
docker compose up -d          # picks up the new env
```

## 7. Update / redeploy

```bash
cd /opt/job_automation
git pull
docker compose up --build -d
```

## 8. Backup the database

```bash
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-jobuser}" job_automation > backup_$(date +%F).sql
```

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Can't log in, form just reloads | `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` set True without HTTPS. Set them False (step 2). |
| CV/PDF links 404 | `./media` not bind-mounted, or nginx `/media/` alias wrong. Ensure `media/` exists and matches `deploy/nginx.conf`. |
| CSRF 403 on POST after adding HTTPS | Add `CSRF_TRUSTED_ORIGINS=https://yourdomain` to `.env`. |
| Search stuck PENDING | Celery worker not running: `docker compose logs celery-worker`; check `redis` is healthy. |
| Search FAILED "Apify not configured" | `APIFY_API_TOKEN` missing/typo in `.env`; `docker compose up -d` to reload. |
| DB auth failed | If you set `POSTGRES_*`, wipe the old volume: `docker compose down -v` then `up` (⚠ deletes data). |
