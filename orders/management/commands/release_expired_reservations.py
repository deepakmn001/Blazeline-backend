from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from orders.services import (
    release_expired_inventory_reservations,
)


class Command(BaseCommand):
    help = (
        "Release expired ACTIVE inventory reservations "
        "and return their quantities to inventory."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Maximum number of expired reservations to process.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]

        if batch_size < 1:
            raise CommandError(
                "--batch-size must be greater than zero."
            )

        released = (
            release_expired_inventory_reservations(
                batch_size=batch_size,
            )
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Released {released} expired inventory reservation(s)."
            )
        )