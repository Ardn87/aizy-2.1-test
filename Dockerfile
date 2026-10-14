FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . /app/
RUN useradd --create-home --uid 1000 django \
    && mkdir -p /app/media /app/staticfiles /app/backups /data \
    && chown -R django:django /app /data

USER django

EXPOSE 8080

CMD ["gunicorn", "facture_project.wsgi:application", "--bind", "0.0.0.0:8000"]
