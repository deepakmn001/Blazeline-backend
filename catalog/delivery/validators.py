import re

PINCODE_RE = re.compile(r"^\d{6}$")


def normalize_pincode(raw_pincode):
    """
    Strips whitespace and validates a pincode is exactly 6 digits.
    Raises ValueError on malformed input — the caller (services.py)
    wraps this into CartValidationError / NotServiceableError as
    appropriate for the API boundary.
    """
    if raw_pincode is None:
        raise ValueError("Pincode is required.")

    cleaned = str(raw_pincode).strip()

    if not PINCODE_RE.match(cleaned):
        raise ValueError(f"'{raw_pincode}' is not a valid 6-digit pincode.")

    return cleaned