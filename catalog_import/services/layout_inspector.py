import fitz


def inspect_page(pdf_path, page_number=20):

    doc = fitz.open(pdf_path)

    page = doc[page_number]

    print("=" * 80)
    print("PAGE INFO")
    print("=" * 80)

    print(f"Page Size : {page.rect.width} x {page.rect.height}")

    print()

    images = page.get_images(full=True)

    print(f"Images Found : {len(images)}")

    print()

    for index, img in enumerate(images, start=1):

        xref = img[0]

        rects = page.get_image_rects(xref)

        print("-" * 80)
        print(f"Image #{index}")
        print(f"XREF : {xref}")

        if not rects:
            print("No rectangle found")
            continue

        for rect in rects:

            print(
                f"Rect : ({rect.x0:.2f}, {rect.y0:.2f}) "
                f"-> ({rect.x1:.2f}, {rect.y1:.2f})"
            )

    print()
    print("=" * 80)
    print("TEXT BLOCKS")
    print("=" * 80)

    blocks = page.get_text("dict")["blocks"]

    print(f"Blocks : {len(blocks)}")

    for i, block in enumerate(blocks):

        if block["type"] != 0:
            continue

        print(
            f"Text Block {i} :",
            block["bbox"]
        )

    doc.close()