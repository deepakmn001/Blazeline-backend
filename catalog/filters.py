"""
Product catalog filtering.

Backward compatible with the original ProductFilter — category, subcategory,
min_price, max_price, featured, active, status all behave exactly as before.

Two additions:
  1. in_stock=true|false        — availability filter via EXISTS subquery.
  2. option_<key>=<value>       — dynamic catalog-option filtering, driven
                                   entirely by the existing ProductOption /
                                   ProductOptionValue / ProductVariantOption
                                   data. No option names are hardcoded.

Semantics:
  - Multiple values within the SAME option_<key> are OR'd
        ?option_size=A&option_size=B  ->  size = A OR size = B
  - Different option_<key> groups are AND'd
        ?option_collection=Flute&option_size=A
            -> collection = Flute AND size = A
  - A well-formed option_<key> whose key does not resolve to any
    ProductOption within the ALREADY-FILTERED scope (category, subcategory,
    price, etc.) makes the constraint unsatisfiable -> zero results, not a
    silently-ignored no-op. Only genuinely malformed params (empty key,
    empty value list) are dropped.

Scoping (important for scale):
  The option_<key> -> ProductOption.name resolution is built from
  ProductOption rows belonging to products in the CURRENT filtered
  queryset — i.e. AFTER category/subcategory/price/etc. have already run
  inside filter_queryset(), not the entire catalog. A request scoped to
  one subcategory never scans option names from unrelated subcategories.
  This also means min_price/max_price narrow the scope the same way:
  option_collection=Flute combined with a min_price that excludes every
  Flute-priced product will correctly resolve to zero results — this is
  intentional "already filtered scope" semantics, not a bug, and the
  facets endpoint should be built with that in mind.

This module also defines DeliveryRuleFilter (see below), which filters
DeliveryRule by its two derived, non-DB-column concepts (status, scope)
using status_query()/scope_query() from delivery/rule_scope.py — Q objects
that push that logic down to SQL rather than looping over rows in Python.
"""
from __future__ import annotations

import re
from typing import Dict, List

import django_filters
from django.db.models import Exists, OuterRef, QuerySet

from .models import Product, ProductOption, ProductVariant, DeliveryRule
from .delivery.rule_scope import status_query, scope_query

# Only params with this prefix are treated as dynamic catalog-option filters.
OPTION_PARAM_PREFIX = "option_"


def _normalize_option_key(raw_name: str) -> str:
    """Normalize an option name for matching against option_<key> params.

    Never mutates stored data — this only normalizes in memory for lookup.

        "Air Delivery"    -> "air_delivery"
        " Finish "        -> "finish"
        "Conductor  Area" -> "conductor_area"
    """
    collapsed = re.sub(r"\s+", " ", raw_name.strip())
    return collapsed.lower().replace(" ", "_")


