from django.core.management.base import BaseCommand, CommandError

from catalog_import.models import CatalogImport
from catalog_import.services.catalog_import_service import import_catalog


class Command(BaseCommand):
    help = "Parse a single page from an existing catalog PDF."

    def add_arguments(self, parser):

        parser.add_argument(
            "--catalog",
            type=int,
            required=True,
            help="CatalogImport ID",
        )

        parser.add_argument(
            "--page",
            type=int,
            required=True,
            help="Page number to parse",
        )

    def handle(self, *args, **options):

        catalog_id = options["catalog"]
        page = options["page"]

        try:

            catalog = CatalogImport.objects.get(pk=catalog_id)

        except CatalogImport.DoesNotExist:

            raise CommandError(
                f"Catalog {catalog_id} not found."
            )

        self.stdout.write("")
        self.stdout.write("=" * 80)
        self.stdout.write(f"Developer Mode")
        self.stdout.write(f"Catalog : {catalog.id}")
        self.stdout.write(f"Page    : {page}")
        self.stdout.write("=" * 80)

        import_catalog(
            pdf_file=catalog.pdf,
            category=catalog.category,
            brand=catalog.brand,
            start_page=page,
            end_page=page,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Finished."))