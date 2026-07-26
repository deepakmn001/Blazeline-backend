import os
import tempfile

import fitz


from .ocr import read_image
from .spatial_parser import (
    rows_to_blocks,
    find_skus,
    group_skus_into_rows,
    calculate_boundaries,
    debug_boundaries,
    assign_blocks_to_products,
    debug_products,
    extract_product_data,
    debug_extracted,
)
from PIL import Image
from .image_extractor import ImageExtractor

def parse_page(page, page_number):
    """
    Parse a single PDF page and return extracted products.
    """

    # --------------------------------------------------
    # Render Page
    # --------------------------------------------------

    pix = page.get_pixmap(
        matrix=fitz.Matrix(4, 4)
    )

    page_width = pix.width
    page_height = pix.height

    # --------------------------------------------------
    # Temporary Image
    # --------------------------------------------------

    temp_file = tempfile.NamedTemporaryFile(
        suffix=".png",
        delete=False,
    )

    image_path = temp_file.name
    temp_file.close()

    try:

        pix.save(image_path)
        page_image = Image.open(image_path)

        # --------------------------------------------------
        # OCR
        # --------------------------------------------------

        rows = read_image(image_path)

        print()
        print("=" * 80)
        print("OCR RAW OUTPUT")
        print("=" * 80)

        for index, row in enumerate(rows[:100], start=1):
            print(f"{index:03d}. {row}")

        print()
        print(f"TOTAL OCR ROWS : {len(rows)}")

        blocks = rows_to_blocks(rows)

        # --------------------------------------------------
        # SKU Detection
        # --------------------------------------------------

        skus = find_skus(blocks)

        print()
        print("=" * 80)
        print(f"PAGE {page_number} - SKU ANCHORS")
        print("=" * 80)

        for sku in skus:

            print(
                f"{sku.text:<20}"
                f"x={int(sku.x):5} "
                f"y={int(sku.y):5}"
            )

        print()
        print(f"Total SKU Found : {len(skus)}")

        # --------------------------------------------------
        # Row Detection
        # --------------------------------------------------

        grouped_rows = group_skus_into_rows(skus)

        print()
        print("=" * 80)
        print(f"PAGE {page_number} - SKU ROWS")
        print("=" * 80)

        for index, row in enumerate(grouped_rows, start=1):

            print(f"\nROW {index}")

            for sku in row:

                print(
                    f"   {sku.text:<20}"
                    f"x={int(sku.x):5} "
                    f"y={int(sku.y):5}"
                )

        print()
        print(f"Total Rows : {len(grouped_rows)}")

        # --------------------------------------------------
        # Column Boundaries
        # --------------------------------------------------

        print()
        print(f"Rendered Width : {page_width}")

        for index, row in enumerate(grouped_rows, start=1):

            print()
            print(f"ROW {index} BOUNDARIES")

            boundaries = calculate_boundaries(
                row=row,
                page_width=page_width,
            )

            debug_boundaries(boundaries)

        # --------------------------------------------------
        # Product Regions
        #
        # NOTE: as of the spatial_parser Phase 1/2 updates,
        # assign_blocks_to_products() already runs page-metadata
        # extraction internally and stamps collection/series/category/
        # subcategory onto every returned ProductRegion. Nothing here
        # needs to change to receive that data - it arrives on the
        # same objects this call already returns.
        # --------------------------------------------------

        products = assign_blocks_to_products(
            blocks=blocks,
            grouped_rows=grouped_rows,
            page_height=page_height,
            page_width=page_width,
        )

        debug_products(products)

        # --------------------------------------------------
        # Structured Extraction
        #
        # NOTE: extract_product_data() now also populates mb_price,
        # finishes, variant, and the confidence fields (sku_confidence,
        # name_confidence, price_confidence, layout_confidence,
        # ocr_confidence, ai_confidence) on each ProductRegion in place.
        # No call-site change needed here either - same function,
        # same signature, richer objects returned.
        #
        # PHASE 5 NOTE: as of the spatial_parser Phase 5 update,
        # ProductRegion also carries a dedicated `price` field for
        # "Standard Products" - a product with a single unlabeled price
        # (no GD/RGD/MB label found nearby at all). That value now
        # lands in product.price instead of product.gd_price, so a
        # populated gd_price downstream always means an actual GD label
        # was read off the page. This file doesn't build gd_price/price
        # itself - it only reads finished ProductRegion objects - so no
        # call-site change was needed here either; the debug preview
        # below has just been extended to surface the new field so it's
        # visible before products cross into importer.py.
        # --------------------------------------------------

        products = extract_product_data(products)

        

        # --------------------------------------------------
        # Preserve PDF page number
        # --------------------------------------------------

        for product in products:
            product.page_number = page_number

        extractor = ImageExtractor()

        extractor.extract_from_page_image(
            page_image=page_image,
            products=products,
        )

        page_image.close()

        debug_extracted(products)

        # --------------------------------------------------
        # Debug: confirm new fields are present before they cross
        # into importer.py's conversion step. Kept lightweight and
        # consistent with this file's existing print-based debugging
        # rather than introducing a logging framework here.
        #
        # PHASE 5 CHANGE: added `price=` alongside `mb=` so a Standard
        # Product's unlabeled price (now stored on product.price rather
        # than product.gd_price - see spatial_parser.py) is visible in
        # this preview instead of silently not showing up anywhere.
        # --------------------------------------------------

        print()
        print("=" * 80)
        print(f"PAGE {page_number} - METADATA & CONFIDENCE PREVIEW")
        print("=" * 80)

        for product in products:
            print(
                f"{product.sku.text:<16}"
                f"collection={product.collection or '-':<14}"
                f"series={product.series or '-':<16}"
                f"price={product.price or '-':<10}"
                f"mb={product.mb_price or '-':<10}"
                f"finishes={product.finishes}"
                f"ai_conf={product.ai_confidence}"
            )

        return products

    finally:

        if os.path.exists(image_path):
            os.remove(image_path)


