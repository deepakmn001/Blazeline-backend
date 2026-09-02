from django.core.exceptions import ValidationError

from .models import Cart


def get_or_create_guest_cart(guest_id):
    cart, _ = Cart.objects.get_or_create(guest_id=guest_id, customer__isnull=True)
    return cart


def get_or_create_customer_cart(customer):
    cart, _ = Cart.objects.get_or_create(customer=customer)
    return cart


def merge_guest_cart_into_customer(guest_id, customer):
    """
    Called right after OTP login. Folds items from the anonymous guest
    cart into the customer's persistent cart, summing quantities on
    collision, then deletes the now-empty guest cart.
    """
    try:
        guest_cart = Cart.objects.get(guest_id=guest_id, customer__isnull=True)
    except (Cart.DoesNotExist, ValueError, ValidationError):
        return

    customer_cart, _ = Cart.objects.get_or_create(customer=customer)

    for item in guest_cart.items.all():
        existing = customer_cart.items.filter(variant=item.variant).first()
        if existing:
            existing.quantity += item.quantity
            existing.save(update_fields=["quantity"])
        else:
            item.cart = customer_cart
            item.save(update_fields=["cart"])

    guest_cart.delete()