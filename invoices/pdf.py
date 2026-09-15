from __future__ import annotations

from io import BytesIO
from pathlib import Path
from decimal import Decimal
from typing import Iterable

from django.conf import settings
from django.core.exceptions import ValidationError

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .models import Invoice, InvoiceItem


# ============================================================================
# BRAND
# ============================================================================

BLAZELINE_ORANGE = colors.HexColor("#FF6A00")
BLAZELINE_BLACK = colors.HexColor("#0B0B0B")
BLAZELINE_DARK = colors.HexColor("#171717")
BLAZELINE_GRAY = colors.HexColor("#6B7280")
BLAZELINE_MID_GRAY = colors.HexColor("#9CA3AF")
BLAZELINE_LIGHT = colors.HexColor("#F7F7F7")
BLAZELINE_BORDER = colors.HexColor("#E5E7EB")
BLAZELINE_GREEN = colors.HexColor("#15803D")
WHITE = colors.white

COMPANY_NAME = "BLAZELINE VENTURES PRIVATE LIMITED"
COMPANY_ADDRESS_LINE_1 = "58/5B B.T Road"
COMPANY_ADDRESS_LINE_2 = "Kolkata - 700002"
COMPANY_GSTIN = "19AAOCB7883M1ZM"
COMPANY_WEBSITE = "www.blazeline.in"
COMPANY_TAGLINE = "ACCELERATING EVERY BUILD"

PAGE_WIDTH, PAGE_HEIGHT = A4

LEFT_MARGIN = 16 * mm
RIGHT_MARGIN = 16 * mm
TOP_MARGIN = 17 * mm
BOTTOM_MARGIN = 28 * mm

CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN


# ============================================================================
# ASSET CONFIGURATION
# ============================================================================


def _invoice_asset_path(filename: str) -> Path:
    """
    Resolve invoice assets from the project.

    Expected production structure:

        invoices/
            assets/
                blazeline_logo.png
                DejaVuSans.ttf
                DejaVuSans-Bold.ttf
    """

    configured_assets_dir = getattr(
        settings,
        "BLAZELINE_INVOICE_ASSETS_DIR",
        None,
    )

    if configured_assets_dir:
        assets_dir = Path(configured_assets_dir)
    else:
        assets_dir = Path(settings.BASE_DIR) / "invoices" / "assets"

    return assets_dir / filename


def _register_fonts() -> tuple[str, str]:
    """
    Register a Unicode font so Indian Rupee (₹) renders correctly.

    DejaVu Sans is used because it supports the ₹ glyph.

    For production, keep the TTF files inside the application image or
    configure explicit paths through settings.
    """

    regular_path = Path(
        getattr(
            settings,
            "BLAZELINE_INVOICE_FONT_PATH",
            _invoice_asset_path("DejaVuSans.ttf"),
        )
    )

    bold_path = Path(
        getattr(
            settings,
            "BLAZELINE_INVOICE_BOLD_FONT_PATH",
            _invoice_asset_path("DejaVuSans-Bold.ttf"),
        )
    )

    if not regular_path.is_file():
        raise ValidationError(
            "Invoice Unicode font not found. "
            f"Expected: {regular_path}"
        )

    if not bold_path.is_file():
        raise ValidationError(
            "Invoice bold Unicode font not found. "
            f"Expected: {bold_path}"
        )

    regular_name = "BlazeLineDejaVu"
    bold_name = "BlazeLineDejaVuBold"

    if regular_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont(
                regular_name,
                str(regular_path),
            )
        )

    if bold_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont(
                bold_name,
                str(bold_path),
            )
        )

    return regular_name, bold_name


def _resolve_logo_path() -> Path:
    configured = getattr(
        settings,
        "BLAZELINE_INVOICE_LOGO_PATH",
        None,
    )

    if configured:
        return Path(configured)

    return _invoice_asset_path("blazeline_logo.png")


