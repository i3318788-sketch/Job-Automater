#!/bin/sh
set -e

# Apply migrations, retrying while the database comes up. Set RUN_MIGRATIONS=0
# on the Celery services so only the web service migrates.
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "Applying database migrations (waiting for DB if needed)..."
    n=0
    until python manage.py migrate --noinput; do
        n=$((n + 1))
        if [ "$n" -ge 30 ]; then
            echo "Database still unavailable after 30 attempts; giving up."
            exit 1
        fi
        echo "Database not ready yet, retrying in 2s ($n/30)..."
        sleep 2
    done
fi

exec "$@"
