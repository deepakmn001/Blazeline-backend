import re

SKU_REGEX = re.compile(
    r"[A-Z]{2,5}-\d{4}(?:\([A-Z]\))?"
)


def center(bbox):
    x1 = bbox[0][0]
    y1 = bbox[0][1]

    x2 = bbox[2][0]
    y2 = bbox[2][1]

    return (
        (x1 + x2) / 2,
        (y1 + y2) / 2,
    )


def group_by_sku(rows):

    processed = []

    for row in rows:

        cx, cy = center(row["bbox"])

        row["center_x"] = cx
        row["center_y"] = cy

        processed.append(row)

    # --------------------------
    # Find SKU blocks
    # --------------------------

    sku_rows = []

    for row in processed:

        if SKU_REGEX.search(row["text"]):

            sku_rows.append(row)

    sku_rows.sort(
        key=lambda r: (
            r["center_y"],
            r["center_x"],
        )
    )

    products = []

    for sku in sku_rows:

        sx = sku["center_x"]
        sy = sku["center_y"]

        group = []

        for row in processed:

            dx = abs(row["center_x"] - sx)
            dy = row["center_y"] - sy

            if dx < 220 and -30 <= dy <= 240:
                group.append(row)

        group.sort(
            key=lambda r: (
                r["center_y"],
                r["center_x"],
            )
        )

        products.append(group)

    return products