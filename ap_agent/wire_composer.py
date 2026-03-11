"""Compose wire transfer emails and manage the approval workflow."""

from ap_agent.invoice_parser import PaymentRequest


class WireComposer:
    """Builds wire transfer request emails from payment requests."""

    def __init__(self, config: dict):
        self.wire_template = config.get("wire_email_template", "")
        self.approval_template = config.get("approval_email_template", "")
        self.wire_detail_template = config.get("wire_detail_template", "")
        self.vendor_confirmation_template = config.get(
            "vendor_confirmation_template", ""
        )

    def compose_wire_details(self, requests: list[PaymentRequest]) -> str:
        """Build the wire details section for all payment requests."""
        details = []
        for i, req in enumerate(requests, 1):
            detail = self.wire_detail_template.format(
                wire_number=i,
                vendor_name=req.vendor_name,
                amount=f"{req.amount:,.2f}",
                bank_name=req.bank_name or "N/A",
                routing_number=req.routing_number,
                account_number=req.account_number,
                invoice_reference=req.invoice_reference or "N/A",
                payment_terms=req.payment_terms or "N/A",
                due_date=req.due_date.isoformat() if req.due_date else "N/A",
            )
            details.append(detail)
        return "\n".join(details)

    def compose_approval_email(self, requests: list[PaymentRequest]) -> str:
        """Compose the approval draft email body sent to the approver."""
        wire_details = self.compose_wire_details(requests)
        total_amount = sum(r.amount for r in requests)
        return self.approval_template.format(
            wire_details=wire_details,
            wire_count=len(requests),
            total_amount=f"{total_amount:,.2f}",
        )

    def compose_wire_email(self, requests: list[PaymentRequest]) -> str:
        """Compose the actual wire transfer request email to the bank."""
        wire_details = self.compose_wire_details(requests)
        total_amount = sum(r.amount for r in requests)
        return self.wire_template.format(
            wire_details=wire_details,
            wire_count=len(requests),
            total_amount=f"{total_amount:,.2f}",
        )

    def compose_vendor_confirmation(
        self,
        request: PaymentRequest,
        confirmation_number: str,
        date_processed: str,
    ) -> str:
        """Compose the confirmation email sent back to a vendor."""
        return self.vendor_confirmation_template.format(
            vendor_name=request.vendor_name,
            amount=f"{request.amount:,.2f}",
            confirmation_number=confirmation_number,
            invoice_reference=request.invoice_reference or "N/A",
            date_processed=date_processed,
        )
