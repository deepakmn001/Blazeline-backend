from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Sequence

from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from catalog.models import ProductVariant

from .models import Promotion, PromotionRule


ZERO = Decimal("0.00")
ONE_HUNDRED = Decimal("100.00")
TWO_PLACES = Decimal("0.01")


class PromotionCalculationError(Exception):
    """
    Base exception for promotion-engine calculation failures.
    """


@dataclass(frozen=True)
class PromotionLineInput:
    """
    Minimal immutable representation of a cart line required by the engine.

    The engine intentionally does not mutate the actual CartItem.
    """

    variant: ProductVariant
    quantity: int


@dataclass(frozen=True)
class PromotionLineResult:
    """
    Immutable calculated pricing result for one cart line.
    """

    variant_id: int
    quantity: int
    base_unit_price: Decimal
    base_line_total: Decimal
    discount_amount: Decimal
    final_unit_price: Decimal
    final_line_total: Decimal
    discount_percent: Decimal
    discount_label: str
    promotion_ids: tuple[int, ...]
    promotion_names: tuple[str, ...]

    @property
    def has_discount(self) -> bool:
        return self.discount_amount > ZERO


@dataclass(frozen=True)
class PromotionResult:
    """
    Immutable aggregate promotion calculation.

    This is a calculation snapshot only.
    It is NOT the historical order snapshot.
    """

    subtotal: Decimal
    discount_amount: Decimal
    discounted_subtotal: Decimal
    lines: tuple[PromotionLineResult, ...]
    applied_promotion_ids: tuple[int, ...]
    applied_promotion_names: tuple[str, ...]

    @property
    def has_discount(self) -> bool:
        return self.discount_amount > ZERO


@dataclass(frozen=True)
class _PromotionLineCandidate:
    """
    Internal candidate generated when a promotion matches a cart line.
    """

    promotion: Promotion
    rule: PromotionRule
    specificity: int
    discount_amount: Decimal