# ============================================================================
# HELPERS
# ============================================================================


def _money(
    value: Decimal | int | str,
    currency: str = "INR",
) -> str:
    """
    Format monetary values with correct Indian Rupee rendering.
    """

    amount = Decimal(
        str(value or "0")
    ).quantize(
        Decimal("0.01")
    )

    currency = str(currency or "INR").upper()

    if currency == "INR":
        return f"₹ {amount:,.2f}"

    return f"{currency} {amount:,.2f}"


def _clean(value: object) -> str:
    return str(value or "").strip()


def _escape_text(value: object) -> str:
    """
    Escape values before inserting them into ReportLab Paragraph markup.
    """

    text = _clean(value)

    return (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _payment_label(value: object) -> str:
    value = _clean(value).lower()

    labels = {
        "upi": "UPI",
        "card": "CARD",
        "netbanking": "NET BANKING",
        "cod": "CASH ON DELIVERY",
        "credit": "CREDIT TERMS",
    }

    return labels.get(
        value,
        value.replace("_", " ").upper(),
    )


def _status_label(value: object) -> str:
    value = _clean(value).lower()

    return value.replace(
        "_",
        " ",
    ).upper()


def _safe_image_size(
    image_path: Path,
    *,
    max_width: float,
    max_height: float,
) -> tuple[float, float]:
    """
    Preserve original image aspect ratio while fitting within bounds.
    """

    reader = ImageReader(str(image_path))

    width, height = reader.getSize()

    if width <= 0 or height <= 0:
        raise ValidationError(
            "Invoice logo has invalid dimensions."
        )

    ratio = min(
        max_width / float(width),
        max_height / float(height),
    )

    return (
        width * ratio,
        height * ratio,
    )


# ============================================================================
# STYLES
# ============================================================================


def _build_styles(
    regular_font: str,
    bold_font: str,
):
    base = getSampleStyleSheet()

    return {
        "brand_subtitle": ParagraphStyle(
            "InvoiceBrandSubtitle",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.2,
            leading=9,
            textColor=BLAZELINE_GRAY,
        ),
        "company_details": ParagraphStyle(
            "InvoiceCompanyDetails",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.2,
            leading=9.5,
            textColor=BLAZELINE_GRAY,
        ),
        "document_title": ParagraphStyle(
            "InvoiceDocumentTitle",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=20,
            leading=22,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_BLACK,
        ),
        "document_meta": ParagraphStyle(
            "InvoiceDocumentMeta",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.5,
            leading=10,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_GRAY,
        ),
        "document_meta_bold": ParagraphStyle(
            "InvoiceDocumentMetaBold",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=7.5,
            leading=10,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_BLACK,
        ),
        "section_title": ParagraphStyle(
            "InvoiceSectionTitle",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=8,
            leading=10,
            textColor=BLAZELINE_BLACK,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "InvoiceBody",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=8,
            leading=10.8,
            textColor=colors.HexColor("#222222"),
        ),
        "body_bold": ParagraphStyle(
            "InvoiceBodyBold",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=8,
            leading=10.8,
            textColor=BLAZELINE_BLACK,
        ),
        "body_small": ParagraphStyle(
            "InvoiceBodySmall",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7,
            leading=9.2,
            textColor=BLAZELINE_GRAY,
        ),
        "body_small_bold": ParagraphStyle(
            "InvoiceBodySmallBold",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=7,
            leading=9.2,
            textColor=BLAZELINE_BLACK,
        ),
        "table_header": ParagraphStyle(
            "InvoiceTableHeader",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=6.8,
            leading=8.5,
            textColor=WHITE,
        ),
        "table_cell": ParagraphStyle(
            "InvoiceTableCell",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.1,
            leading=9,
            textColor=colors.HexColor("#222222"),
        ),
        "table_cell_bold": ParagraphStyle(
            "InvoiceTableCellBold",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=7.1,
            leading=9,
            textColor=BLAZELINE_BLACK,
        ),
        "summary_label": ParagraphStyle(
            "InvoiceSummaryLabel",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.8,
            leading=10,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_GRAY,
        ),
        "summary_value": ParagraphStyle(
            "InvoiceSummaryValue",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=7.8,
            leading=10,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_BLACK,
        ),
        "grand_total_label": ParagraphStyle(
            "InvoiceGrandTotalLabel",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=9,
            leading=11,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_BLACK,
        ),
        "grand_total_value": ParagraphStyle(
            "InvoiceGrandTotalValue",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=13,
            leading=16,
            alignment=TA_RIGHT,
            textColor=BLAZELINE_ORANGE,
        ),
        "payment_status_paid": ParagraphStyle(
            "InvoicePaidStatus",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=8,
            leading=10,
            textColor=BLAZELINE_GREEN,
        ),
        "footer": ParagraphStyle(
            "InvoiceFooter",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=6.6,
            leading=8.5,
            textColor=BLAZELINE_GRAY,
        ),
    }


# ============================================================================
# DOCUMENT TEMPLATE
# ============================================================================


class InvoiceDocumentTemplate(BaseDocTemplate):
    def __init__(
        self,
        buffer,
        **kwargs,
    ):
        super().__init__(
            buffer,
            pagesize=A4,
            leftMargin=LEFT_MARGIN,
            rightMargin=RIGHT_MARGIN,
            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN,
            **kwargs,
        )

        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="invoice_frame",
        )

        self.addPageTemplates(
            [
                PageTemplate(
                    id="invoice",
                    frames=[frame],
                    onPage=_draw_page_chrome,
                )
            ]
        )


