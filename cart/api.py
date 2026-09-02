import uuid

from django.db import transaction
from django.urls import path
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from catalog.models import ProductVariant
from catalog.serializers import ProductVariantSerializer

from . import services
from .models import Cart, CartItem


# ==========================================================
# CONSTANTS
# ==========================================================

# Safety ceiling against accidental or abusive huge quantities.
# Change this only if BlazeLine's business rules require another limit.
MAX_CART_ITEM_QUANTITY = 999


# ==========================================================
# SERIALIZERS
# ==========================================================


class CartItemSerializer(serializers.ModelSerializer):
    variant = ProductVariantSerializer(read_only=True)

    variant_id = serializers.PrimaryKeyRelatedField(
        source="variant",
        queryset=ProductVariant.objects.filter(active=True),
        write_only=True,
    )

    line_total = serializers.SerializerMethodField()
    product_name = serializers.SerializerMethodField()
    product_slug = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = [
            "id",
            "variant",
            "variant_id",
            "quantity",
            "line_total",
            "product_name",
            "product_slug",
        ]

    def get_line_total(self, obj):
        return obj.variant.selling_price * obj.quantity

    def get_product_name(self, obj):
        return obj.variant.product.name

    def get_product_slug(self, obj):
        return obj.variant.product.slug


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    guest_id = serializers.UUIDField(read_only=True)
    subtotal = serializers.SerializerMethodField()
    total_items = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = [
            "id",
            "guest_id",
            "items",
            "subtotal",
            "total_items",
            "updated_at",
        ]

    def get_subtotal(self, obj):
        return sum(
            (
                item.variant.selling_price * item.quantity
                for item in obj.items.all()
            ),
            0,
        )

    def get_total_items(self, obj):
        return sum(
            (item.quantity for item in obj.items.all()),
            0,
        )


# ==========================================================
# HELPERS
# ==========================================================


def _is_customer(request):
    from accounts.models import Customer

    return isinstance(request.user, Customer)


def _parse_quantity(value, default=1):
    """
    Strict positive integer quantity validation.
    Rejects booleans, zero, negatives and excessively large values.
    """
    if value is None:
        value = default

    if isinstance(value, bool):
        raise ValueError

    try:
        quantity = int(value)
    except (TypeError, ValueError):
        raise ValueError

    if quantity < 1:
        raise ValueError

    if quantity > MAX_CART_ITEM_QUANTITY:
        raise OverflowError

    return quantity


def _serialize(cart, guest_id, request):
    data = CartSerializer(cart).data

    if guest_id and not _is_customer(request):
        data["guest_id"] = str(guest_id)

    return data


def resolve_cart(request):
    """
    Resolve exactly one cart scope.

    Authenticated customers:
        customer -> persistent customer cart

    Anonymous users:
        X-Guest-Id -> guest cart

    A malformed/missing guest ID gets a fresh UUID.
    """

    if _is_customer(request):
        return (
            services.get_or_create_customer_cart(request.user),
            None,
        )

    raw_guest_id = (
        request.headers.get("X-Guest-Id")
        or request.data.get("guest_id")
        or request.query_params.get("guest_id")
    )

    try:
        guest_id = uuid.UUID(str(raw_guest_id))
    except (TypeError, ValueError, AttributeError):
        guest_id = uuid.uuid4()

    return (
        services.get_or_create_guest_cart(guest_id),
        guest_id,
    )


def _lock_cart(cart):
    """
    Re-read and lock the cart row inside the current transaction.

    The cart object returned by resolve_cart() may have been read before
    another request changed it, so mutations always lock a fresh row.
    """
    return (
        Cart.objects
        .select_for_update()
        .get(pk=cart.pk)
    )


# ==========================================================
# VIEWS
# ==========================================================


class CartView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        cart, guest_id = resolve_cart(request)

        return Response(
            _serialize(
                cart,
                guest_id,
                request,
            )
        )


