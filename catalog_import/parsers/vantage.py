import re

from .base import BaseCatalogParser


SKU_REGEX = re.compile(r"[A-Z]{2,5}-\d{4}(?:\([A-Z]\))?")
PRICE_REGEX = re.compile(r"\d{3,6}\.\d{2}")


class VantageParser(BaseCatalogParser):

    def parse(self, rows):

        products = []
        current = None

        for row in rows:

            text = row["text"].strip()

            sku = SKU_REGEX.search(text)

            if sku:

                if current:
                    products.append(current)

                current = {
                    "sku": sku.group(),
                    "name": "",
                    "price": None,
                    "finish": None,
                }

                continue

            if current is None:
                continue

            if text in ("GD", "RGD"):

                current["finish"] = text
                continue

            price = PRICE_REGEX.search(text)

            if price:

                current["price"] = float(price.group())
                continue

            if len(text) > 3:

                if current["name"]:
                    current["name"] += " "

                current["name"] += text

        if current:
            products.append(current)

        return products