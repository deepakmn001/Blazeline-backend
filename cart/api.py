import uuid

from django.urls import path
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from catalog.models import ProductVariant
from catalog.serializers import ProductVariantSerializer

from . import services
from .models import Cart, CartItem

# ==========================================================
# SERIALIZERS
# ==========================================================


class CartItemSerializer(serializers.ModelSerializer):
    variant = ProductVariantSerializer(read_only=True)
    variant_id = serializers.PrimaryKeyRelatedField(
        source="variant", queryset=ProductVariant.objects.filter(active=True), write_only=True,
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
        fields = ["id", "guest_id", "items", "subtotal", "total_items", "updated_at"]

    def get_subtotal(self, obj):
        return sum((item.variant.selling_price * item.quantity for item in obj.items.all()), 0)

    def get_total_items(self, obj):
        return sum((item.quantity for item in obj.items.all()), 0)


# ==========================================================
# HELPERS
# ==========================================================


def _is_customer(request):
    from accounts.models import Customer
    return isinstance(request.user, Customer)


def resolve_cart(request):
    """
    Returns (cart, guest_id_or_None). Logged-in customers always use
    their persistent Cart. Anonymous requests are identified by an
    X-Guest-Id header (falls back to guest_id in body/query) — minted
    fresh if missing/invalid; the frontend should persist and resend it.
    """
    if _is_customer(request):
        return services.get_or_create_customer_cart(request.user), None

    guest_id = (
        request.headers.get("X-Guest-Id")
        or request.data.get("guest_id")
        or request.query_params.get("guest_id")
    )
    try:
        guest_id = uuid.UUID(str(guest_id))
    except (TypeError, ValueError):
        guest_id = uuid.uuid4()

    return services.get_or_create_guest_cart(guest_id), guest_id


def _serialize(cart, guest_id, request):
    data = CartSerializer(cart).data
    if guest_id and not _is_customer(request):
        data["guest_id"] = str(guest_id)
    return data


# ==========================================================
# VIEWS
# ==========================================================


class CartView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        cart, guest_id = resolve_cart(request)
        return Response(_serialize(cart, guest_id, request))


class CartItemView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        cart, guest_id = resolve_cart(request)

        try:
            quantity = int(request.data.get("quantity", 1))
        except (TypeError, ValueError):
            return Response({"detail": "Quantity must be a whole number."}, status=status.HTTP_400_BAD_REQUEST)
        if quantity < 1:
            return Response({"detail": "Quantity must be at least 1."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            variant = ProductVariant.objects.get(pk=request.data.get("variant_id"), active=True)
        except (ProductVariant.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Variant not found."}, status=status.HTTP_404_NOT_FOUND)

        item, created = CartItem.objects.get_or_create(cart=cart, variant=variant, defaults={"quantity": quantity})
        if not created:
            item.quantity += quantity
            item.save(update_fields=["quantity"])

        return Response(_serialize(cart, guest_id, request), status=status.HTTP_201_CREATED)


class CartItemDetailView(APIView):
    permission_classes = [permissions.AllowAny]

    def patch(self, request, item_id):
        cart, guest_id = resolve_cart(request)
        try:
            item = cart.items.get(pk=item_id)
        except CartItem.DoesNotExist:
            return Response({"detail": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            quantity = int(request.data.get("quantity", item.quantity))
        except (TypeError, ValueError):
            return Response({"detail": "Quantity must be a whole number."}, status=status.HTTP_400_BAD_REQUEST)
        if quantity < 1:
            return Response({"detail": "Quantity must be at least 1."}, status=status.HTTP_400_BAD_REQUEST)

        item.quantity = quantity
        item.save(update_fields=["quantity"])
        return Response(_serialize(cart, guest_id, request))

    def delete(self, request, item_id):
        cart, guest_id = resolve_cart(request)
        try:
            item = cart.items.get(pk=item_id)
        except CartItem.DoesNotExist:
            return Response({"detail": "Item not found."}, status=status.HTTP_404_NOT_FOUND)

        item.delete()
        return Response(_serialize(cart, guest_id, request))


# ==========================================================
# URLS
# ==========================================================

urlpatterns = [
    path("", CartView.as_view(), name="cart-detail"),
    path("items/", CartItemView.as_view(), name="cart-item-add"),
    path("items/<int:item_id>/", CartItemDetailView.as_view(), name="cart-item-detail"),
]