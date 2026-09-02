from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Cart


def get_or_create_guest_cart(guest_id):
    cart, _ = Cart.objects.get_or_create(
        guest_id=guest_id,
        customer__isnull=True,
    )
    return cart


def get_or_create_customer_cart(customer):
    """
    Returns the customer's persistent cart.

    Customer has a OneToOne relationship with Cart, so Django's
    get_or_create() is protected by the database uniqueness constraint.
    """
    cart, _ = Cart.objects.get_or_create(
        customer=customer,
    )
    return cart


@transaction.atomic
def merge_guest_cart_into_customer(guest_id, customer):
    """
    Atomically merge an anonymous guest cart into a customer's
    persistent cart.

    Guarantees:
    - guest cart must still be an anonymous cart
    - customer cart is locked during the merge
    - guest cart and its items are locked
    - same variants have quantities summed
    - different variants are moved to the customer cart
    - guest cart is deleted only after the merge succeeds
    - if anything fails, the entire merge is rolled back
    """

    try:
        # Lock the guest cart first so another merge cannot process
        # the same guest cart concurrently.
        guest_cart = (
            Cart.objects
            .select_for_update()
            .get(
                guest_id=guest_id,
                customer__isnull=True,
            )
        )
    except (Cart.DoesNotExist, ValueError, ValidationError):
        # Nothing to merge.
        return

    # Lock the customer's cart row for the duration of the merge.
    customer_cart, _ = (
        Cart.objects
        .select_for_update()
        .get_or_create(customer=customer)
    )

    # Lock all guest items before reading/modifying them.
    guest_items = list(
        guest_cart.items
        .select_for_update()
        .select_related("variant")
    )

    for guest_item in guest_items:
        # Lock the matching customer item, if it exists.
        existing = (
            customer_cart.items
            .select_for_update()
            .filter(variant=guest_item.variant)
            .first()
        )

        if existing:
            existing.quantity += guest_item.quantity
            existing.save(
                update_fields=["quantity", "updated_at"]
            )
        else:
            guest_item.cart = customer_cart
            guest_item.save(
                update_fields=["cart", "updated_at"]
            )

    # Reaching this point means the whole merge succeeded.
    # Deleting the guest cart also removes any remaining related
    # items through the CartItem foreign key cascade.
    guest_cart.delete()