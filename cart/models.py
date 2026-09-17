import uuid

from django.db import models

from accounts.models import Customer
from catalog.models import ProductVariant


class Cart(models.Model):

    customer = models.OneToOneField(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="cart",
    )

    guest_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cart"
        verbose_name_plural = "Carts"

    def __str__(self):
        return f"Cart({self.customer or self.guest_id})"


class CartItem(models.Model):

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name="cart_items")
    quantity = models.PositiveIntegerField(default=1)

    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Cart Item"
        verbose_name_plural = "Cart Items"
        constraints = [
            models.UniqueConstraint(fields=["cart", "variant"], name="uniq_cart_variant"),
        ]

    def __str__(self):
        return f"{self.cart} · {self.variant.sku} x{self.quantity}"
class CartItemIdempotencyKey(models.Model):
    """
    Replay protection for POST /cart/items/.

    The same Idempotency-Key for the same cart and same request
    will not increment the cart item twice.
    """

    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name="idempotency_keys",
    )

    key = models.CharField(max_length=128)

    # SHA-256 fingerprint of the original request payload.
    # Prevents reusing the same key with a different request.
    request_fingerprint = models.CharField(max_length=64)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Cart Item Idempotency Key"
        verbose_name_plural = "Cart Item Idempotency Keys"

        constraints = [
            models.UniqueConstraint(
                fields=["cart", "key"],
                name="uniq_cart_idempotency_key",
            ),
        ]

        indexes = [
            models.Index(
                fields=["created_at"],
                name="cart_idem_created_idx",
            ),
        ]

    def __str__(self):
        return f"{self.cart} · {self.key}"    