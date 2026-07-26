import fitz


def extract_text(pdf_path: str):
    """
    Extract text from every page of a PDF.

    Returns:
        [
            {
                "page": 1,
                "text": "...",
            },
            ...
        ]
    """

    document = fitz.open(pdf_path)

    pages = []

    for index, page in enumerate(document):

        pages.append(
            {
                "page": index + 1,
                "text": page.get_text("text"),
            }
        )

    document.close()

    return pages