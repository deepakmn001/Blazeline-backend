from __future__ import annotations

from pathlib import Path

from PIL import Image


class ImageExtractor:

    DEFAULT_PADDING = 10

    def __init__(
        self,
        output_dir="media/catalog_products",
        padding=DEFAULT_PADDING,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.padding = padding

    def _crop(
        self,
        image,
        region,
    ):

        width, height = image.size

        left = max(0, int(region.left - self.padding))
        top = max(0, int(region.top - self.padding))
        right = min(width, int(region.right + self.padding))
        bottom = min(height, int(region.bottom + self.padding))

        return image.crop(
            (
                left,
                top,
                right,
                bottom,
            )
        )

    def _save(
        self,
        image,
        path,
    ):

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        image.save(
            path,
            "PNG",
            optimize=True,
        )

    def extract_from_page_image(
        self,
        page_image,
        products,
    ):

        extracted = []

        for product in products:

            crop = self._crop(
                page_image,
                product,
            )

            sku = (
    product.sku.text
    .replace("/", "_")
    .replace("\\", "_")
    .replace(" ", "_")
)

            image_path = (
                self.output_dir
                / f"page_{product.page_number:03d}"
                / f"{sku}.png"
            )

            self._save(
                crop,
                image_path,
            )

            product.image_path = image_path.relative_to("media").as_posix()

            extracted.append(product)

        return extracted