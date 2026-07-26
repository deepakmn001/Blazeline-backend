from django.core.management.base import BaseCommand
from catalog.models import Category, SubCategory


# =========================================================
# CATEGORIES
# Mirrors categories.ts exactly — name, slug, group,
# description, and featured flag must never diverge from
# the landing website's CATEGORIES array.
# =========================================================
CATEGORIES = [
    {
        "name": "Building Materials",
        "slug": "building-materials",
        "group": "Structure",
        "description": (
            "Cement, steel, bricks and aggregate — the base every build "
            "starts on, sourced from ISI-certified brands and dispatched "
            "in site-ready quantities."
        ),
        "featured": True,
    },
    {
        "name": "Plywood & Boards",
        "slug": "plywood-boards",
        "group": "Structure",
        "description": (
            "Commercial, marine, and moisture-rated boards for furniture, "
            "false ceilings, and structural work — checked before it ships."
        ),
        "featured": True,
    },
    {
        "name": "Paints & Finishes",
        "slug": "paints-finishes",
        "group": "Finishes",
        "description": (
            "Interior and exterior paints, wood finishes, and "
            "waterproofing compounds in the shades and sheens contractors "
            "actually order."
        ),
        "featured": True,
    },
    {
        "name": "Electricals",
        "slug": "electricals",
        "group": "Electrical",
        "description": (
            "ISI-marked wires, switchgear, and distribution boards from "
            "brands electricians already trust — rated and labelled for a "
            "smooth inspection."
        ),
        "featured": True,
    },
    {
        "name": "Lighting",
        "slug": "lighting",
        "group": "Lighting",
        "description": (
            "Functional and decorative lighting for every stage of a "
            "project — practical light for site work, statement fixtures "
            "for the final handover."
        ),
        "featured": True,
    },
    {
        "name": "Plumbing",
        "slug": "plumbing",
        "group": "Plumbing",
        "description": (
            "CPVC piping and drainage systems rated for long-term site "
            "use, stocked in the run lengths and fitting sizes plumbers "
            "actually order."
        ),
        "featured": False,
    },
    {
        "name": "Sanitary & Bath Fittings",
        "slug": "sanitary-bath-fittings",
        "group": "Bathroom",
        "description": (
            "Wash basins, faucets, and complete bathroom fitments from "
            "brands that back their warranty — curated for tight handover "
            "timelines."
        ),
        "featured": True,
    },
    {
        "name": "Hardware & Fittings",
        "slug": "hardware-fittings",
        "group": "Hardware",
        "description": (
            "The fasteners, hinges, and fittings that hold everything "
            "together — the stuff that's a nightmare to source "
            "last-minute."
        ),
        "featured": True,
    },
    {
        "name": "Adhesives & Chemicals",
        "slug": "adhesives-chemicals",
        "group": "Finishes",
        "description": (
            "Tile adhesives, sealants, and bonding solutions rated for "
            "Indian site conditions, stocked in contractor pack sizes."
        ),
        "featured": True,
    },
    {
        "name": "Ceiling & Roofing",
        "slug": "ceiling-roofing",
        "group": "Ceiling",
        "description": (
            "Gypsum and POP false ceiling systems, decorative panels, and "
            "roofing sheets — plus the channel hardware to mount them "
            "properly."
        ),
        "featured": False,
    },
]


