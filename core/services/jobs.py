"""File PostgreSQL durable ; aucun thread dans les processus web."""
import uuid
import logging
logger=logging.getLogger(__name__)
from datetime import timedelta
from django.db import transaction, connection
from django.utils import timezone
from django.core.exceptions import ValidationError
from core.models import AnalysisJob
from core.request_context import current_user


def enqueue(document_type, document_id):
    from core.views import background_ai_analysis_context
    document_type = 'bon_livraison' if document_type == 'reception_bon_livraison' else document_type
    with transaction.atomic():
        document, _, _, _ = background_ai_analysis_context(document_type, document_id)
        document = type(document).objects.select_for_update().get(pk=document.pk)
        job = AnalysisJob.objects.filter(document_type=document_type, document_id=document_id, state__in=['queued', 'running']).first()
        if job:
            return job
        user = current_user.get()
        job = AnalysisJob.objects.create(document_type=document_type, document_id=document_id, actor=user if user and user.is_authenticated else None)
        if document.statut != 'litige':
            document.statut = 'analyse_en_cours'
        document.erreur_analyse = ''
        document.save(update_fields=['statut', 'erreur_analyse', 'date_modification'])
        return job


def process_next():
    from core.views import background_ai_analysis_context, record_ai_error
    from core.request_context import current_user
    now = timezone.now()
    with transaction.atomic():
        # Lease exceeds the configured OCR timeout; interrupted tasks become resumable.
        AnalysisJob.objects.filter(state='running', started_at__lt=now-timedelta(minutes=30)).update(state='queued', token=None)
        job = AnalysisJob.objects.select_for_update(skip_locked=True).filter(state='queued').first()
        if job is None:
            return False
        job.state='running'; job.started_at=now; job.attempts += 1; job.token=uuid.uuid4()
        job.save()
    # Verrou de session : même après expiration d’une lease, deux workers ne traitent
    # jamais le même document simultanément. PostgreSQL libère le verrou à la déconnexion.
    lock_family = {'facture':1,'bon_livraison':2,'catalogue':3}[job.document_type]
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [lock_family, job.document_id])
        locked=cursor.fetchone()[0]
    if not locked:
        AnalysisJob.objects.filter(pk=job.pk,token=job.token).update(state='queued',token=None)
        return True
    actor_token = current_user.set(job.actor)
    try:
        document, runner, failure_marker, source = background_ai_analysis_context(job.document_type, job.document_id)
        try:
            runner(document)
            state, error = 'done', ''
        except Exception as exc:
            logger.exception("Analyse échouée : type=%s document=%s", job.document_type,job.document_id)
            failure_marker(document, exc)
            record_ai_error(source, exc, job.document_type, job.document_id)
            state, error = 'failed', type(exc).__name__
    except Exception as exc:
        state, error = 'failed', type(exc).__name__
    finally:
        current_user.reset(actor_token)
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [lock_family, job.document_id])
    AnalysisJob.objects.filter(pk=job.pk, token=job.token, state='running').update(state=state, error=error, finished_at=timezone.now())
    return True
