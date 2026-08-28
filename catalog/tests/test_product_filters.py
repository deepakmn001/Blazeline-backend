"""
Tests for catalog/filters.py — ProductFilter.

Hits the real ProductViewSet directly via APIRequestFactory, so this
exercises the full filter_backends chain (DjangoFilterBackend -> our
ProductFilter -> pagination -> serialization) exactly as production does,
without depending on urls.py routing.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory

from ..models import (
    Category,
    SubCategory,
    Product,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    ProductVariantOption,
)
from ..views import ProductViewSet


def _list(request_kwargs=None):
    """Call ProductViewSet.list() directly and return the parsed response."""
    factory = APIRequestFactory()
    request = factory.get("/products/", request_kwargs or {})
    view = ProductViewSet.as_view({"get": "list"})
    response = view(request)
    response.render()
    return response


def _product_ids(response):
    data = response.data
    # StandardResultsPagination wraps results in "results"; fall back to
    # the raw list if pagination is ever disabled.
    items = data["results"] if isinstance(data, dict) and "results" in data else data
    return [item["id"] for item in items]


class ProductFilterTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category_a = Category.objects.create(name="Roofing", slug="roofing")
        cls.category_b = Category.objects.create(name="Electrical", slug="electrical")

        cls.subcategory = SubCategory.objects.create(
            category=cls.category_a, name="Sheets", slug="sheets"
        )
        cls.subcategory_other = SubCategory.objects.create(
            category=cls.category_b, name="Wires", slug="wires"
        )

        # ---- Product 1: single variant, Collection=Flute, Size=140x155, in stock ----
        cls.p1 = Product.objects.create(
            category=cls.category_a,
            subcategory=cls.subcategory,
            name="Sheet One",
            slug="sheet-one",
            status="published",
        )
        opt_collection_p1 = ProductOption.objects.create(product=cls.p1, name="Collection")
        opt_size_p1 = ProductOption.objects.create(product=cls.p1, name="Size")
        val_flute_p1 = ProductOptionValue.objects.create(option=opt_collection_p1, value="Flute")
        val_size_a_p1 = ProductOptionValue.objects.create(
            option=opt_size_p1, value="140 W X 155 H MM"
        )
        variant_p1 = ProductVariant.objects.create(
            product=cls.p1,
            sku="SHEET-1-A",
            mrp=Decimal("1500"),
            selling_price=Decimal("1200"),
            stock=5,
            active=True,
            is_default=True,
        )
        ProductVariantOption.objects.create(variant=variant_p1, option_value=val_flute_p1)
        ProductVariantOption.objects.create(variant=variant_p1, option_value=val_size_a_p1)

        # ---- Product 2: single variant, Collection=Bark Wood, Size=125x340, OUT of stock ----
        cls.p2 = Product.objects.create(
            category=cls.category_a,
            subcategory=cls.subcategory,
            name="Sheet Two",
            slug="sheet-two",
            status="published",
        )
        opt_collection_p2 = ProductOption.objects.create(product=cls.p2, name="Collection")
        opt_size_p2 = ProductOption.objects.create(product=cls.p2, name="Size")
        val_bark_p2 = ProductOptionValue.objects.create(option=opt_collection_p2, value="Bark Wood")
        val_size_b_p2 = ProductOptionValue.objects.create(
            option=opt_size_p2, value="125 W X 340 H MM"
        )
        variant_p2 = ProductVariant.objects.create(
            product=cls.p2,
            sku="SHEET-2-A",
            mrp=Decimal("1800"),
            selling_price=Decimal("1500"),
            stock=0,
            active=True,
            is_default=True,
        )
        ProductVariantOption.objects.create(variant=variant_p2, option_value=val_bark_p2)
        ProductVariantOption.objects.create(variant=variant_p2, option_value=val_size_b_p2)

        # ---- Product 3: TWO variants (tests cross-group AND + no duplicate rows) ----
        # variant_p3a: Collection=Flute, Size=125x340, in stock
        # variant_p3b: Collection=Bark Wood, Size=140x155, out of stock
        cls.p3 = Product.objects.create(
            category=cls.category_a,
            subcategory=cls.subcategory,
            name="Sheet Three",
            slug="sheet-three",
            status="published",
        )
        opt_collection_p3 = ProductOption.objects.create(product=cls.p3, name="Collection")
        opt_size_p3 = ProductOption.objects.create(product=cls.p3, name="Size")
        val_flute_p3 = ProductOptionValue.objects.create(option=opt_collection_p3, value="Flute")
        val_bark_p3 = ProductOptionValue.objects.create(option=opt_collection_p3, value="Bark Wood")
        val_size_a_p3 = ProductOptionValue.objects.create(
            option=opt_size_p3, value="140 W X 155 H MM"
        )
        val_size_b_p3 = ProductOptionValue.objects.create(
            option=opt_size_p3, value="125 W X 340 H MM"
        )
        variant_p3a = ProductVariant.objects.create(
            product=cls.p3,
            sku="SHEET-3-A",
            mrp=Decimal("1300"),
            selling_price=Decimal("1100"),
            stock=3,
            active=True,
            is_default=True,
        )
        ProductVariantOption.objects.create(variant=variant_p3a, option_value=val_flute_p3)
        ProductVariantOption.objects.create(variant=variant_p3a, option_value=val_size_b_p3)

        variant_p3b = ProductVariant.objects.create(
            product=cls.p3,
            sku="SHEET-3-B",
            mrp=Decimal("1300"),
            selling_price=Decimal("1100"),
            stock=0,
            active=True,
        )
        ProductVariantOption.objects.create(variant=variant_p3b, option_value=val_bark_p3)
        ProductVariantOption.objects.create(variant=variant_p3b, option_value=val_size_a_p3)

        # ---- Product 4: different category entirely (control group) ----
        cls.p4 = Product.objects.create(
            category=cls.category_b,
            subcategory=cls.subcategory_other,
            name="Cable One",
            slug="cable-one",
            status="published",
        )
        variant_p4 = ProductVariant.objects.create(
            product=cls.p4,
            sku="CABLE-1-A",
            mrp=Decimal("500"),
            selling_price=Decimal("400"),
            stock=10,
            active=True,
            is_default=True,
        )

    # ------------------------------------------------------------------
    # Existing / backward-compatible filters
    # ------------------------------------------------------------------

    def test_category_only(self):
        response = _list({"category": self.category_a.id})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p2.id, self.p3.id})

    def test_subcategory_only(self):
        response = _list({"subcategory": self.subcategory.id})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p2.id, self.p3.id})

    def test_min_price(self):
        response = _list({"min_price": "1200"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        # p1=1200, p2=1500 qualify; p3's variants are 1100 each so it's excluded;
        # p4=400 excluded.
        self.assertEqual(ids, {self.p1.id, self.p2.id})

    def test_max_price(self):
        response = _list({"max_price": "1100"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p3.id, self.p4.id})

    def test_category_plus_min_price(self):
        response = _list({"category": self.category_a.id, "min_price": "1200"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p2.id})

    # ------------------------------------------------------------------
    # in_stock
    # ------------------------------------------------------------------

    def test_in_stock_true(self):
        response = _list({"in_stock": "true"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        # p1 (stock 5), p3 (variant_p3a stock 3), p4 (stock 10) qualify.
        # p2's only variant has stock 0.
        self.assertEqual(ids, {self.p1.id, self.p3.id, self.p4.id})

    def test_in_stock_false(self):
        response = _list({"in_stock": "false"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p2.id})

    # ------------------------------------------------------------------
    # Dynamic option_<key> filters
    # ------------------------------------------------------------------

    def test_one_dynamic_option(self):
        response = _list({"option_collection": "Flute"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p3.id})

    def test_two_values_same_option_is_or(self):
        response = _list(
            {
                "option_size": [
                    "140 W X 155 H MM",
                    "125 W X 340 H MM",
                ]
            }
        )
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        # Every product has a variant matching one size or the other.
        self.assertEqual(ids, {self.p1.id, self.p2.id, self.p3.id})

    def test_two_different_options_is_and(self):
        # p1: Flute + 140x155 on the SAME (only) variant -> matches.
        # p3: Flute is on variant_p3a (size 125x340), Bark Wood+140x155 is on
        #     variant_p3b. Product-level AND (not same-variant AND) still
        #     matches p3 because it has *a* variant with Flute and *a*
        #     variant with 140x155 (different variants) — this is the
        #     documented "different option groups = AND at product level"
        #     semantics from the spec, not same-row AND.
        response = _list(
            {
                "option_collection": "Flute",
                "option_size": "140 W X 155 H MM",
            }
        )
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p3.id})
        self.assertNotIn(self.p2.id, ids)

    def test_dynamic_option_plus_price(self):
        response = _list({"option_collection": "Flute", "min_price": "1150"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        # p1 (1200) qualifies; p3's variants are 1100, below min_price.
        self.assertEqual(ids, {self.p1.id})

    def test_dynamic_option_plus_in_stock(self):
        response = _list({"option_collection": "Bark Wood", "in_stock": "true"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        # p2 has Bark Wood but its only variant is out of stock.
        # p3 has Bark Wood on variant_p3b (out of stock), but variant_p3a
        # (Flute, in stock) is what satisfies in_stock=true at product level.
        self.assertEqual(ids, {self.p3.id})

    def test_unknown_option_returns_no_results(self):
        # A well-formed option_<key> that doesn't resolve to any real
        # option within the filtered scope is an unsatisfiable constraint,
        # not a no-op — it must return zero results, never a 500, and
        # never the unfiltered set.
        response = _list({"option_unknown_axis": "foo"})
        self.assertEqual(response.status_code, 200)
        ids = _product_ids(response)
        self.assertEqual(ids, [])

    def test_option_out_of_scope_for_category_returns_no_results(self):
        # "Collection" exists on products in category_a, but this request
        # is scoped to category_b (Cable One), which has no options at
        # all — the lookup must be built from the FILTERED scope, so this
        # correctly returns nothing rather than matching category_a's data.
        response = _list({"category": self.category_b.id, "option_collection": "Flute"})
        self.assertEqual(response.status_code, 200)
        ids = _product_ids(response)
        self.assertEqual(ids, [])

    def test_empty_option_value_is_ignored(self):
        response = _list({"option_collection": ""})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p2.id, self.p3.id, self.p4.id})

    def test_option_key_normalization(self):
        # "Collection" in the DB should match "option_collection" regardless
        # of the casing/spacing a client sends in the param key itself.
        response = _list({"option_Collection": "Flute"})
        self.assertEqual(response.status_code, 200)
        ids = set(_product_ids(response))
        self.assertEqual(ids, {self.p1.id, self.p3.id})

    # ------------------------------------------------------------------
    # Error handling — must never 500
    # ------------------------------------------------------------------

    def test_malformed_numeric_price_does_not_crash(self):
        # django-filter's own form validation rejects non-numeric input
        # with a clean 400 — that's expected, pre-existing behavior
        # (inherited unchanged from the original NumberFilter), not
        # something this filter set introduces. The actual requirement
        # is "never a 500", not "always 200".
        response = _list({"min_price": "abc"})
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 400)

    def test_malformed_category_does_not_crash(self):
        response = _list({"category": "abc"})
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 400)

    def test_malformed_in_stock_does_not_crash(self):
        response = _list({"in_stock": "hello"})
        self.assertEqual(response.status_code, 200)

    def test_bare_option_prefix_does_not_crash(self):
        response = _list({"option_": "foo"})
        self.assertEqual(response.status_code, 200)

    # ------------------------------------------------------------------
    # Pagination + duplicate-row safety
    # ------------------------------------------------------------------

    def test_pagination_compatibility(self):
        response = _list({"category": self.category_a.id})
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)
        self.assertIn("count", response.data)

    def test_no_duplicate_products_from_multi_variant_join(self):
        # p3 has two variants; a naive join-based filter could return it
        # twice. It must appear exactly once.
        response = _list({"category": self.category_a.id, "option_size": "125 W X 340 H MM"})
        self.assertEqual(response.status_code, 200)
        ids = _product_ids(response)
        self.assertEqual(len(ids), len(set(ids)), "Duplicate product rows returned")