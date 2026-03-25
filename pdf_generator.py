"""Generate professional PO PDFs using ReportLab."""

from __future__ import annotations

import os
import tempfile

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    HRFlowable,
)

import config
from models import PurchaseOrder


def generate_po_pdf(po: PurchaseOrder) -> str:
    """Generate a PDF for the given PO. Returns the file path."""
    tmp_dir = tempfile.mkdtemp(prefix="po_")
    filename = f"{po.po_number}.pdf"
    filepath = os.path.join(tmp_dir, filename)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    elements = []

    # --- Styles ---
    title_style = ParagraphStyle(
        "POTitle",
        parent=styles["Title"],
        fontSize=22,
        spaceAfter=4,
        textColor=colors.HexColor("#1a1a2e"),
    )
    heading_style = ParagraphStyle(
        "POHeading",
        parent=styles["Heading2"],
        fontSize=11,
        textColor=colors.HexColor("#16213e"),
        spaceAfter=2,
    )
    normal_style = ParagraphStyle(
        "PONormal",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
    )
    small_style = ParagraphStyle(
        "POSmall",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.grey,
    )

    # --- Header ---
    company_name = Paragraph(f"<b>{config.COMPANY_NAME}</b>", title_style)
    company_details = Paragraph(
        config.COMPANY_ADDRESS.replace("\n", "<br/>")
        + (f"<br/>{config.COMPANY_EMAIL}" if config.COMPANY_EMAIL else "")
        + (f"<br/>{config.COMPANY_PHONE}" if config.COMPANY_PHONE else ""),
        normal_style,
    )
    po_label = Paragraph("<b>PURCHASE ORDER</b>", ParagraphStyle(
        "POLabel",
        parent=styles["Normal"],
        fontSize=14,
        alignment=2,  # right-aligned
        textColor=colors.HexColor("#1a1a2e"),
    ))
    po_details = Paragraph(
        f"<b>PO #:</b> {po.po_number}<br/>"
        f"<b>Date:</b> {po.date_created}",
        ParagraphStyle("PODetails", parent=normal_style, alignment=2),
    )

    header_table = Table(
        [[company_name, po_label], [company_details, po_details]],
        colWidths=[3.5 * inch, 3.5 * inch],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.15 * inch))
    elements.append(HRFlowable(
        width="100%", thickness=2, color=colors.HexColor("#1a1a2e"),
    ))
    elements.append(Spacer(1, 0.25 * inch))

    # --- Supplier info ---
    elements.append(Paragraph("<b>Supplier</b>", heading_style))
    supplier_info = (
        f"<b>{po.supplier.name}</b><br/>"
        f"{po.supplier.address.replace(chr(10), '<br/>')}"
    )
    if po.supplier.contact_name:
        supplier_info += f"<br/>Attn: {po.supplier.contact_name}"
    if po.supplier.email:
        supplier_info += f"<br/>{po.supplier.email}"
    if po.supplier.phone:
        supplier_info += f"<br/>{po.supplier.phone}"
    elements.append(Paragraph(supplier_info, normal_style))
    elements.append(Spacer(1, 0.25 * inch))

    # --- Line items table ---
    table_header = ["#", "Item", "Description", "Qty", "Unit", "Unit Price", "Total"]
    table_data = [table_header]

    for i, item in enumerate(po.line_items, 1):
        table_data.append([
            str(i),
            Paragraph(item.product_name, normal_style),
            Paragraph(item.description, normal_style),
            f"{item.quantity:g}",
            item.unit,
            f"${item.unit_price:,.2f}",
            f"${item.total:,.2f}",
        ])

    col_widths = [0.35 * inch, 1.8 * inch, 1.8 * inch, 0.6 * inch, 0.5 * inch, 0.9 * inch, 0.9 * inch]
    item_table = Table(table_data, colWidths=col_widths, repeatRows=1)
    item_table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        # Data rows
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        # Grid
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(item_table)
    elements.append(Spacer(1, 0.15 * inch))

    # --- Totals ---
    totals_data = [
        ["", "", "Subtotal:", f"${po.subtotal:,.2f}"],
        ["", "", "Tax:", f"${po.tax:,.2f}"],
        ["", "", "Total:", f"${po.total:,.2f}"],
    ]
    totals_table = Table(
        totals_data,
        colWidths=[2.5 * inch, 2.5 * inch, 1.0 * inch, 1.0 * inch],
    )
    totals_table.setStyle(TableStyle([
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (2, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEABOVE", (2, -1), (-1, -1), 1.5, colors.HexColor("#1a1a2e")),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 0.35 * inch))

    # --- Payment terms & notes ---
    if po.supplier.payment_terms:
        elements.append(Paragraph(
            f"<b>Payment Terms:</b> {po.supplier.payment_terms}",
            normal_style,
        ))
        elements.append(Spacer(1, 0.1 * inch))

    if po.notes:
        elements.append(Paragraph(f"<b>Notes:</b> {po.notes}", normal_style))
        elements.append(Spacer(1, 0.1 * inch))

    # --- Footer ---
    elements.append(Spacer(1, 0.5 * inch))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    elements.append(Spacer(1, 0.1 * inch))
    elements.append(Paragraph(
        f"Generated by {config.COMPANY_NAME} PO System",
        small_style,
    ))

    doc.build(elements)
    return filepath
