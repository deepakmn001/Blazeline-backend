from django.core.management.base import BaseCommand

from catalog.models import Category, HomepageCategory


DEFAULT_HOMEPAGE_CATEGORIES = [
    ("building-materials", 1),
    ("plywood-boards", 2),
    ("electricals", 3),
    ("lighting", 4),
    ("sanitary-bath-fittings", 5),
    ("hardware-fittings", 6),
    ("paints-finishes", 7),
    ("adhesives-chemicals", 8),
]


class Command(BaseCommand):
    help = "Seed the default BlazeLine homepage categories."

    def handle(self, *args, **options):
        created_count = 0
        updated_count = 0
        missing_count = 0

        for slug, sort_order in DEFAULT_HOMEPAGE_CATEGORIES:
            try:
                category = Category.objects.get(slug=slug)
            except Category.DoesNotExist:
                self.stdout.write(
                    self.style.WARNING(
                        f"Category not found: {slug}"
                    )
                )
                missing_count += 1
                continue

            homepage_category, created = (
                HomepageCategory.objects.get_or_create(
                    category=category,
                    defaults={
                        "is_active": True,
                        "sort_order": sort_order,
                    },
                )
            )

            if created:
                created_count += 1

                self.stdout.write(
                    self.style.SUCCESS(
                        f"Added: {category.name} → position {sort_order}"
                    )
                )

            else:
                changed = False

                if homepage_category.sort_order != sort_order:
                    homepage_category.sort_order = sort_order
                    changed = True

                if not homepage_category.is_active:
                    homepage_category.is_active = True
                    changed = True

                if changed:
                    homepage_category.save(
                        update_fields=[
                            "sort_order",
                            "is_active",
                        ]
                    )
                    updated_count += 1

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Updated: {category.name} → position {sort_order}"
                        )
                    )
                else:
                    self.stdout.write(
                        f"Already configured: {category.name}"
                    )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Created: {created_count}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Updated: {updated_count}"
            )
        )
        self.stdout.write(
            self.style.WARNING(
                f"Missing categories: {missing_count}"
            )
        )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "Homepage category seed completed."
            )
        )