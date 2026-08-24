from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Product


# ---------------------------------------------------------------------------
# HISTORICAL BRAND MAPPING
#
# IMPORTANT:
# These mappings are for the ONE-TIME historical backfill only.
#
# Brand is now stored permanently on Product.brand and is independent of
# category/subcategory. Future category/subcategory moves will NOT change it.
# ---------------------------------------------------------------------------

SUBCATEGORY_BRAND_MAP: dict[str, str] = {
    "Bricks & Blocks": "ADVANCE",
    "Kitchen Sinks": "VANTAGE",
    "Roofing Sheets": "Blinco",
    "Wires & Cables": "Finolex",
    "Electrical Accessories": "Finolex",
    "Motors, Fans & Pumps": "Finolex",
    "Water Heaters": "Finolex",
    "Center Tables": "IKON",
    "Bedroom Set": "IKON",
    "Chairs": "IKON",
    "Dining Set": "IKON",
    "Door Handle & Locks": "Häfele",
}


# ---------------------------------------------------------------------------
# EXPECTED COUNTS CONFIRMED FROM THE LIVE CATALOG
# ---------------------------------------------------------------------------

EXPECTED_SUBCATEGORY_COUNTS: dict[str, int] = {
    "Bricks & Blocks": 14,
    "Kitchen Sinks": 106,
    "Roofing Sheets": 142,
    "Wires & Cables": 19,
    "Electrical Accessories": 6,
    "Motors, Fans & Pumps": 26,
    "Water Heaters": 8,
    "Center Tables": 40,
    "Bedroom Set": 28,
    "Chairs": 49,
    "Dining Set": 24,
    "Door Handle & Locks": 8,
}

EXPECTED_TOTAL_PRODUCTS = 470


