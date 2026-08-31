"""Receipt PDFs.

Rendered from the frozen snapshot on the Receipt row, never from live figures,
so a receipt downloaded a year from now shows what the transfer actually did.
Drawn on the same palette and type as the screen version.
"""
from __future__ import annotations

import io
import logging

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas

logger = logging.getLogger(__name__)

INK = HexColor("#1A1816")
PAPER = HexColor("#FAF7F2")
OCHRE = HexColor("#C67139")
OCHRE_TINT = HexColor("#FFF2EB")
MUTED = HexColor("#6F675C")
FAINT = HexColor("#A19786")
HAIRLINE = HexColor("#E8E2D8")

ROWS = [
    ("Date", "date"),
    ("Customer", "customer"),
    ("Type", "route"),
    ("Amount sent", "amount_sent"),
    ("Exchange rate", "exchange_rate"),
    ("Converted", "converted"),
    ("NkenzaPay fee", "fee_amount"),
    ("Payment method", "payment_method"),
    ("Status", "status"),
]


def render_pdf(receipt) -> io.BytesIO:
    data = receipt.snapshot
    buffer = io.BytesIO()
    page = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    page.setFillColor(PAPER)
    page.rect(0, 0, width, height, stroke=0, fill=1)

    left = 24 * mm
    right = width - 24 * mm
    y = height - 30 * mm

    # Brand mark: a circle with its left half filled, drawn rather than
    # imported so the receipt has no external asset dependency.
    page.setStrokeColor(INK)
    page.setLineWidth(1.4)
    page.circle(left + 4 * mm, y, 4 * mm, stroke=1, fill=0)
    page.setFillColor(OCHRE)
    page.wedge(left, y - 4 * mm, left + 8 * mm, y + 4 * mm, 90, 180, stroke=0, fill=1)

    page.setFillColor(INK)
    page.setFont("Helvetica-Bold", 15)
    page.drawString(left + 12 * mm, y - 2 * mm, "NkenzaPay")

    page.setFont("Courier", 9)
    page.setFillColor(MUTED)
    page.drawRightString(right, y - 2 * mm, data.get("reference", receipt.number))

    y -= 18 * mm
    page.setFillColor(INK)
    page.setFont("Helvetica-Bold", 22)
    page.drawString(left, y, "Transaction receipt")

    y -= 6 * mm
    page.setFont("Helvetica", 10)
    page.setFillColor(MUTED)
    page.drawString(left, y, "Cross-border transfer, completed and confirmed by both sides.")

    y -= 10 * mm
    _dashed_rule(page, left, right, y)

    y -= 10 * mm
    page.setFont("Helvetica", 10.5)
    for label, key in ROWS:
        value = str(data.get(key, ""))
        if key == "fee_amount":
            label = f"NkenzaPay fee ({data.get('fee_percent', '')}%)"
        page.setFillColor(MUTED)
        page.drawString(left, y, label)
        page.setFillColor(INK)
        page.setFont("Helvetica-Bold", 10.5)
        page.drawRightString(right, y, value)
        page.setFont("Helvetica", 10.5)
        y -= 9 * mm

    y -= 2 * mm
    box_height = 22 * mm
    page.setFillColor(OCHRE_TINT)
    page.roundRect(left, y - box_height, right - left, box_height, 6 * mm, stroke=0, fill=1)
    page.setFillColor(MUTED)
    page.setFont("Helvetica", 9.5)
    page.drawString(left + 8 * mm, y - 9 * mm, "AMOUNT RECEIVED")
    page.setFillColor(OCHRE)
    page.setFont("Helvetica-Bold", 20)
    page.drawString(left + 8 * mm, y - 17 * mm, str(data.get("amount_received", "")))

    y -= box_height + 14 * mm
    page.setStrokeColor(HAIRLINE)
    page.setLineWidth(0.8)
    page.line(left, y, right, y)

    y -= 8 * mm
    page.setFillColor(FAINT)
    page.setFont("Helvetica", 8.5)
    page.drawString(left, y, "The amount received is the converted amount after the "
                             "NkenzaPay charge, as shown when the order was created.")
    y -= 5 * mm
    page.drawString(left, y, "Payment execution is performed through authorised "
                             "financial partners.")
    y -= 5 * mm
    page.setFont("Courier", 8)
    page.drawString(left, y, f"Receipt {receipt.number} · generated "
                             f"{receipt.generated_at:%d %B %Y %H:%M} UTC")

    page.showPage()
    page.save()
    buffer.seek(0)
    return buffer


def _dashed_rule(page, x1, x2, y):
    page.setStrokeColor(HAIRLINE)
    page.setLineWidth(1)
    page.setDash(3, 3)
    page.line(x1, y, x2, y)
    page.setDash()


def queue_pdf(receipt):
    """Build and store the PDF. Runs on a worker when one is configured."""
    try:
        from .tasks import build_receipt_pdf

        build_receipt_pdf.delay(receipt.pk)
    except Exception as exc:  # noqa: BLE001 - a missing broker is not a failed transfer
        logger.info("Building receipt %s inline: %s", receipt.number, exc)
        build_and_store(receipt)


def build_and_store(receipt):
    from nkenzapay.common.storage import storage

    buffer = render_pdf(receipt)
    key = f"receipts/{receipt.number}.pdf"
    storage().save_bytes(key, buffer.getvalue(), "application/pdf")
    receipt.pdf_key = key
    receipt.save(update_fields=["pdf_key"])
    return key