def parse_catalog(
    pdf_source,
    start_page=None,
    end_page=None,
):
    """
    Parse an entire catalog PDF.

    Supports:
    - file path
    - Django UploadedFile
    - File object

    Optional:
    - start_page
    - end_page

    Returns:
        List[ProductRegion]
    """

    # --------------------------------------------------
    # Validate page range
    # --------------------------------------------------

    if start_page is not None and start_page < 1:
        raise ValueError("start_page must be >= 1")

    if end_page is not None and end_page < 1:
        raise ValueError("end_page must be >= 1")

    if (
        start_page is not None
        and end_page is not None
        and start_page > end_page
    ):
        raise ValueError("start_page cannot be greater than end_page")

    # --------------------------------------------------
    # Open PDF
    # --------------------------------------------------

    if isinstance(pdf_source, str):

        doc = fitz.open(pdf_source)

    else:

        pdf_source.seek(0)

        doc = fitz.open(
            stream=pdf_source.read(),
            filetype="pdf",
        )

    all_products = []

    try:

        total_pages = len(doc)

        print()
        print("=" * 80)
        print(f"TOTAL PAGES : {total_pages}")
        print("=" * 80)

        # --------------------------------------------------
        # Developer Mode Information
        # --------------------------------------------------

        if start_page is not None or end_page is not None:

            print()
            print("=" * 80)
            print(
                f"DEVELOPER MODE : "
                f"Parsing pages "
                f"{start_page or 1} -> {end_page or total_pages}"
            )
            print("=" * 80)

        # --------------------------------------------------
        # Parse Pages
        # --------------------------------------------------

        for page_number, page in enumerate(doc, start=1):

            # Skip pages before start_page
            if (
                start_page is not None
                and page_number < start_page
            ):
                continue

            # Stop after end_page
            if (
                end_page is not None
                and page_number > end_page
            ):
                break

            print()
            print("#" * 80)
            print(f"PARSING PAGE {page_number}/{total_pages}")
            print("#" * 80)

            products = parse_page(
                page=page,
                page_number=page_number,
            )

            all_products.extend(products)

    finally:

        doc.close()

    print()
    print("=" * 80)
    print(f"TOTAL PRODUCTS EXTRACTED : {len(all_products)}")
    print("=" * 80)

    return all_products