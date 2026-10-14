import json

from django.core.management.base import BaseCommand, CommandError

from core.services.catalogue_ai import analyze_supplier_catalog
from core.services.document_ai import DocumentAIError


class Command(BaseCommand):
    help = "Teste l'analyse OpenAI reelle sur un catalogue fournisseur local."

    def add_arguments(self, parser):
        parser.add_argument("file_path", help="Chemin du fichier catalogue a analyser.")

    def handle(self, *args, **options):
        try:
            result = analyze_supplier_catalog(options["file_path"])
        except DocumentAIError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
