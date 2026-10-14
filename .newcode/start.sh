#!/bin/sh
set -eu

umask 077
mkdir -p /data/media /data/staticfiles
if [ ! -s /data/django-secret ]; then
  python -c 'import secrets; print(secrets.token_urlsafe(48))' > "/data/.django-secret.$$"
  chmod 600 "/data/.django-secret.$$"
  mv "/data/.django-secret.$$" /data/django-secret
fi
DJANGO_SECRET_KEY=$(cat /data/django-secret)
export DJANGO_SECRET_KEY

python manage.py migrate --noinput
python scripts/bootstrap_demo.py
python manage.py collectstatic --noinput >/dev/null

exec gunicorn facture_project.wsgi:application --bind 0.0.0.0:8080 --workers 1 --timeout 120
