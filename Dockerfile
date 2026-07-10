FROM python:3.10-slim

# Keep Python lean and unbuffered for container logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=job_automation.settings

WORKDIR /app

# System deps for psycopg2 and building wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code.
COPY . .

# Collect static files (safe to run at build; uses whitenoise manifest storage).
RUN DEBUG=False SECRET_KEY=build-time-key python manage.py collectstatic --noinput || true

# Entrypoint runs migrations then starts the given command.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "job_automation.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
