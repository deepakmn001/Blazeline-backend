from django.core.management.base import BaseCommand
from django.utils.text import slugify

from catalog.models import Category


CATEGORIES = [
    {
        "name": "Building Materials",
        "group": "Structure",
        "description": "Cement, steel, bricks and aggregate.",
    },
    {
        "name": "Plywood & Boards",
        "group": "Structure",
        "description": "Commercial, marine and decorative boards.",
    },
    {
        "name": "Paints & Finishes",
        "group": "Finishes",
        "description": "Interior, exterior paints and waterproofing.",
    },
    {
        "name": "Electricals",
        "group": "Electrical",
        "description": "Wires, switches, MCBs and accessories.",
    },
    {
        "name": "Lighting",
        "group": "Lighting",
        "description": "Indoor and outdoor lighting solutions.",
    },
    {
        "name": "Plumbing",
        "group": "Plumbing",
        "description": "CPVC pipes, fittings and drainage.",
    },
    {
        "name": "Sanitary & Bath Fittings",
        "group": "Bathroom",
        "description": "Basins, faucets, showers and toilets.",
    },
    {
        "name": "Hardware & Fittings",
        "group": "Hardware",
        "description": "Locks, hinges, handles and fasteners.",
    },
    {
        "name": "Adhesives & Chemicals",
        "group": "Finishes",
        "description": "Tile adhesives, epoxy and sealants.",
    },
    {
        "name": "Ceiling & Roofing",
        "group": "Ceiling",
        "description": "False ceiling and roofing materials.",
    },
]


class Command(BaseCommand):
    help = "Seed default BlazeLine categories"

    def handle(self, *args, **options):
        created = 0

        for item in CATEGORIES:
            obj, was_created = Category.objects.get_or_create(
                slug=slugify(item["name"]),
                defaults={
                    "name": item["name"],
                    "group": item["group"],
                    "description": item["description"],
                    "featured": True,
                    "active": True,
                },
            )

            if was_created:
                created += 1
                self.stdout.write(
                    self.style.SUCCESS(f"✓ Created {obj.name}")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"• Exists {obj.name}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {created} categories created."
            )
        )