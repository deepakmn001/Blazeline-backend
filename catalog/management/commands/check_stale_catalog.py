from django.core.management.base import BaseCommand
from catalog.models import Category, SubCategory

try:
    from catalog.models import Product
except ImportError:
    Product = None


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
        "Read-only report: lists stale Category / SubCategory rows that "
        "don't match categories.ts, and how many Products point to each "
        "one. Does NOT delete or modify anything."
    )

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("=== Stale Sub Categories ==="))

        stale_subs = SubCategory.objects.exclude(slug__in=VALID_SUBCATEGORY_SLUGS)

        if not stale_subs.exists():
            self.stdout.write(self.style.SUCCESS("None found. Nothing to clean up here."))
        else:
            for sub in stale_subs:
                product_count = self._product_count(subcategory=sub)
                self.stdout.write(
                    f"  - [{sub.category.name}] '{sub.name}' "
                    f"(slug: {sub.slug}) -> {product_count} product(s) linked"
                )

        self.stdout.write("")
        self.stdout.write(self.style.WARNING("=== Stale Categories ==="))

        stale_cats = Category.objects.exclude(slug__in=VALID_CATEGORY_SLUGS)

        if not stale_cats.exists():
            self.stdout.write(self.style.SUCCESS("None found. Nothing to clean up here."))
        else:
            for cat in stale_cats:
                product_count = self._product_count(category=cat)
                self.stdout.write(
                    f"  - '{cat.name}' (slug: {cat.slug}) -> "
                    f"{product_count} product(s) linked"
                )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Check complete. No data was changed."))

    def _product_count(self, category=None, subcategory=None):
        if Product is None:
            return "unknown (Product model not importable)"

        qs = Product.objects.all()
        if subcategory is not None:
            qs = qs.filter(subcategory=subcategory)
        if category is not None:
            qs = qs.filter(category=category)
        return qs.count()