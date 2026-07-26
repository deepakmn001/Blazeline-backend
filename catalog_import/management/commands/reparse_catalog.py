from django.core.management.base import BaseCommand, CommandError

from catalog_import.services.reparse_service import (
    reparse_catalog,
    reparse_page,
)


class Command(BaseCommand):
    help = "Reparse an imported catalog or a single page."

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
            help="Reparse only one page",
        )

    def handle(self, *args, **options):
        catalog_id = options["catalog"]
        page = options.get("page")

        try:

            if page is not None:

                self.stdout.write(
                    self.style.WARNING(
                        f"Reparsing catalog {catalog_id}, page {page}..."
                    )
                )

                summary = reparse_page(
                    catalog_id=catalog_id,
                    page=page,
                )

            else:

                self.stdout.write(
                    self.style.WARNING(
                        f"Reparsing entire catalog {catalog_id}..."
                    )
                )

                summary = reparse_catalog(
                    catalog_id=catalog_id,
                )

            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS("Reparse completed successfully.")
            )

            self.stdout.write(str(summary))

        except Exception as exc:
            raise CommandError(str(exc))