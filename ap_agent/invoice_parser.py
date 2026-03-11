"""Parse vendor emails to extract payment request details."""

import re
from dataclasses import dataclass, field


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
        ]
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

        # Add warnings for missing fields
        if not request.amount:
            request.parse_warnings.append("Could not extract payment amount")
        if not request.routing_number:
            request.parse_warnings.append("Could not extract routing number")
        if not request.account_number:
            request.parse_warnings.append("Could not extract account number")

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
