import django_filters
from .models import Product

class ProductFilter(django_filters.FilterSet):
    category = django_filters.NumberFilter(field_name="category_id")
    subcategory = django_filters.NumberFilter(field_name="subcategory_id")
    min_price = django_filters.NumberFilter(field_name="variants__selling_price", lookup_expr="gte")
    max_price = django_filters.NumberFilter(field_name="variants__selling_price", lookup_expr="lte")

    class Meta:
        model = Product
        fields = ["category", "subcategory", "featured","active", "status"]