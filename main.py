#!/usr/bin/env python3
"""
Funner PO Bot — Automated Purchase Order Generation

Monitors a Gmail inbox for natural-language PO requests, parses them with
Claude, looks up supplier/product data from Google Sheets, generates a
professional PDF, and emails it back.

Usage:
    python main.py              # Start polling
    python main.py --once       # Process pending emails once and exit
"""

from __future__ import annotations

import logging
import os
import sys
import time

import config
import gmail_service
import sheets_service
import po_parser
import pdf_generator
from models import LineItem, PurchaseOrder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("po-bot")


def process_email(email_msg: gmail_service.IncomingEmail) -> None:
    """Process a single incoming PO request email."""
    log.info(f"Processing email from {email_msg.sender}: {email_msg.subject}")

    # Mark as processing
    gmail_service.label_email(email_msg.message_id, gmail_service.LABEL_PROCESSING)

    try:
        # Fetch reference data from Sheets
        suppliers = sheets_service.get_suppliers()
        products = sheets_service.get_products()

        if not suppliers:
            raise ValueError(
                "No suppliers found in Google Sheet. "
                "Please add suppliers to the 'Suppliers' tab."
            )

        # Parse the request with Claude
        parsed = po_parser.parse_po_request(email_msg.body, suppliers, products)
        log.info(
            f"Parsed: supplier={parsed.supplier_name}, "
            f"items={len(parsed.items)}, confidence={parsed.confidence}"
        )

        # Find the matched supplier
        supplier = None
        for s in suppliers:
            if s.supplier_id == parsed.supplier_id or s.name == parsed.supplier_name:
                supplier = s
                break

        if not supplier:
            raise ValueError(
                f"Could not match supplier '{parsed.supplier_name}' "
                f"to any known supplier. Known suppliers: "
                f"{', '.join(s.name for s in suppliers)}"
            )

        # Build line items, filling in default prices from product catalog
        line_items = []
        for parsed_item in parsed.items:
            # Try to find matching product for default price
            unit_price = parsed_item.unit_price or 0.0
            unit = parsed_item.unit or "ea"

            if not parsed_item.unit_price:
                for p in products:
                    if (
                        p.product_name.lower() == parsed_item.product_name.lower()
                        and p.supplier_id == supplier.supplier_id
                    ):
                        unit_price = p.default_price
                        if not parsed_item.unit:
                            unit = p.unit
                        break

            quantity = parsed_item.quantity or 0.0
            line_items.append(
                LineItem(
                    product_name=parsed_item.product_name,
                    quantity=quantity,
                    unit=unit,
                    unit_price=unit_price,
                )
            )

        # Generate PO number
        po_number = sheets_service.get_next_po_number()

        # Create the PO
        po = PurchaseOrder(
            po_number=po_number,
            supplier=supplier,
            line_items=line_items,
            notes=parsed.notes,
            status="Draft",
            source_email_id=email_msg.message_id,
        )
        po.compute_totals()

        # Log the PO to sheets (as Draft)
        sheets_service.log_po(po)

        # Generate PDF
        pdf_path = pdf_generator.generate_po_pdf(po)
        log.info(f"Generated PDF: {pdf_path}")

        # Build reply message
        items_summary = "\n".join(
            f"  - {item.product_name}: {item.quantity:g} {item.unit} "
            f"@ ${item.unit_price:,.2f} = ${item.total:,.2f}"
            for item in po.line_items
        )
        ambiguity_note = ""
        if parsed.ambiguities:
            ambiguity_note = (
                "\n\nNotes/Ambiguities:\n"
                + "\n".join(f"  ⚠ {a}" for a in parsed.ambiguities)
            )

        reply_body = (
            f"Here's your purchase order!\n\n"
            f"PO #: {po.po_number}\n"
            f"Supplier: {po.supplier.name}\n"
            f"Items:\n{items_summary}\n"
            f"Total: ${po.total:,.2f}\n"
            f"{ambiguity_note}\n\n"
            f"The PDF is attached. Review and send it to the supplier."
        )

        # Send reply with PDF
        gmail_service.send_po_reply(
            to=email_msg.sender,
            thread_id=email_msg.thread_id,
            subject=email_msg.subject,
            body_text=reply_body,
            pdf_path=pdf_path,
        )

        # Update status
        sheets_service.update_po_status(po_number, "Sent")
        gmail_service.label_email(email_msg.message_id, gmail_service.LABEL_COMPLETED)
        log.info(f"PO {po_number} sent to {email_msg.sender}")

        # Clean up temp PDF
        try:
            os.remove(pdf_path)
            os.rmdir(os.path.dirname(pdf_path))
        except OSError:
            pass

    except Exception as e:
        log.error(f"Error processing email {email_msg.message_id}: {e}")
        gmail_service.label_email(email_msg.message_id, gmail_service.LABEL_ERROR)

        # Send error reply
        try:
            gmail_service.send_po_reply(
                to=email_msg.sender,
                thread_id=email_msg.thread_id,
                subject=email_msg.subject,
                body_text=(
                    f"Sorry, I couldn't generate that PO.\n\n"
                    f"Error: {e}\n\n"
                    f"Please try again with more details, e.g.:\n"
                    f'"Make a PO for 16 kg of fragrance from Giveran"'
                ),
            )
        except Exception as reply_err:
            log.error(f"Could not send error reply: {reply_err}")


def run_once() -> int:
    """Process all pending emails once. Returns number processed."""
    emails = gmail_service.fetch_unread_po_emails()
    if not emails:
        log.info("No new PO requests.")
        return 0

    log.info(f"Found {len(emails)} new email(s)")
    for email_msg in emails:
        process_email(email_msg)

    return len(emails)


def run_polling() -> None:
    """Poll Gmail in a loop."""
    log.info(
        f"PO Bot started. Polling every {config.POLL_INTERVAL_SECONDS}s. "
        f"Press Ctrl+C to stop."
    )
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Polling error: {e}")

        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run_polling()
