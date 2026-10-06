#!/bin/sh
set -e
# create the initial web UI admin on first start (no-op when it already exists)
python -m app.cli create-user admin --role admin --password "${WEBUI_ADMIN_PASSWORD:-AdminPass123}" 2>/dev/null || true
exec gunicorn -b 0.0.0.0:8080 -w 2 -k gthread --threads 4 --timeout 120 \
  --access-logfile - --error-logfile - wsgi:application