class PromotionEngine:
    """
    Authoritative promotion calculation engine.

    Design principles:
        1. Never trust frontend/cart calculated discounts.
        2. Never modify ProductVariant.selling_price.
        3. Use Decimal for monetary arithmetic.
        4. Re-evaluate at checkout.
        5. Produce deterministic results.
        6. Keep promotion calculation isolated from order persistence.
    """

    SPECIFICITY = {
        PromotionRule.TargetType.ALL: 1,
        PromotionRule.TargetType.CATEGORY: 2,
        PromotionRule.TargetType.SUBCATEGORY: 3,
        PromotionRule.TargetType.BRAND: 4,
        PromotionRule.TargetType.PRODUCT: 5,
        PromotionRule.TargetType.VARIANT: 6,
    }

    @classmethod
    def money(cls, value: Decimal | int | float | str | None) -> Decimal:
        """
        Normalize monetary values to exactly two decimal places.

        ROUND_HALF_UP is consistent with ordinary commercial currency
        rounding and prevents binary-float arithmetic from entering
        the promotion calculation.
        """
        if value is None:
            return ZERO

        decimal_value = (
            value
            if isinstance(value, Decimal)
            else Decimal(str(value))
        )

        return decimal_value.quantize(
            TWO_PLACES,
            rounding=ROUND_HALF_UP,
        )

    @classmethod
    def percentage_value(cls, value: Decimal) -> Decimal:
        return cls.money(value).quantize(
            TWO_PLACES,
            rounding=ROUND_HALF_UP,
        )

    @classmethod
    def _normalize_brand(cls, value: str | None) -> str | None:
        return PromotionRule.normalize_brand(value)

    @classmethod
    def _validate_quantity(cls, quantity: int) -> None:
        if isinstance(quantity, bool):
            raise PromotionCalculationError(
                "Cart quantity must be an integer."
            )

        if not isinstance(quantity, int):
            raise PromotionCalculationError(
                "Cart quantity must be an integer."
            )

        if quantity <= 0:
            raise PromotionCalculationError(
                "Cart quantity must be greater than zero."
            )

    @classmethod
    def _variant_base_price(
        cls,
        variant: ProductVariant,
    ) -> Decimal:
        """
        Use the existing authoritative ProductVariant.selling_price.

        No product price is modified by this service.
        """
        price = cls.money(variant.selling_price)

        if price < ZERO:
            raise PromotionCalculationError(
                f"Variant {variant.pk} has an invalid negative selling price."
            )

        return price

    @classmethod
    def _promotion_is_active(
        cls,
        promotion: Promotion,
        *,
        now,
    ) -> bool:
        """
        Authoritative runtime validity check.

        Half-open window:
            start_at <= now < end_at
        """
        if not promotion.is_active:
            return False

        if promotion.start_at is None or promotion.end_at is None:
            return False

        return (
            promotion.start_at <= now
            and now < promotion.end_at
        )

    @classmethod
    def _promotion_meets_cart_requirement(
        cls,
        promotion: Promotion,
        *,
        cart_subtotal: Decimal,
    ) -> bool:
        minimum = cls.money(promotion.minimum_cart_value)

        return cart_subtotal >= minimum

    @classmethod
    def _rule_matches_variant(
        cls,
        rule: PromotionRule,
        variant: ProductVariant,
    ) -> bool:
        """
        Match one promotion rule against one concrete variant.

        ProductVariant -> Product -> Category/SubCategory/Brand
        """

        target_type = rule.target_type

        if target_type == PromotionRule.TargetType.ALL:
            return True

        if target_type == PromotionRule.TargetType.CATEGORY:
            return (
                rule.category_id is not None
                and variant.product.category_id == rule.category_id
            )

        if target_type == PromotionRule.TargetType.SUBCATEGORY:
            return (
                rule.subcategory_id is not None
                and variant.product.subcategory_id == rule.subcategory_id
            )

        if target_type == PromotionRule.TargetType.BRAND:
            promotion_brand = cls._normalize_brand(rule.brand)
            product_brand = cls._normalize_brand(variant.product.brand)

            return (
                promotion_brand is not None
                and product_brand is not None
                and promotion_brand == product_brand
            )

        if target_type == PromotionRule.TargetType.PRODUCT:
            return (
                rule.product_id is not None
                and variant.product_id == rule.product_id
            )

        if target_type == PromotionRule.TargetType.VARIANT:
            return (
                rule.variant_id is not None
                and variant.pk == rule.variant_id
            )

        return False

    @classmethod
    def _best_matching_rule(
        cls,
        promotion: Promotion,
        variant: ProductVariant,
    ) -> PromotionRule | None:
        """
        A promotion can contain multiple rules.

        For a given variant, the engine uses the most specific matching
        rule belonging to that promotion.
        """
        matching_rules = [
            rule
            for rule in promotion.rules.all()
            if cls._rule_matches_variant(rule, variant)
        ]

        if not matching_rules:
            return None

        matching_rules.sort(
            key=lambda rule: (
                cls.SPECIFICITY.get(rule.target_type, 0),
                rule.id,
            ),
            reverse=True,
        )

        return matching_rules[0]

    @classmethod
    def _calculate_raw_discount(
        cls,
        promotion: Promotion,
        *,
        unit_price: Decimal,
        quantity: int,
    ) -> Decimal:
        """
        Calculate the gross discount for an applicable line.

        PERCENTAGE:
            unit_price * percentage * quantity

        FIXED_AMOUNT:
            fixed discount per unit * quantity

        The result is capped at the line subtotal.
        """
        line_subtotal = cls.money(unit_price * quantity)

        if line_subtotal <= ZERO:
            return ZERO

        if promotion.discount_type == Promotion.DiscountType.PERCENTAGE:
            percentage = cls.money(promotion.discount_value)

            discount = (
                line_subtotal * percentage / ONE_HUNDRED
            )

        elif promotion.discount_type == Promotion.DiscountType.FIXED_AMOUNT:
            fixed_amount = cls.money(promotion.discount_value)

            discount = fixed_amount * quantity

        else:
            raise PromotionCalculationError(
                f"Unsupported discount type: {promotion.discount_type}"
            )

        discount = cls.money(discount)

        return min(discount, line_subtotal)

    @classmethod
    def _promotion_candidate_sort_key(
        cls,
        candidate: _PromotionLineCandidate,
    ) -> tuple:
        """
        Deterministic winner selection:

            1. target specificity
            2. explicit promotion priority
            3. higher calculated discount
            4. lower promotion id (stable tie-breaker)
        """
        return (
            candidate.specificity,
            candidate.promotion.priority,
            candidate.discount_amount,
            -candidate.promotion.id,
        )

    @classmethod
    def _load_active_promotions(cls) -> QuerySet[Promotion]:
        """
        Load promotion/rule graph efficiently.

        The caller still filters by current validity and cart value.
        """
        rules_qs = PromotionRule.objects.select_related(
            "category",
            "subcategory",
            "product",
            "variant",
        ).order_by("id")

        return (
            Promotion.objects
            .filter(is_active=True)
            .prefetch_related(
                Prefetch(
                    "rules",
                    queryset=rules_qs,
                )
            )
        )

    @classmethod
    def _promotion_candidates_for_line(
        cls,
        *,
        promotions: Sequence[Promotion],
        line: PromotionLineInput,
        cart_subtotal: Decimal,
        now,
    ) -> list[_PromotionLineCandidate]:
        """
        Produce all applicable promotion candidates for one cart line.
        """
        cls._validate_quantity(line.quantity)

        variant = line.variant
        unit_price = cls._variant_base_price(variant)

        candidates: list[_PromotionLineCandidate] = []

        for promotion in promotions:
            if not cls._promotion_is_active(
                promotion,
                now=now,
            ):
                continue

            if not cls._promotion_meets_cart_requirement(
                promotion,
                cart_subtotal=cart_subtotal,
            ):
                continue

            matched_rule = cls._best_matching_rule(
                promotion,
                variant,
            )

            if matched_rule is None:
                continue

            discount_amount = cls._calculate_raw_discount(
                promotion,
                unit_price=unit_price,
                quantity=line.quantity,
            )

            if discount_amount <= ZERO:
                continue

            candidates.append(
                _PromotionLineCandidate(
                    promotion=promotion,
                    rule=matched_rule,
                    specificity=cls.SPECIFICITY[
                        matched_rule.target_type
                    ],
                    discount_amount=discount_amount,
                    
                )
            )

        return candidates

    @classmethod
    def _apply_promotion_cap(
        cls,
        candidate: _PromotionLineCandidate,
        requested_discount: Decimal,
    ) -> Decimal:
        """
        Apply promotion-level maximum discount cap.

        The cap is evaluated against the promotion's discount contribution.
        """
        requested_discount = cls.money(requested_discount)

        max_discount = candidate.promotion.max_discount_amount

        if max_discount is None:
            return requested_discount

        cap = cls.money(max_discount)

        return min(
            requested_discount,
            cap,
        )

    @classmethod
    def _select_line_promotions(
        cls,
        candidates: list[_PromotionLineCandidate],
    ) -> list[_PromotionLineCandidate]:
        """
        Decide which promotions may actually affect a line.

        Rules:
            - No applicable promotion => none
            - Any non-stackable promotion => highest-ranked non-stackable only
            - Otherwise stackable promotions may combine
        """
        if not candidates:
            return []

        candidates.sort(
            key=cls._promotion_candidate_sort_key,
            reverse=True,
        )

        non_stackable = [
            candidate
            for candidate in candidates
            if not candidate.promotion.stackable
        ]

        if non_stackable:
            return [non_stackable[0]]

        return candidates
    @classmethod
    def calculate_for_variants(cls, variants, *, quantity=1, now=None):
        """
        Calculate promotional pricing for multiple variants in one engine call.

        Used by catalog/product APIs so active promotions are loaded once
        instead of once per variant.
        """
        variant_list = list(variants)

        if not variant_list:
            return {}

        if isinstance(quantity, bool):
            raise PromotionCalculationError(
                "Promotion quantity must be an integer."
            )

        if not isinstance(quantity, int):
            raise PromotionCalculationError(
                "Promotion quantity must be an integer."
            )

        if quantity <= 0:
            raise PromotionCalculationError(
                "Promotion quantity must be greater than zero."
            )

        lines = (
            PromotionLineInput(
                variant=variant,
                quantity=quantity,
            )
            for variant in variant_list
        )

        result = cls.calculate(lines, now=now)

        return {
            line.variant_id: line
            for line in result.lines
        }

    @classmethod
    def calculate(
        cls,
        lines: Iterable[PromotionLineInput],
        *,
        now=None,
    ) -> PromotionResult:
        """
        Calculate the complete cart promotion result.

        `lines` must represent the current authoritative cart state.

        The same engine can be called:
            - by cart presentation logic
            - by checkout/order creation
            - by tests

        Checkout MUST call it again so the final order does not depend
        on an earlier cart response.
        """
        materialized_lines = tuple(lines)

        if not materialized_lines:
            return PromotionResult(
                subtotal=ZERO,
                discount_amount=ZERO,
                discounted_subtotal=ZERO,
                lines=tuple(),
                applied_promotion_ids=tuple(),
                applied_promotion_names=tuple(),
            )

        if now is None:
            now = timezone.now()

        subtotal = ZERO

        for line in materialized_lines:
            cls._validate_quantity(line.quantity)

            unit_price = cls._variant_base_price(
                line.variant,
            )

            subtotal += cls.money(
                unit_price * line.quantity
            )

        subtotal = cls.money(subtotal)

        promotions = tuple(
            cls._load_active_promotions()
        )

        line_results: list[PromotionLineResult] = []

        applied_promotion_ids: set[int] = set()
        applied_promotion_names: dict[int, str] = {}

        # Tracks actual discount already consumed against each promotion's
        # maximum cart-level cap.
        promotion_discount_used: dict[int, Decimal] = {}

        for line in materialized_lines:
            variant = line.variant
            quantity = line.quantity

            base_unit_price = cls._variant_base_price(
                variant,
            )

            base_line_total = cls.money(
                base_unit_price * quantity
            )

            candidates = cls._promotion_candidates_for_line(
                promotions=promotions,
                line=line,
                cart_subtotal=subtotal,
                now=now,
            )

            selected_candidates = cls._select_line_promotions(
                candidates
            )

            remaining_line_discount = base_line_total
            line_discount = ZERO

            selected_promotion_ids: list[int] = []
            selected_promotion_names: list[str] = []

            for candidate in selected_candidates:
                if remaining_line_discount <= ZERO:
                    break

                promotion_id = candidate.promotion.id

                already_used = promotion_discount_used.get(
                    promotion_id,
                    ZERO,
                )

                requested = candidate.discount_amount

                promotion_cap = candidate.promotion.max_discount_amount

                if promotion_cap is not None:
                    remaining_promotion_cap = cls.money(
                        promotion_cap
                    ) - already_used

                    if remaining_promotion_cap <= ZERO:
                        continue

                    requested = min(
                        requested,
                        remaining_promotion_cap,
                    )

                requested = min(
                    requested,
                    remaining_line_discount,
                )

                requested = cls._apply_promotion_cap(
                    candidate,
                    requested,
                )

                if requested <= ZERO:
                    continue

                requested = cls.money(requested)

                line_discount += requested
                remaining_line_discount -= requested

                promotion_discount_used[promotion_id] = (
                    already_used + requested
                )

                selected_promotion_ids.append(
                    promotion_id
                )
                selected_promotion_names.append(
                    candidate.promotion.name
                )

                applied_promotion_ids.add(
                    promotion_id
                )
                applied_promotion_names[promotion_id] = (
                    candidate.promotion.name
                )

            line_discount = min(
                cls.money(line_discount),
                base_line_total,
            )

            final_line_total = cls.money(
                base_line_total - line_discount
            )

            if quantity > 0:
                final_unit_price = cls.money(
                    final_line_total / quantity
                )
            else:
                final_unit_price = ZERO

            if base_line_total > ZERO:
                discount_percent = cls.money(
                    (
                        line_discount
                        / base_line_total
                    )
                    * ONE_HUNDRED
                )
            else:
                discount_percent = ZERO

            discount_percent = min(
                discount_percent,
                ONE_HUNDRED,
            )

            if line_discount > ZERO:
                if discount_percent == discount_percent.to_integral():
                    label = (
                        f"{int(discount_percent)}% OFF"
                    )
                else:
                    label = (
                        f"{discount_percent}% OFF"
                    )
            else:
                label = ""

            line_results.append(
                PromotionLineResult(
                    variant_id=variant.pk,
                    quantity=quantity,
                    base_unit_price=base_unit_price,
                    base_line_total=base_line_total,
                    discount_amount=line_discount,
                    final_unit_price=final_unit_price,
                    final_line_total=final_line_total,
                    discount_percent=discount_percent,
                    discount_label=label,
                    promotion_ids=tuple(
                        selected_promotion_ids
                    ),
                    promotion_names=tuple(
                        selected_promotion_names
                    ),
                )
            )

        total_discount = cls.money(
            sum(
                (
                    result.discount_amount
                    for result in line_results
                ),
                ZERO,
            )
        )

        # Final defensive invariant.
        total_discount = min(
            total_discount,
            subtotal,
        )

        discounted_subtotal = cls.money(
            subtotal - total_discount
        )

        return PromotionResult(
            subtotal=subtotal,
            discount_amount=total_discount,
            discounted_subtotal=discounted_subtotal,
            lines=tuple(line_results),
            applied_promotion_ids=tuple(
                sorted(applied_promotion_ids)
            ),
            applied_promotion_names=tuple(
                applied_promotion_names[promotion_id]
                for promotion_id in sorted(
                    applied_promotion_names
                )
            ),
        )


def calculate_cart_promotions(
    cart_items,
    *,
    now=None,
) -> PromotionResult:
    """
    Convenience adapter for the existing CartItem queryset.

    This function deliberately works from existing CartItem/Variant
    objects and does not modify them.
    """
    lines = (
        PromotionLineInput(
            variant=item.variant,
            quantity=int(item.quantity),
        )
        for item in cart_items
    )

    return PromotionEngine.calculate(
        lines,
        now=now,
    )