def _draw_page_chrome(
    canvas,
    document,
):
    canvas.saveState()

    # ------------------------------------------------------------------
    # Top brand accent
    # ------------------------------------------------------------------

    canvas.setFillColor(BLAZELINE_ORANGE)

    canvas.rect(
        0,
        PAGE_HEIGHT - 4.5 * mm,
        PAGE_WIDTH,
        4.5 * mm,
        fill=1,
        stroke=0,
    )

    # ------------------------------------------------------------------
    # Footer separator
    # ------------------------------------------------------------------

    canvas.setStrokeColor(BLAZELINE_BORDER)
    canvas.setLineWidth(0.5)

    canvas.line(
        LEFT_MARGIN,
        18 * mm,
        PAGE_WIDTH - RIGHT_MARGIN,
        18 * mm,
    )

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------

    font_name = (
        "BlazeLineDejaVu"
        if "BlazeLineDejaVu"
        in pdfmetrics.getRegisteredFontNames()
        else "Helvetica"
    )

    canvas.setFont(
        font_name,
        6.5,
    )

    canvas.setFillColor(
        BLAZELINE_GRAY
    )

    canvas.drawString(
        LEFT_MARGIN,
        12.5 * mm,
        f"{COMPANY_NAME} • {COMPANY_WEBSITE}",
    )

    canvas.drawRightString(
        PAGE_WIDTH - RIGHT_MARGIN,
        12.5 * mm,
        f"Page {document.page}",
    )

    canvas.restoreState()


# ============================================================================
# HEADER
# ============================================================================


