import json

from django.core.management.base import BaseCommand, CommandError

from core.services.document_ai import DocumentAIError, analyze_delivery_note


class Command(BaseCommand):
    help = "Teste l'analyse OpenAI reelle sur un bon de livraison local."

    def add_arguments(self, parser):
        parser.add_argument("file_path", help="Chemin du fichier bon de livraison a analyser.")

    def handle(self, *args, **options):
        try:
            result = analyze_delivery_note(options["file_path"])
        except DocumentAIError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