class CartItemView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        cart, guest_id = resolve_cart(request)

        # --------------------------
        # Validate quantity
        # --------------------------

        try:
            quantity = _parse_quantity(
                request.data.get("quantity", 1)
            )
        except OverflowError:
            return Response(
                {
                    "detail": (
                        f"Quantity cannot exceed "
                        f"{MAX_CART_ITEM_QUANTITY}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError:
            return Response(
                {
                    "detail": (
                        "Quantity must be a positive "
                        "whole number."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --------------------------
        # Validate variant
        # --------------------------

        variant_id = request.data.get("variant_id")

        try:
            variant = (
                ProductVariant.objects
                .select_related("product")
                .get(
                    pk=variant_id,
                    active=True,
                )
            )
        except (
            ProductVariant.DoesNotExist,
            ValueError,
            TypeError,
        ):
            return Response(
                {"detail": "Variant not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # --------------------------
        # Atomic mutation
        # --------------------------

        with transaction.atomic():
            locked_cart = _lock_cart(cart)

            # Lock the existing cart item if present.
            item = (
                CartItem.objects
                .select_for_update()
                .filter(
                    cart=locked_cart,
                    variant=variant,
                )
                .first()
            )

            if item:
                new_quantity = (
                    item.quantity + quantity
                )

                if new_quantity > MAX_CART_ITEM_QUANTITY:
                    return Response(
                        {
                            "detail": (
                                f"Quantity cannot exceed "
                                f"{MAX_CART_ITEM_QUANTITY} "
                                f"for a single item."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                item.quantity = new_quantity
                item.save(
                    update_fields=[
                        "quantity",
                        "updated_at",
                    ]
                )
            else:
                CartItem.objects.create(
                    cart=locked_cart,
                    variant=variant,
                    quantity=quantity,
                )

            # Reload a canonical cart representation after mutation.
            locked_cart = (
                Cart.objects
                .prefetch_related(
                    "items__variant__product",
                )
                .get(pk=locked_cart.pk)
            )

        return Response(
            _serialize(
                locked_cart,
                guest_id,
                request,
            ),
            status=status.HTTP_201_CREATED,
        )


class CartItemDetailView(APIView):
    permission_classes = [permissions.AllowAny]

    def patch(self, request, item_id):
        cart, guest_id = resolve_cart(request)

        # --------------------------
        # Validate quantity
        # --------------------------

        try:
            quantity = _parse_quantity(
                request.data.get(
                    "quantity",
                    1,
                )
            )
        except OverflowError:
            return Response(
                {
                    "detail": (
                        f"Quantity cannot exceed "
                        f"{MAX_CART_ITEM_QUANTITY}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError:
            return Response(
                {
                    "detail": (
                        "Quantity must be a positive "
                        "whole number."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            locked_cart = _lock_cart(cart)

            try:
                item = (
                    CartItem.objects
                    .select_for_update()
                    .select_related(
                        "variant",
                        "variant__product",
                    )
                    .get(
                        pk=item_id,
                        cart=locked_cart,
                    )
                )
            except CartItem.DoesNotExist:
                return Response(
                    {"detail": "Item not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # The cart item itself belongs to the locked cart.
            item.quantity = quantity

            item.save(
                update_fields=[
                    "quantity",
                    "updated_at",
                ]
            )

            locked_cart = (
                Cart.objects
                .prefetch_related(
                    "items__variant__product",
                )
                .get(pk=locked_cart.pk)
            )

        return Response(
            _serialize(
                locked_cart,
                guest_id,
                request,
            )
        )

    def delete(self, request, item_id):
        cart, guest_id = resolve_cart(request)

        with transaction.atomic():
            locked_cart = _lock_cart(cart)

            try:
                item = (
                    CartItem.objects
                    .select_for_update()
                    .get(
                        pk=item_id,
                        cart=locked_cart,
                    )
                )
            except CartItem.DoesNotExist:
                return Response(
                    {"detail": "Item not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            item.delete()

            locked_cart = (
                Cart.objects
                .prefetch_related(
                    "items__variant__product",
                )
                .get(pk=locked_cart.pk)
            )

        return Response(
            _serialize(
                locked_cart,
                guest_id,
                request,
            )
        )


# ==========================================================
# URLS
# ==========================================================

urlpatterns = [
    path(
        "",
        CartView.as_view(),
        name="cart-detail",
    ),
    path(
        "items/",
        CartItemView.as_view(),
        name="cart-item-add",
    ),
    path(
        "items/<int:item_id>/",
        CartItemDetailView.as_view(),
        name="cart-item-detail",
    ),
]