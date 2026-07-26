from django.core.management.base import BaseCommand

from catalog_import.services.parser import parse_catalog
from catalog_import.services.importer import (
    convert_regions_to_parsed_products,
    validate_products,
    summarize_import,
)


class Command(BaseCommand):
    help = "Developer tool to parse and inspect catalog pages."

    def add_arguments(self, parser):
        parser.add_argument(
            "pdf",
            help="Path to PDF file",
        )

        parser.add_argument(
            "--page",
            type=int,
            default=1,
            help="Single page to parse",
        )

        parser.add_argument(
            "--brand",
            default="",
        )

        parser.add_argument(
            "--category",
            default="",
        )

        parser.add_argument(
            "--subcategory",
            default="",
        )

    def handle(self, *args, **options):

        self.stdout.write("")
        self.stdout.write("=" * 80)
        self.stdout.write("BLAZELINE PARSER DEVELOPER MODE")
        self.stdout.write("=" * 80)
        self.stdout.write(f"PDF  : {options['pdf']}")
        self.stdout.write(f"PAGE : {options['page']}")
        self.stdout.write("=" * 80)
        self.stdout.write("")

        # --------------------------------------------------
        # Parse only the requested page
        # --------------------------------------------------

        regions = parse_catalog(
            options["pdf"],
            start_page=options["page"],
            end_page=options["page"],
        )

        # --------------------------------------------------
        # Convert parser output
        # --------------------------------------------------

        products = convert_regions_to_parsed_products(
            regions=regions,
            brand=options["brand"],
            category=options["category"],
            subcategory=options["subcategory"],
        )

        # --------------------------------------------------
        # Validate
        # --------------------------------------------------

        validate_products(products)

        # --------------------------------------------------
        # Summary
        # --------------------------------------------------

        summary = summarize_import(products)

        self.print_summary(summary)
        self.print_products(products)

    def print_summary(self, summary):

        self.stdout.write("")
        self.stdout.write("=" * 50)
        self.stdout.write("IMPORT SUMMARY")
        self.stdout.write("=" * 50)

        self.stdout.write(f"Total Products       : {summary['total']}")
        self.stdout.write(f"Valid Products       : {summary['valid']}")
        self.stdout.write(f"Invalid Products     : {summary['invalid']}")
        self.stdout.write(f"Missing SKU          : {summary['missing_sku']}")
        self.stdout.write(f"Missing Name         : {summary['missing_name']}")
        self.stdout.write(f"Missing Price        : {summary['missing_price']}")

        # Optional summary fields
        if "missing_collection" in summary:
            self.stdout.write(
                f"Missing Collection   : {summary['missing_collection']}"
            )

        if "missing_mb_price" in summary:
            self.stdout.write(
                f"Missing MB Price     : {summary['missing_mb_price']}"
            )

        if "low_confidence_count" in summary:
            self.stdout.write(
                f"Low Confidence       : {summary['low_confidence_count']}"
            )

        self.stdout.write("=" * 50)

    def print_products(self, products):

        for index, product in enumerate(products, start=1):

            self.stdout.write("")
            self.stdout.write("-" * 80)
            self.stdout.write(f"PRODUCT #{index}")
            self.stdout.write("-" * 80)

            self.stdout.write(f"SKU            : {product.sku}")
            self.stdout.write(f"NAME           : {product.name}")
            self.stdout.write(f"GD PRICE       : {product.gd_price}")
            self.stdout.write(f"RGD PRICE      : {product.rgd_price}")

            if hasattr(product, "mb_price"):
                self.stdout.write(f"MB PRICE       : {product.mb_price}")

            self.stdout.write(f"FINISH         : {product.finish}")

            if hasattr(product, "variant"):
                self.stdout.write(f"VARIANT        : {product.variant}")

            if hasattr(product, "collection"):
                self.stdout.write(f"COLLECTION     : {product.collection}")

            if hasattr(product, "series"):
                self.stdout.write(f"SERIES         : {product.series}")

            if hasattr(product, "ai_confidence"):
                self.stdout.write(f"AI CONFIDENCE  : {product.ai_confidence}")

            self.stdout.write(f"STATUS         : {product.status}")
            self.stdout.write(f"ERROR          : {product.error_message}")

        self.stdout.write("")
        self.stdout.write("=" * 80)
        self.stdout.write("PARSER TEST COMPLETED")
        self.stdout.write("=" * 80)