def _build_header(
    *,
    invoice: Invoice,
    styles,
):
    logo_path = _resolve_logo_path()

    if not logo_path.is_file():
        raise ValidationError(
            "BlazeLine invoice logo not found. "
            f"Expected: {logo_path}"
        )

    logo_width, logo_height = _safe_image_size(
        logo_path,
        max_width=66 * mm,
        max_height=26 * mm,
    )

    logo = Image(
        str(logo_path),
        width=logo_width,
        height=logo_height,
        hAlign="LEFT",
    )

    company_block = [
        logo,
        Spacer(1, 2.5 * mm),
        Paragraph(
            COMPANY_NAME,
            ParagraphStyle(
                "InvoiceCompanyName",
                parent=styles["body_small_bold"],
                fontSize=7.8,
                leading=9.6,
            ),
        ),
        Paragraph(
            (
                f"{_escape_text(COMPANY_ADDRESS_LINE_1)}<br/>"
                f"{_escape_text(COMPANY_ADDRESS_LINE_2)}"
            ),
            styles["company_details"],
        ),
        Paragraph(
            f"<b>GSTIN:</b> {_escape_text(COMPANY_GSTIN)}",
            styles["company_details"],
        ),
    ]

    invoice_meta = [
        Paragraph(
            "TAX INVOICE",
            styles["document_title"],
        ),
        Spacer(1, 2.5 * mm),
        Paragraph(
            f"<b>Invoice No.</b> "
            f"{_escape_text(invoice.invoice_number)}",
            styles["document_meta_bold"],
        ),
        Paragraph(
            f"<b>Order No.</b> "
            f"{_escape_text(invoice.order.order_number)}",
            styles["document_meta"],
        ),
        Paragraph(
            f"<b>Invoice Date</b> "
            f"{invoice.issued_at.strftime('%d %b %Y')}",
            styles["document_meta"],
        ),
    ]

    table = Table(
        [[company_block, invoice_meta]],
        colWidths=[
            108 * mm,
            CONTENT_WIDTH - 108 * mm,
        ],
    )

    table.setStyle(
        TableStyle(
            [
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "TOP",
                ),
                (
                    "ALIGN",
                    (1, 0),
                    (1, 0),
                    "RIGHT",
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    0,
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    0,
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    0,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    0,
                ),
            ]
        )
    )

    return [
        table,
        Spacer(1, 5 * mm),
        Table(
            [[""]],
            colWidths=[CONTENT_WIDTH],
            rowHeights=[1.1 * mm],
            style=TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, -1),
                        BLAZELINE_ORANGE,
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        0,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        0,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        0,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        0,
                    ),
                ]
            ),
        ),
    ]


# ============================================================================
# CUSTOMER / PAYMENT BLOCK
# ============================================================================


def _build_party_information(
    *,
    invoice: Invoice,
    styles,
):
    customer_lines = [
        Paragraph(
            "BILL TO",
            styles["section_title"],
        ),
        Paragraph(
            f"<b>{_escape_text(invoice.customer_name)}</b>",
            styles["body"],
        ),
    ]

    if invoice.company_name:
        customer_lines.append(
            Paragraph(
                _escape_text(invoice.company_name),
                styles["body"],
            )
        )

    address_parts = [
        invoice.billing_address_line1,
        invoice.billing_address_line2,
        invoice.billing_landmark,
        (
            f"{invoice.billing_city}, "
            f"{invoice.billing_state} - "
            f"{invoice.billing_pincode}"
        ),
    ]

    address_parts = [
        _escape_text(value)
        for value in address_parts
        if _clean(value)
    ]

    if address_parts:
        customer_lines.append(
            Paragraph(
                "<br/>".join(address_parts),
                styles["body_small"],
            )
        )

    if invoice.customer_phone:
        customer_lines.append(
            Paragraph(
                f"<b>Phone:</b> "
                f"{_escape_text(invoice.customer_phone)}",
                styles["body_small"],
            )
        )

    if invoice.customer_email:
        customer_lines.append(
            Paragraph(
                f"<b>Email:</b> "
                f"{_escape_text(invoice.customer_email)}",
                styles["body_small"],
            )
        )

    if invoice.customer_gstin:
        customer_lines.append(
            Paragraph(
                f"<b>Customer GSTIN:</b> "
                f"{_escape_text(invoice.customer_gstin)}",
                styles["body_small"],
            )
        )

    payment_lines = [
        Paragraph(
            "PAYMENT",
            styles["section_title"],
        ),
        Paragraph(
            f"<b>Method:</b> "
            f"{_payment_label(invoice.payment_method)}",
            styles["body"],
        ),
    ]

    if _clean(invoice.payment_status).lower() == "paid":
        payment_lines.append(
            Paragraph(
                "<b>Status:</b> PAID",
                styles["payment_status_paid"],
            )
        )
    else:
        payment_lines.append(
            Paragraph(
                f"<b>Status:</b> "
                f"{_status_label(invoice.payment_status)}",
                styles["body_bold"],
            )
        )

    if invoice.razorpay_payment_id:
        payment_lines.append(
            Paragraph(
                "Razorpay ID:<br/>"
                f"{_escape_text(invoice.razorpay_payment_id)}",
                styles["body_small"],
            )
        )

    table = Table(
        [
            [
                customer_lines,
                payment_lines,
            ]
        ],
        colWidths=[
            118 * mm,
            CONTENT_WIDTH - 118 * mm,
        ],
    )

    table.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, -1),
                    BLAZELINE_LIGHT,
                ),
                (
                    "BOX",
                    (0, 0),
                    (-1, -1),
                    0.6,
                    BLAZELINE_BORDER,
                ),
                (
                    "INNERGRID",
                    (0, 0),
                    (-1, -1),
                    0.4,
                    BLAZELINE_BORDER,
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "TOP",
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    9,
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    9,
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    8,
                ),
            ]
        )
    )

    return table