class Command(BaseCommand):
    help = (
        "Safely backfill Product.brand for the historically confirmed "
        "470-product catalog. Dry-run by default; use --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the confirmed Product.brand values.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])

        products = list(
            Product.objects
            .select_related("category", "subcategory")
            .order_by("id")
        )

        total_products = len(products)

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING("Historical Product Brand Backfill")
        )
        self.stdout.write("=" * 78)
        self.stdout.write(
            f"Products found in database : {total_products}"
        )
        self.stdout.write(
            f"Expected confirmed products : {EXPECTED_TOTAL_PRODUCTS}"
        )
        self.stdout.write("")

        # ------------------------------------------------------------------
        # HARD SAFETY CHECK
        # ------------------------------------------------------------------

        if total_products != EXPECTED_TOTAL_PRODUCTS:
            self.stdout.write(
                self.style.ERROR(
                    "ABORTED: database product count does not match the "
                    "expected 470 confirmed products."
                )
            )
            self.stdout.write(
                "No records were modified."
            )
            return

        # ------------------------------------------------------------------
        # CURRENT SUBCATEGORY COUNTS
        # ------------------------------------------------------------------

        current_counts = Counter(
            product.subcategory.name
            for product in products
        )

        self.stdout.write(
            self.style.MIGRATE_HEADING("Subcategory Count Verification")
        )
        self.stdout.write("-" * 78)

        count_mismatches: list[tuple[str, int, int]] = []

        for subcategory, expected_count in EXPECTED_SUBCATEGORY_COUNTS.items():
            actual_count = current_counts.get(subcategory, 0)

            status = "OK" if actual_count == expected_count else "MISMATCH"

            if status == "OK":
                self.stdout.write(
                    f"{subcategory:<28} "
                    f"{actual_count:>4} / {expected_count:<4}  OK"
                )
            else:
                count_mismatches.append(
                    (subcategory, actual_count, expected_count)
                )
                self.stdout.write(
                    f"{subcategory:<28} "
                    f"{actual_count:>4} / {expected_count:<4}  MISMATCH"
                )

        self.stdout.write("")

        # Any extra/unmapped subcategory means our historical mapping is
        # incomplete or catalog placement changed. Never guess.
        mapped_subcategories = set(SUBCATEGORY_BRAND_MAP)
        unexpected_subcategories = sorted(
            name
            for name in current_counts
            if name not in mapped_subcategories
        )

        if unexpected_subcategories:
            self.stdout.write(
                self.style.WARNING(
                    "Unmapped subcategories currently present:"
                )
            )

            for name in unexpected_subcategories:
                self.stdout.write(
                    f"  - {name}: {current_counts[name]} product(s)"
                )

            self.stdout.write("")

        if count_mismatches:
            self.stdout.write(
                self.style.ERROR(
                    "ABORTED: one or more confirmed subcategory counts "
                    "do not match the expected historical counts."
                )
            )
            self.stdout.write(
                "No records were modified."
            )
            return

        if unexpected_subcategories:
            self.stdout.write(
                self.style.ERROR(
                    "ABORTED: unmapped subcategories were found."
                )
            )
            self.stdout.write(
                "No records were modified."
            )
            return

        # ------------------------------------------------------------------
        # BUILD UPDATE PLAN
        # ------------------------------------------------------------------

        update_plan: list[Product] = []
        already_branded = 0

        brand_counts = Counter()
        skipped_products: list[Product] = []

        for product in products:
            subcategory_name = product.subcategory.name
            target_brand = SUBCATEGORY_BRAND_MAP.get(subcategory_name)

            if not target_brand:
                skipped_products.append(product)
                continue

            current_brand = (product.brand or "").strip()

            # Never overwrite an existing product-level brand.
            if current_brand:
                already_branded += 1
                brand_counts[current_brand] += 1
                continue

            product.brand = target_brand
            update_plan.append(product)
            brand_counts[target_brand] += 1

        # ------------------------------------------------------------------
        # REPORT
        # ------------------------------------------------------------------

        self.stdout.write(
            self.style.MIGRATE_HEADING("Planned Brand Assignment")
        )
        self.stdout.write("-" * 78)

        for subcategory, brand in SUBCATEGORY_BRAND_MAP.items():
            count = EXPECTED_SUBCATEGORY_COUNTS[subcategory]
            self.stdout.write(
                f"{subcategory:<28} "
                f"→ {brand:<20} "
                f"{count:>4} product(s)"
            )

        self.stdout.write("")
        self.stdout.write(
            f"Already branded : {already_branded}"
        )
        self.stdout.write(
            f"To be updated   : {len(update_plan)}"
        )
        self.stdout.write(
            f"Skipped         : {len(skipped_products)}"
        )
        self.stdout.write("")

        # ------------------------------------------------------------------
        # FINAL SAFETY CHECK
        # ------------------------------------------------------------------

        expected_updates = EXPECTED_TOTAL_PRODUCTS - already_branded

        if len(update_plan) != expected_updates:
            self.stdout.write(
                self.style.ERROR(
                    "ABORTED: planned update count does not match the "
                    "expected number of unbranded products."
                )
            )
            self.stdout.write(
                f"Expected updates : {expected_updates}"
            )
            self.stdout.write(
                f"Actual updates   : {len(update_plan)}"
            )
            self.stdout.write(
                "No records were modified."
            )
            return

        # ------------------------------------------------------------------
        # DRY RUN
        # ------------------------------------------------------------------

        if not apply_changes:
            self.stdout.write(
                self.style.SUCCESS(
                    "DRY RUN ONLY: no database records were modified."
                )
            )
            self.stdout.write(
                "All 470 confirmed products passed the safety checks."
            )
            self.stdout.write(
                "Run with --apply to perform the one-time backfill."
            )
            return

        # ------------------------------------------------------------------
        # APPLY — ATOMIC
        # ------------------------------------------------------------------

        with transaction.atomic():
            Product.objects.bulk_update(
                update_plan,
                ["brand", "updated_at"],
                batch_size=500,
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Brand backfill completed successfully. "
                f"Updated: {len(update_plan)}"
            )
        )