import easyocr

reader = easyocr.Reader(
    ["en"],
    gpu=False,
)


def read_image(image_path):

    results = reader.readtext(image_path)

    rows = []

    for bbox, text, confidence in results:

        rows.append(
            {
                "bbox": bbox,
                "text": text.strip(),
                "confidence": float(confidence),
            }
        )

    return rows