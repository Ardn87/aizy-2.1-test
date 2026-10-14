import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from core.services.jobs import process_next

class Command(BaseCommand):
    help = 'Traite les analyses persistantes (redémarrage sans perte des documents).'
    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
    def handle(self, *args, **options):
        while True:
            close_old_connections()
            worked = process_next()
            if options['once']:
                break
            if not worked:
                time.sleep(2)
