#!/bin/sh
set -eu

# Only web owns migrations. Workers wait for web's health check in compose.yaml.
if [ "${1:-}" = "gunicorn" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
fi

exec "$@"
