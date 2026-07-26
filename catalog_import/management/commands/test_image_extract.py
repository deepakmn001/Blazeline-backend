from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from catalog_import.models import Catalog
from catalog_import.services.parser import parse_catalog
from catalog_import.services.image_extractor import ImageExtractor


class Command(BaseCommand):

    help = "Test product image extraction"

    def add_arguments(self, parser):

        parser.add_argument(
            "--catalog",
            type=int,
            required=True,
        )

        parser.add_argument(
            "--page",
            type=int,
            default=None,
        )

    def handle(self, *args, **options):

        catalog_id = options["catalog"]
        page = options["page"]

        catalog = Catalog.objects.get(pk=catalog_id)

        pdf_path = Path(catalog.pdf.path)

        self.stdout.write("")
        self.stdout.write("=" * 80)
        self.stdout.write("PARSING PDF")
        self.stdout.write("=" * 80)

        parsed_products = parse_catalog(
            pdf_path=pdf_path,
            start_page=page,
            end_page=page,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Found {len(parsed_products)} products"
            )
        )

        output_dir = (
            Path(settings.MEDIA_ROOT)
            / "catalog_products"
        )

        extractor = ImageExtractor()

        images = extractor.extract(
            pdf_path=pdf_path,
            product_regions=parsed_products,
            output_dir=output_dir,
        )

        self.stdout.write("")
        self.stdout.write("=" * 80)
        self.stdout.write("EXTRACTED IMAGES")
        self.stdout.write("=" * 80)

        for item in images:

            self.stdout.write(
                f"{item['sku']}  ->  {item['image_path']}"
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated {len(images)} images."
            )
        )