from __future__ import annotations

from django.db import transaction

from catalog_import.models import (
    CatalogImport,
    ParsedProduct,
)

from .developer_service import parse_existing_catalog
from .database_importer import (
    build_parsed_product_models,
    bulk_save_products,
)


def delete_catalog_products(catalog: CatalogImport):
    """
    Delete all parsed products of a catalog.
    """
    return ParsedProduct.objects.filter(
        catalog=catalog
    ).delete()


def delete_catalog_page(
    catalog: CatalogImport,
    page: int,
):
    """
    Delete parsed products of one page only.
    """
    return ParsedProduct.objects.filter(
        catalog=catalog,
        page_number=page,
    ).delete()


def _save_parsed_products(
    catalog: CatalogImport,
    parsed_products,
):
    """
    Convert parsed dataclass objects into Django models
    and bulk save them.
    """

    model_instances = build_parsed_product_models(
        catalog,
        parsed_products,
    )

    bulk_save_products(
        model_instances,
    )

    return len(model_instances)


def reparse_catalog(
    catalog_id: int,
):
    """
    Delete all parsed products and import the catalog again.
    """

    with transaction.atomic():

        catalog, parsed_products, summary = parse_existing_catalog(
            catalog_id=catalog_id,
        )

        delete_catalog_products(
            catalog,
        )

        _save_parsed_products(
            catalog,
            parsed_products,
        )

        return summary


def reparse_page(
    catalog_id: int,
    page: int,
):
    """
    Delete one page and parse/import it again.
    """

    with transaction.atomic():

        catalog, parsed_products, summary = parse_existing_catalog(
            catalog_id=catalog_id,
            start_page=page,
            end_page=page,
        )

        delete_catalog_page(
            catalog,
            page,
        )

        

        _save_parsed_products(
            catalog,
            parsed_products,
        )

        return summary