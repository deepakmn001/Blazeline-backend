from django.core.management.base import BaseCommand
from catalog.models import Category, SubCategory

try:
    from catalog.models import Product
except ImportError:
    Product = None


# The exact canonical slugs from categories.ts. Anything in the DB but
# NOT in these sets is stale leftover data from the old seed_catalog.py
# and will be deleted by this command.
VALID_CATEGORY_SLUGS = {
    "building-materials",
    "plywood-boards",
    "paints-finishes",
    "electricals",
    "lighting",
    "plumbing",
    "sanitary-bath-fittings",
    "hardware-fittings",
    "adhesives-chemicals",
    "ceiling-roofing",
}

VALID_SUBCATEGORY_SLUGS = {
    "ppc-cement", "tmt-steel-bar", "bricks-blocks", "sands-aggregate",
    "gi-sheets", "gi-coils",
    "plywood-boards",
    "interior-emulsions", "exterior-emulsions", "coatings-varnish",
    "wood-metal-polish-paints", "painting-accessories-supplies",
    "waterproofing-crack-fillers",
    "wires-cables", "switches-sockets", "mcbs-dbs",
    "electrical-conduits-fittings", "electrical-accessories",
    "motors-fans-pumps",
    "led-strips-profile-lights", "outdoor-lighting", "light-accessories",
    "cpvc-pipes-fittings", "drainage-plumbing-accessories",
    "wash-basins-faucets", "showers-bath-fixtures", "wc-toilet-fixtures",
    "kitchen-sinks",
    "cabinet-drawer-hinges", "door-handle-locks", "screws-fasteners",
    "kitchen-hardware", "tools-blades",
    "epoxy-adhesive", "tile-adhesives", "wood-adhesives", "sealants",
    "gypsum-pop", "gypsum-pop-false-ceilings",
    "ceiling-tiles-decorative-panels", "false-ceiling-channels",
    "gi-channel-accessories", "roofing-sheets",
}


class Command(BaseCommand):
    help = (
        "Deletes stale Category / SubCategory rows that don't match "
        "categories.ts. Refuses to delete any row that still has "
        "products linked to it, to prevent accidental data loss."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Actually perform the deletion. Without this flag, "
                 "the command only shows what WOULD be deleted (dry run).",
        )

    def handle(self, *args, **options):
        confirmed = options["yes"]

        stale_subs = list(
            SubCategory.objects.exclude(slug__in=VALID_SUBCATEGORY_SLUGS)
        )
        stale_cats = list(
            Category.objects.exclude(slug__in=VALID_CATEGORY_SLUGS)
        )

        deletable_subs, blocked_subs = self._split_by_product_usage(
            stale_subs, is_subcategory=True
        )
        deletable_cats, blocked_cats = self._split_by_product_usage(
            stale_cats, is_subcategory=False
        )

        self._report("Sub Categories", deletable_subs, blocked_subs)
        self._report("Categories", deletable_cats, blocked_cats)

        if blocked_subs or blocked_cats:
            self.stdout.write("")
            self.stdout.write(
                self.style.ERROR(
                    "Some stale rows still have products linked and were "
                    "SKIPPED. Reassign those products first, then re-run."
                )
            )

        if not confirmed:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "Dry run only — nothing was deleted. "
                    "Re-run with --yes to actually delete the safe rows above."
                )
            )
            return

        deleted_subs = 0
        for sub in deletable_subs:
            sub.delete()
            deleted_subs += 1

        deleted_cats = 0
        for cat in deletable_cats:
            cat.delete()
            deleted_cats += 1

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_subs} sub-categor(y/ies) and "
                f"{deleted_cats} categor(y/ies)."
            )
        )

    def _split_by_product_usage(self, rows, is_subcategory):
        deletable = []
        blocked = []

        for row in rows:
            count = self._product_count(
                subcategory=row if is_subcategory else None,
                category=None if is_subcategory else row,
            )
            if isinstance(count, int) and count > 0:
                blocked.append((row, count))
            else:
                deletable.append(row)

        return deletable, blocked

    def _product_count(self, category=None, subcategory=None):
        if Product is None:
            return "unknown"

        qs = Product.objects.all()
        if subcategory is not None:
            qs = qs.filter(subcategory=subcategory)
        if category is not None:
            qs = qs.filter(category=category)
        return qs.count()

    def _report(self, label, deletable, blocked):
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"=== {label}: safe to delete ==="))
        if not deletable:
            self.stdout.write("  (none)")
        for row in deletable:
            self.stdout.write(f"  - {row.name} (slug: {row.slug})")

        if blocked:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(f"=== {label}: BLOCKED (has products) ==="))
            for row, count in blocked:
                self.stdout.write(f"  - {row.name} (slug: {row.slug}) -> {count} product(s)")