# ============================================================================
# ITEMS
# ============================================================================


def _build_items_table(
    *,
    items: Iterable[InvoiceItem],
    currency: str,
    styles,
):
    rows = [
        [
            Paragraph("#", styles["table_header"]),
            Paragraph(
                "PRODUCT / DESCRIPTION",
                styles["table_header"],
            ),
            Paragraph(
                "SKU",
                styles["table_header"],
            ),
            Paragraph(
                "QTY",
                styles["table_header"],
            ),
            Paragraph(
                "UNIT PRICE",
                styles["table_header"],
            ),
            Paragraph(
                "GST",
                styles["table_header"],
            ),
            Paragraph(
                "AMOUNT",
                styles["table_header"],
            ),
        ]
    ]

    for index, item in enumerate(
        items,
        start=1,
    ):
        description = (
            f"<b>{_escape_text(item.product_name)}</b>"
        )

        if item.variant_name:
            description += (
                "<br/>"
                f"<font size='6.5' color='#6B7280'>"
                f"{_escape_text(item.variant_name)}"
                f"</font>"
            )

        rows.append(
            [
                Paragraph(
                    str(index),
                    styles["table_cell"],
                ),
                Paragraph(
                    description,
                    styles["table_cell"],
                ),
                Paragraph(
                    _escape_text(item.sku),
                    styles["table_cell"],
                ),
                Paragraph(
                    f"{item.quantity:,}",
                    styles["table_cell"],
                ),
                Paragraph(
                    _money(
                        item.unit_price,
                        currency,
                    ),
                    styles["table_cell"],
                ),
                Paragraph(
                    (
                        f"{Decimal(item.tax_rate):.2f}%"
                        "<br/>"
                        f"{_money(item.tax_amount, currency)}"
                    ),
                    styles["table_cell"],
                ),
                Paragraph(
                    _money(
                        item.line_total,
                        currency,
                    ),
                    styles["table_cell_bold"],
                ),
            ]
        )

    table = Table(
        rows,
        repeatRows=1,
        splitByRow=1,
        colWidths=[
            8 * mm,
            57 * mm,
            29 * mm,
            13 * mm,
            26 * mm,
            24 * mm,
            27 * mm,
        ],
    )

    table.setStyle(
        TableStyle(
            [
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    BLAZELINE_BLACK,
                ),
                (
                    "LINEBELOW",
                    (0, 0),
                    (-1, 0),
                    1.2,
                    BLAZELINE_ORANGE,
                ),
                (
                    "GRID",
                    (0, 1),
                    (-1, -1),
                    0.35,
                    BLAZELINE_BORDER,
                ),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [
                        WHITE,
                        colors.HexColor("#FBFBFB"),
                    ],
                ),
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE",
                ),
                (
                    "ALIGN",
                    (0, 0),
                    (0, -1),
                    "CENTER",
                ),
                (
                    "ALIGN",
                    (3, 1),
                    (-1, -1),
                    "RIGHT",
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    5,
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    5,
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -1),
                    6,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -1),
                    6,
                ),
            ]
        )
    )

    return table


