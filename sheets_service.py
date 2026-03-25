"""Google Sheets integration for supplier data, products, and PO log."""

from __future__ import annotations

import json
from datetime import date

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config
from models import Supplier, Product, PurchaseOrder

_sheets_service = None
_creds = None


def _get_creds() -> Credentials:
    """Get or refresh Google credentials for Sheets API."""
    global _creds
    if _creds and _creds.valid:
        return _creds

    import os

    if _creds and _creds.expired and _creds.refresh_token:
        _creds.refresh(Request())
    elif os.path.exists(config.GMAIL_TOKEN_PATH):
        _creds = Credentials.from_authorized_user_file(
            config.GMAIL_TOKEN_PATH,
            config.SHEETS_SCOPES + config.GMAIL_SCOPES,
        )
        if _creds.expired and _creds.refresh_token:
            _creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            config.GMAIL_CREDENTIALS_PATH,
            config.SHEETS_SCOPES + config.GMAIL_SCOPES,
        )
        _creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_PATH, "w") as f:
            f.write(_creds.to_json())

    return _creds


def _get_service():
    global _sheets_service
    if _sheets_service is None:
        creds = _get_creds()
        _sheets_service = build("sheets", "v4", credentials=creds)
    return _sheets_service


def _read_sheet(tab_name: str) -> list[list[str]]:
    """Read all rows from a sheet tab. First row is treated as header."""
    service = _get_service()
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=config.GOOGLE_SHEET_ID, range=f"{tab_name}")
        .execute()
    )
    return result.get("values", [])


def _rows_to_dicts(rows: list[list[str]]) -> list[dict[str, str]]:
    """Convert rows (first row = header) to list of dicts."""
    if len(rows) < 2:
        return []
    headers = rows[0]
    result = []
    for row in rows[1:]:
        d = {}
        for i, header in enumerate(headers):
            d[header] = row[i] if i < len(row) else ""
        result.append(d)
    return result


def get_suppliers() -> list[Supplier]:
    """Fetch all suppliers from the Suppliers sheet."""
    rows = _read_sheet(config.SUPPLIERS_SHEET)
    dicts = _rows_to_dicts(rows)
    suppliers = []
    for d in dicts:
        suppliers.append(
            Supplier(
                supplier_id=d.get("supplier_id", ""),
                name=d.get("name", ""),
                contact_name=d.get("contact_name", ""),
                address=d.get("address", ""),
                email=d.get("email", ""),
                phone=d.get("phone", ""),
                payment_terms=d.get("payment_terms", "Net 30"),
                default_currency=d.get("default_currency", "USD"),
            )
        )
    return suppliers


def get_products() -> list[Product]:
    """Fetch all products from the Products sheet."""
    rows = _read_sheet(config.PRODUCTS_SHEET)
    dicts = _rows_to_dicts(rows)
    products = []
    for d in dicts:
        products.append(
            Product(
                product_id=d.get("product_id", ""),
                product_name=d.get("product_name", ""),
                supplier_id=d.get("supplier_id", ""),
                unit=d.get("unit", "ea"),
                default_price=float(d.get("default_price", 0) or 0),
                currency=d.get("currency", ""),
                category=d.get("category", ""),
            )
        )
    return products


def get_next_po_number() -> str:
    """Read and increment the PO counter from the Config sheet."""
    service = _get_service()
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=config.GOOGLE_SHEET_ID, range=f"{config.CONFIG_SHEET}!B1")
        .execute()
    )
    values = result.get("values", [])
    current = int(values[0][0]) if values and values[0] else 0
    next_num = current + 1

    # Write back the incremented counter
    service.spreadsheets().values().update(
        spreadsheetId=config.GOOGLE_SHEET_ID,
        range=f"{config.CONFIG_SHEET}!B1",
        valueInputOption="RAW",
        body={"values": [[next_num]]},
    ).execute()

    year = date.today().year
    return f"{config.PO_PREFIX}-{year}-{next_num:04d}"


def log_po(po: PurchaseOrder) -> None:
    """Append a PO entry to the PO Log sheet."""
    items_json = json.dumps(
        [item.model_dump() for item in po.line_items], default=str
    )
    row = [
        po.po_number,
        po.date_created,
        po.supplier.supplier_id,
        po.supplier.name,
        items_json,
        str(po.subtotal),
        str(po.tax),
        str(po.total),
        po.status,
        po.source_email_id,
    ]
    service = _get_service()
    service.spreadsheets().values().append(
        spreadsheetId=config.GOOGLE_SHEET_ID,
        range=f"{config.PO_LOG_SHEET}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def update_po_status(po_number: str, status: str) -> None:
    """Update the status column of an existing PO in the log."""
    rows = _read_sheet(config.PO_LOG_SHEET)
    if len(rows) < 2:
        return

    for i, row in enumerate(rows[1:], start=2):  # 1-indexed, skip header
        if row and row[0] == po_number:
            service = _get_service()
            # Status is column I (index 8, so column 9)
            service.spreadsheets().values().update(
                spreadsheetId=config.GOOGLE_SHEET_ID,
                range=f"{config.PO_LOG_SHEET}!I{i}",
                valueInputOption="RAW",
                body={"values": [[status]]},
            ).execute()
            return