# =========================================================
# SUBCATEGORIES
# Each tuple is (name, slug) taken verbatim from the
# `subcategories` array of the matching category in
# categories.ts. Order is preserved exactly.
# =========================================================
SUBCATEGORIES = {
    "building-materials": [
        ("PPC Cement", "ppc-cement"),
        ("TMT Steel Bar", "tmt-steel-bar"),
        ("Bricks & Blocks", "bricks-blocks"),
        ("Sands & Aggregate", "sands-aggregate"),
        ("GI Sheets", "gi-sheets"),
        ("GI Coils", "gi-coils"),
    ],

    "plywood-boards": [
        ("Plywood & Boards", "plywood-boards"),
    ],

    "paints-finishes": [
        ("Interior Emulsions", "interior-emulsions"),
        ("Exterior Emulsions", "exterior-emulsions"),
        ("Coatings & Varnish", "coatings-varnish"),
        ("Wood & Metal Polish Paints", "wood-metal-polish-paints"),
        ("Painting Accessories & Supplies", "painting-accessories-supplies"),
        ("Waterproofing & Crack Fillers", "waterproofing-crack-fillers"),
    ],

    "electricals": [
        ("Wires & Cables", "wires-cables"),
        ("Switches & Sockets", "switches-sockets"),
        ("MCBs & DBs", "mcbs-dbs"),
        ("Electrical Conduits & Fittings", "electrical-conduits-fittings"),
        ("Electrical Accessories", "electrical-accessories"),
        ("Motors, Fans & Pumps", "motors-fans-pumps"),
    ],

    "lighting": [
        ("LED Strips & Profile Lights", "led-strips-profile-lights"),
        ("Outdoor Lighting", "outdoor-lighting"),
        ("Light Accessories", "light-accessories"),
    ],

    "plumbing": [
        ("CPVC Pipes & Fittings", "cpvc-pipes-fittings"),
        ("Drainage & Plumbing Accessories", "drainage-plumbing-accessories"),
    ],

    "sanitary-bath-fittings": [
        ("Wash Basins & Faucets", "wash-basins-faucets"),
        ("Showers & Bath Fixtures", "showers-bath-fixtures"),
        ("WC & Toilet Fixtures", "wc-toilet-fixtures"),
        ("Kitchen Sinks", "kitchen-sinks"),
    ],

    "hardware-fittings": [
        ("Cabinet & Drawer Hinges", "cabinet-drawer-hinges"),
        ("Door Handle & Locks", "door-handle-locks"),
        ("Screws & Fasteners", "screws-fasteners"),
        ("Kitchen Hardware", "kitchen-hardware"),
        ("Tools & Blades", "tools-blades"),
    ],

    "adhesives-chemicals": [
        ("Epoxy Adhesive", "epoxy-adhesive"),
        ("Tile Adhesives", "tile-adhesives"),
        ("Wood Adhesives", "wood-adhesives"),
        ("Sealants", "sealants"),
    ],

    "ceiling-roofing": [
        ("Gypsum & POP", "gypsum-pop"),
        ("Gypsum & POP False Ceilings", "gypsum-pop-false-ceilings"),
        ("Ceiling Tiles & Decorative Panels", "ceiling-tiles-decorative-panels"),
        ("False Ceiling Channels", "false-ceiling-channels"),
        ("GI Channel & Accessories", "gi-channel-accessories"),
        ("Roofing Sheets", "roofing-sheets"),
    ],
}


class Command(BaseCommand):
    help = (
        "Seed BlazeLine Categories & Sub Categories so the backend "
        "database is an exact mirror of the landing website's "
        "categories.ts catalog."
    )

    def handle(self, *args, **options):
        categories_created = self._seed_categories()
        subcategories_created = self._seed_subcategories()

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(f"Categories Created : {categories_created}")
        )
        self.stdout.write(
            self.style.SUCCESS(f"Sub Categories Created : {subcategories_created}")
        )

    def _seed_categories(self) -> int:
        """Create Category rows from CATEGORIES, preserving order."""
        created_count = 0

        for item in CATEGORIES:
            _, was_created = Category.objects.get_or_create(
                slug=item["slug"],
                defaults={
                    "name": item["name"],
                    "group": item["group"],
                    "description": item["description"],
                    "featured": item["featured"],
                    "active": True,
                },
            )

            if was_created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"✓ Category: {item['name']}"))

        return created_count

    def _seed_subcategories(self) -> int:
        """Create SubCategory rows from SUBCATEGORIES, preserving order."""
        created_count = 0

        for category_slug, subcategory_items in SUBCATEGORIES.items():
            category = Category.objects.get(slug=category_slug)

            for name, slug in subcategory_items:
                _, was_created = SubCategory.objects.get_or_create(
                    category=category,
                    slug=slug,
                    defaults={
                        "name": name,
                        "active": True,
                    },
                )

                if was_created:
                    created_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"   ↳ {category.name} → {name}")
                    )

        return created_count