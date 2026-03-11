"""Parse vendor emails to extract payment request details."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


@dataclass
class PaymentRequest:
    """Structured payment request extracted from a vendor email."""

    vendor_name: str = ""
    vendor_email: str = ""
    amount: float = 0.0
    bank_name: str = ""
    routing_number: str = ""
    account_number: str = ""
    invoice_reference: str = ""
    email_id: str = ""
    thread_id: str = ""
    raw_body: str = ""
    due_date: date | None = None
    payment_terms: str = ""  # e.g. "Net 30", "Due on receipt"
    invoice_date: date | None = None
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        """Check if all required wire transfer fields are present."""
        return all([
            self.vendor_name,
            self.vendor_email,
            self.amount > 0,
            self.routing_number,
            self.account_number,
        ])

    @property
    def days_until_due(self) -> int | None:
        """Days until payment is due. Negative means overdue."""
        if self.due_date:
            return (self.due_date - date.today()).days
        return None

    def summary(self) -> str:
        """Human-readable summary of this payment request."""
        status = "COMPLETE" if self.is_complete else "INCOMPLETE"
        lines = [
            f"[{status}] Payment Request from {self.vendor_name}",
            f"  Email: {self.vendor_email}",
            f"  Amount: ${self.amount:,.2f}",
            f"  Bank: {self.bank_name}",
            f"  Routing: {self.routing_number}",
            f"  Account: {self.account_number}",
            f"  Invoice/Ref: {self.invoice_reference}",
            f"  Terms: {self.payment_terms or 'N/A'}",
            f"  Due Date: {self.due_date.isoformat() if self.due_date else 'N/A'}",
        ]
        if self.days_until_due is not None:
            if self.days_until_due < 0:
                lines.append(f"  *** OVERDUE by {abs(self.days_until_due)} days ***")
            else:
                lines.append(f"  Due in {self.days_until_due} days")
        if self.parse_warnings:
            lines.append(f"  Warnings: {'; '.join(self.parse_warnings)}")
        return "\n".join(lines)


class InvoiceParser:
    """Extract payment details from vendor emails."""

    # Common patterns for extracting payment information
    AMOUNT_PATTERNS = [
        r"\$\s?([\d,]+\.?\d*)",
        r"(?:amount|total|payment|due|balance)[:\s]*\$?\s?([\d,]+\.?\d*)",
        r"([\d,]+\.?\d*)\s*(?:USD|dollars)",
    ]

    ROUTING_PATTERNS = [
        r"(?:routing|aba|transit)\s*(?:number|no|#)?[:\s]*(\d{9})",
        r"(\d{9})\s*(?:routing|aba)",
    ]

    ACCOUNT_PATTERNS = [
        r"(?:account)\s*(?:number|no|#)?[:\s]*(\d{4,17})",
        r"(?:acct)\s*(?:number|no|#)?[:\s]*(\d{4,17})",
    ]

    BANK_NAME_PATTERNS = [
        r"(?:bank|financial institution|bank name)[:\s]*([A-Za-z\s&.]+?)(?:\n|$|,)",
    ]

    INVOICE_PATTERNS = [
        r"(?:invoice|inv|reference|ref)\s*(?:number|no|#)?[:\s]*([\w\-]+)",
    ]

    # Payment terms patterns (Net 30, Net 60, Due on receipt, etc.)
    TERMS_PATTERNS = [
        r"(net\s*\d+)",
        r"(due\s+on\s+receipt)",
        r"(due\s+upon\s+receipt)",
        r"(?:payment\s+)?(?:terms?)[:\s]*(net\s*\d+|due\s+on\s+receipt|\d+\s*days?)",
        r"(payable\s+within\s+\d+\s*days?)",
    ]

    # Due date patterns (explicit dates)
    DUE_DATE_PATTERNS = [
        # "Due date: March 15, 2026" or "Due: 03/15/2026"
        r"(?:due\s*date|payment\s*due|due\s*by|pay\s*by)[:\s]*(\w+\s+\d{1,2},?\s*\d{4})",
        r"(?:due\s*date|payment\s*due|due\s*by|pay\s*by)[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
        r"(?:due\s*date|payment\s*due|due\s*by|pay\s*by)[:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})",
    ]

    # Invoice date patterns
    INVOICE_DATE_PATTERNS = [
        r"(?:invoice\s*date|dated?|issued?)[:\s]*(\w+\s+\d{1,2},?\s*\d{4})",
        r"(?:invoice\s*date|dated?|issued?)[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
        r"(?:invoice\s*date|dated?|issued?)[:\s]*(\d{4}[/\-]\d{1,2}[/\-]\d{1,2})",
    ]

    # Common date formats for parsing
    DATE_FORMATS = [
        "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y",
        "%Y/%m/%d", "%Y-%m-%d",
        "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
        "%d %B %Y", "%d %b %Y",
    ]

    def parse_email(self, email: dict) -> PaymentRequest:
        """Parse a single email into a PaymentRequest.

        Args:
            email: Dict with keys: id, thread_id, subject, from, body, date.

        Returns:
            PaymentRequest with extracted (or empty) fields.
        """
        body = email.get("body", "")
        subject = email.get("subject", "")
        text = f"{subject}\n{body}"

        request = PaymentRequest(
            vendor_email=self._extract_email_address(email.get("from", "")),
            vendor_name=self._extract_sender_name(email.get("from", "")),
            email_id=email.get("id", ""),
            thread_id=email.get("thread_id", ""),
            raw_body=body,
        )

        request.amount = self._extract_first_match(self.AMOUNT_PATTERNS, text, float)
        request.routing_number = self._extract_first_match(
            self.ROUTING_PATTERNS, text, str
        )
        request.account_number = self._extract_first_match(
            self.ACCOUNT_PATTERNS, text, str
        )
        request.bank_name = self._extract_first_match(
            self.BANK_NAME_PATTERNS, text, str
        )
        request.invoice_reference = self._extract_first_match(
            self.INVOICE_PATTERNS, text, str
        )

        # Extract payment terms and dates
        request.payment_terms = self._extract_first_match(
            self.TERMS_PATTERNS, text, str
        )
        request.invoice_date = self._extract_date(self.INVOICE_DATE_PATTERNS, text)
        request.due_date = self._extract_date(self.DUE_DATE_PATTERNS, text)

        # If no explicit due date but we have terms + invoice date, calculate it
        if not request.due_date and request.payment_terms and request.invoice_date:
            request.due_date = self._calculate_due_date(
                request.invoice_date, request.payment_terms
            )

        # If no explicit due date but we have terms + email date, use email date
        if not request.due_date and request.payment_terms:
            email_date = self._parse_date_string(email.get("date", ""))
            if email_date:
                request.due_date = self._calculate_due_date(
                    email_date, request.payment_terms
                )

        # Add warnings for missing fields
        if not request.amount:
            request.parse_warnings.append("Could not extract payment amount")
        if not request.routing_number:
            request.parse_warnings.append("Could not extract routing number")
        if not request.account_number:
            request.parse_warnings.append("Could not extract account number")
        if not request.due_date:
            request.parse_warnings.append("Could not determine due date")

        return request

    def parse_emails(self, emails: list[dict]) -> list[PaymentRequest]:
        """Parse multiple emails into PaymentRequests."""
        return [self.parse_email(email) for email in emails]

    def _extract_first_match(self, patterns: list[str], text: str, cast_type: type):
        """Try each pattern and return the first match, cast to the given type."""
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if cast_type == float:
                    value = value.replace(",", "")
                    try:
                        return float(value)
                    except ValueError:
                        continue
                return value
        return cast_type() if cast_type != float else 0.0

    def _extract_email_address(self, from_field: str) -> str:
        """Extract email address from a 'From' header like 'Name <email>'."""
        match = re.search(r"<(.+?)>", from_field)
        if match:
            return match.group(1)
        if "@" in from_field:
            return from_field.strip()
        return from_field

    def _extract_sender_name(self, from_field: str) -> str:
        """Extract display name from a 'From' header."""
        match = re.match(r'^"?([^"<]+)"?\s*<', from_field)
        if match:
            return match.group(1).strip()
        if "@" not in from_field:
            return from_field.strip()
        return from_field.split("@")[0]

    def _extract_date(self, patterns: list[str], text: str) -> date | None:
        """Try each pattern and parse the first matching date string."""
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                parsed = self._parse_date_string(match.group(1))
                if parsed:
                    return parsed
        return None

    def _parse_date_string(self, date_str: str) -> date | None:
        """Try multiple date formats to parse a date string."""
        if not date_str:
            return None
        date_str = date_str.strip().rstrip(",")
        for fmt in self.DATE_FORMATS:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        # Try dateutil as fallback for unusual formats
        try:
            from dateutil import parser as dateutil_parser
            return dateutil_parser.parse(date_str, fuzzy=True).date()
        except (ImportError, ValueError):
            return None

    @staticmethod
    def _calculate_due_date(start_date: date, terms: str) -> date | None:
        """Calculate due date from an invoice date and payment terms string."""
        terms_lower = terms.lower().strip()

        if "receipt" in terms_lower:
            return start_date  # Due immediately

        # Extract number of days from terms like "Net 30", "60 days", etc.
        match = re.search(r"(\d+)", terms_lower)
        if match:
            days = int(match.group(1))
            return start_date + timedelta(days=days)

        return None