class ProductFilter(django_filters.FilterSet):
    category = django_filters.NumberFilter(field_name="category_id")
    subcategory = django_filters.NumberFilter(field_name="subcategory_id")
    min_price = django_filters.NumberFilter(
        field_name="variants__selling_price", lookup_expr="gte"
    )
    max_price = django_filters.NumberFilter(
        field_name="variants__selling_price", lookup_expr="lte"
    )
    in_stock = django_filters.BooleanFilter(method="filter_in_stock")

    class Meta:
        model = Product
        fields = ["category", "subcategory", "featured", "active", "status"]

    # -------------------------------------------------------------------
    # in_stock
    # -------------------------------------------------------------------
    def filter_in_stock(
        self, queryset: QuerySet, name: str, value: bool
    ) -> QuerySet:
        """EXISTS-based availability filter — no join multiplication.

        in_stock=true  -> product has >=1 variant that is active AND stock>0
        in_stock=false -> product has NO variant that is active AND stock>0
        """
        has_stock_variant = ProductVariant.objects.filter(
            product_id=OuterRef("pk"),
            active=True,
            stock__gt=0,
        )
        if value:
            return queryset.filter(Exists(has_stock_variant))
        return queryset.filter(~Exists(has_stock_variant))

    # -------------------------------------------------------------------
    # dynamic option_<key> filters
    # -------------------------------------------------------------------
    def _parse_dynamic_option_params(self) -> Dict[str, List[str]]:
        """Collect and validate option_<key> params from the raw request.

        Genuinely malformed input (empty key, no usable values) is dropped
        silently here — this must never raise on a customer-supplied query
        string. This is the ONLY place malformed params are dropped; a
        well-formed key that later fails to resolve to a real option is a
        separate case, handled in _apply_dynamic_option_filters as an
        unsatisfiable constraint, not a no-op.
        """
        if self.request is None:
            return {}

        parsed: Dict[str, List[str]] = {}
        for raw_param in self.request.query_params.keys():
            if not raw_param.startswith(OPTION_PARAM_PREFIX):
                continue

            key = raw_param[len(OPTION_PARAM_PREFIX):].strip()
            if not key:
                continue

            values = [
                v.strip()
                for v in self.request.query_params.getlist(raw_param)
                if v and v.strip()
            ]
            if not values:
                continue

            parsed[_normalize_option_key(key)] = values
        return parsed

    def _build_option_name_lookup(self, scope_qs: QuerySet) -> Dict[str, List[str]]:
        """normalized_key -> [actual ProductOption.name, ...]

        Scoped to `scope_qs` (the queryset AFTER category/subcategory/price/
        etc. have already been applied) so a request for one subcategory
        only ever scans option names belonging to that subcategory's own
        products — not the entire catalog.

        ProductOption is defined per-product, so the same logical option
        ("Finish") can legitimately exist as many rows with inconsistent
        casing/whitespace across products. We group by normalized form so
        option_finish matches "Finish", "finish", " FINISH " etc. without
        ever touching the stored rows.
        """
        names = (
            ProductOption.objects
            .filter(product__in=scope_qs)
            .values_list("name", flat=True)
            .distinct()
        )
        lookup: Dict[str, List[str]] = {}
        for name in names:
            lookup.setdefault(_normalize_option_key(name), []).append(name)
        return lookup

    def _apply_dynamic_option_filters(
        self,
        queryset: QuerySet,
        dynamic_params: Dict[str, List[str]],
        option_name_lookup: Dict[str, List[str]],
    ) -> QuerySet:
        for normalized_key, values in dynamic_params.items():
            if normalized_key == "brand":
                queryset = queryset.filter(
                    brand__in=values,
                )
                continue

            option_names = option_name_lookup.get(normalized_key)

            if not option_names:
                # A well-formed option_<key> that doesn't resolve to any
                # option within the current filtered scope is a real,
                # unsatisfiable constraint. Returning the unfiltered
                # queryset here would silently drop the customer's filter
                # and show them products that don't match what they asked
                # for — return no results instead.
                return queryset.none()

            # A SINGLE filter() call keeps the option-name and option-value
            # conditions on the same joined variant_options/option_value
            # row. That's what guarantees we're matching a value that is
            # actually attached to the product's variant for THIS option —
            # not a value that merely exists somewhere unrelated.
            queryset = queryset.filter(
                variants__variant_options__option_value__option__name__in=option_names,
                variants__variant_options__option_value__value__in=values,
            )
        return queryset

    def filter_queryset(self, queryset: QuerySet) -> QuerySet:
        """
        django-filter's own extension hook — called from inside its `qs`
        property, which already caches the result via `self._qs`. Overriding
        this (instead of `qs` itself) means we inherit that caching for
        free and stay on the library's normal lifecycle rather than
        reimplementing it.

        super().filter_queryset() applies every declared Filter first
        (category, subcategory, min_price, max_price, in_stock, +
        Meta.fields) exactly as django-filter normally would. Dynamic
        option_<key> filters are layered on top of that already-filtered
        queryset, so the option-name lookup stays scoped to it too.
        """
        queryset = super().filter_queryset(queryset)

        dynamic_params = self._parse_dynamic_option_params()
        needs_distinct = False

        if dynamic_params:
            option_name_lookup = self._build_option_name_lookup(queryset)
            queryset = self._apply_dynamic_option_filters(
                queryset, dynamic_params, option_name_lookup
            )
            needs_distinct = True

        # By this point in the lifecycle self.form has already been
        # validated (django-filter triggers self.errors before calling
        # filter_queryset), so cleaned_data is safe to read directly.
        cleaned = self.form.cleaned_data or {}
        if cleaned.get("min_price") is not None or cleaned.get("max_price") is not None:
            needs_distinct = True

        return queryset.distinct() if needs_distinct else queryset


class DeliveryRuleFilter(django_filters.FilterSet):
    """
    Filters DeliveryRule by its two derived (non-DB-column) concepts:
    status and scope. Both methods delegate to status_query()/scope_query()
    in delivery/rule_scope.py, which return Q objects — so filtering happens
    entirely in SQL and never loads the full DeliveryRule table into Python.
    (See that module's docstring: the Python-loop versions,
    compute_rule_status()/resolve_rule_scope(), are for per-instance
    serializer use only — NOT for filtering querysets.)
    """
    status = django_filters.ChoiceFilter(
        choices=[
            ("active", "Active"),
            ("scheduled", "Scheduled"),
            ("expired", "Expired"),
            ("inactive", "Inactive"),
        ],
        method="filter_status",
    )
    scope = django_filters.ChoiceFilter(
        choices=[
            ("global", "Global"),
            ("zone", "Zone"),
            ("category", "Category"),
            ("subcategory", "Subcategory"),
            ("product", "Product"),
            ("variant", "Variant"),
        ],
        method="filter_scope",
    )

    class Meta:
        model = DeliveryRule
        fields = ["active", "zone", "category", "combine_mode"]

    def filter_status(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        """SQL-side status filter — see status_query() docstring for the
        exact window logic (mirrors calculate_delivery() in services.py)."""
        return queryset.filter(status_query(value))

    def filter_scope(self, queryset: QuerySet, name: str, value: str) -> QuerySet:
        """SQL-side scope filter — see scope_query() docstring for the
        most-specific-wins precedence (variant > product > subcategory >
        category > zone > global)."""
        return queryset.filter(scope_query(value))