# ============================================================================
# TOTALS
# ============================================================================


def _summary_row(
    *,
    label: str,
    value: str,
    styles,
):
    return [
        Paragraph(
            label,
            styles["summary_label"],
        ),
        Paragraph(
            value,
            styles["summary_value"],
        ),
    ]


def _build_totals(
    *,
    invoice: Invoice,
    styles,
):
    rows = [
        _summary_row(
            label="Subtotal",
            value=_money(
                invoice.subtotal,
                invoice.currency,
            ),
            styles=styles,
        )
    ]

    if invoice.discount_amount:
        rows.append(
            _summary_row(
                label="Discount",
                value=(
                    f"- {_money(invoice.discount_amount, invoice.currency)}"
                ),
                styles=styles,
            )
        )

    rows.append(
        _summary_row(
            label="Delivery",
            value=_money(
                invoice.delivery_charge,
                invoice.currency,
            ),
            styles=styles,
        )
    )

    if invoice.cod_fee:
        rows.append(
            _summary_row(
                label="COD Fee",
                value=_money(
                    invoice.cod_fee,
                    invoice.currency,
                ),
                styles=styles,
            )
        )

    rows.append(
        _summary_row(
            label="GST / Tax",
            value=_money(
                invoice.tax_amount,
                invoice.currency,
            ),
            styles=styles,
        )
    )

    rows.append(
        [
            Paragraph(
                "GRAND TOTAL",
                styles["grand_total_label"],
            ),
            Paragraph(
                _money(
                    invoice.grand_total,
                    invoice.currency,
                ),
                styles["grand_total_value"],
            ),
        ]
    )

    table = Table(
        rows,
        colWidths=[
            42 * mm,
            48 * mm,
        ],
        hAlign="RIGHT",
    )

    table.setStyle(
        TableStyle(
            [
                (
                    "LINEABOVE",
                    (0, -1),
                    (-1, -1),
                    1.3,
                    BLAZELINE_ORANGE,
                ),
                (
                    "TOPPADDING",
                    (0, 0),
                    (-1, -2),
                    3,
                ),
                (
                    "BOTTOMPADDING",
                    (0, 0),
                    (-1, -2),
                    3,
                ),
                (
                    "TOPPADDING",
                    (0, -1),
                    (-1, -1),
                    7,
                ),
                (
                    "BOTTOMPADDING",
                    (0, -1),
                    (-1, -1),
                    7,
                ),
                (
                    "LEFTPADDING",
                    (0, 0),
                    (-1, -1),
                    4,
                ),
                (
                    "RIGHTPADDING",
                    (0, 0),
                    (-1, -1),
                    4,
                ),
            ]
        )
    )

    return table


# ============================================================================
# FOOTER INFORMATION
# ============================================================================


def _build_footer_content(
    *,
    invoice: Invoice,
    styles,
):
    tax_note = (
        "Tax and customer details are reproduced from the information "
        "captured at checkout."
    )

    return [
        Spacer(1, 7 * mm),
        Table(
            [
                [
                    Paragraph(
                        "<b>IMPORTANT</b><br/>"
                        "This is a computer-generated invoice and "
                        "does not require a physical signature.<br/>"
                        f"{_escape_text(tax_note)}",
                        styles["footer"],
                    ),
                    Paragraph(
                        f"<b>{COMPANY_NAME}</b><br/>"
                        f"{_escape_text(COMPANY_ADDRESS_LINE_1)}<br/>"
                        f"{_escape_text(COMPANY_ADDRESS_LINE_2)}<br/>"
                        f"GSTIN: {_escape_text(COMPANY_GSTIN)}<br/>"
                        f"{_escape_text(COMPANY_WEBSITE)}",
                        styles["footer"],
                    ),
                ]
            ],
            colWidths=[
                110 * mm,
                CONTENT_WIDTH - 110 * mm,
            ],
            style=TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, -1),
                        BLAZELINE_LIGHT,
                    ),
                    (
                        "BOX",
                        (0, 0),
                        (-1, -1),
                        0.5,
                        BLAZELINE_BORDER,
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "TOP",
                    ),
                    (
                        "LEFTPADDING",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                    (
                        "RIGHTPADDING",
                        (0, 0),
                        (-1, -1),
                        9,
                    ),
                    (
                        "TOPPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                    (
                        "BOTTOMPADDING",
                        (0, 0),
                        (-1, -1),
                        7,
                    ),
                ]
            ),
        ),
    ]


# ============================================================================
# PUBLIC API
# ============================================================================


def render_invoice_pdf(
    *,
    invoice: Invoice,
) -> bytes:
    """
    Render one canonical BlazeLine invoice into a PDF.

    Responsibilities deliberately limited to:
        - reading invoice data
        - rendering the PDF
        - returning PDF bytes

    This function does NOT:
        - modify payment/order state
        - upload to Cloudinary
        - send email
        - send WhatsApp
        - create invoices
    """

    if not invoice.pk:
        raise ValidationError(
            "Invoice must be saved before generating its PDF."
        )

    if not invoice.order_id:
        raise ValidationError(
            "Invoice is not linked to an order."
        )

    items = list(
        InvoiceItem.objects
        .filter(invoice=invoice)
        .order_by("id")
    )

    if not items:
        raise ValidationError(
            "Cannot generate invoice PDF without invoice items."
        )

    regular_font, bold_font = _register_fonts()

    logo_path = _resolve_logo_path()

    if not logo_path.is_file():
        raise ValidationError(
            "BlazeLine invoice logo not found. "
            f"Expected: {logo_path}"
        )

    styles = _build_styles(
        regular_font,
        bold_font,
    )

    buffer = BytesIO()

    document = InvoiceDocumentTemplate(
        buffer,
        title=f"BlazeLine Invoice {invoice.invoice_number}",
        author=COMPANY_NAME,
        subject=f"Tax Invoice {invoice.invoice_number}",
        creator="BlazeLine Invoice System",
    )

    story = []

    # Header
    story.extend(
        _build_header(
            invoice=invoice,
            styles=styles,
        )
    )

    story.append(
        Spacer(1, 6 * mm)
    )

    # Customer + payment
    story.append(
        _build_party_information(
            invoice=invoice,
            styles=styles,
        )
    )

    story.append(
        Spacer(1, 7 * mm)
    )

    # Items
    story.append(
        Paragraph(
            "ORDER ITEMS",
            styles["section_title"],
        )
    )

    story.append(
        _build_items_table(
            items=items,
            currency=invoice.currency,
            styles=styles,
        )
    )

    story.append(
        Spacer(1, 6 * mm)
    )

    # Totals
    totals_table = _build_totals(
        invoice=invoice,
        styles=styles,
    )

    story.append(
        totals_table
    )

    # Footer information
    story.extend(
        _build_footer_content(
            invoice=invoice,
            styles=styles,
        )
    )

    document.build(story)

    pdf_bytes = buffer.getvalue()

    buffer.close()

    if not pdf_bytes.startswith(b"%PDF"):
        raise ValidationError(
            "Invoice PDF generation produced invalid PDF data."
        )

    if len(pdf_bytes) < 1500:
        raise ValidationError(
            "Generated invoice PDF is unexpectedly small."
        )

    return pdf_bytes