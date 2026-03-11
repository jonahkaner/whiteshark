"""Main AP Agent orchestrator — state machine that processes all phases."""

import json
import logging
import os
import re
from datetime import datetime

from ap_agent.gmail_client import GmailClient
from ap_agent.invoice_parser import InvoiceParser, PaymentRequest
from ap_agent.ledger import Ledger
from ap_agent.wire_composer import WireComposer

logger = logging.getLogger(__name__)

# File to persist state between runs (batch tracking)
STATE_FILE = "ap_state.json"


class APAgent:
    """Automated Accounts Payable agent.

    Runs as a state machine across multiple invocations:
      Phase 1: Intake — read vendor emails, parse, log to ledger
      Phase 2: Draft — compose wire request, email approver for approval
      Phase 3: Send — on approval, send wire to bank + Morgan Stanley
      Phase 4: Confirm — on MS confirmation, notify vendors, mark complete
    """

    def __init__(self, config: dict):
        self.config = config
        self.gmail = GmailClient(
            config["credentials_path"], config["token_path"]
        )
        self.parser = InvoiceParser()
        self.composer = WireComposer(config)
        self.ledger = Ledger(config["ledger_path"])
        self.state = self._load_state()

    def run(self):
        """Execute all phases in order. Each phase is idempotent."""
        logger.info("=== AP Agent Run Started: %s ===", datetime.now().isoformat())

        self.phase_intake()
        self.phase_check_approvals()
        self.phase_check_confirmations()

        logger.info("=== AP Agent Run Complete ===")

    # ── Phase 1: Intake ──────────────────────────────────────────────

    def phase_intake(self):
        """Read new vendor emails, parse payment details, draft approval."""
        logger.info("Phase 1: Checking for new vendor payment requests...")

        emails = self.gmail.get_unread_emails(
            label=self.config.get("watch_label", "INBOX")
        )

        if not emails:
            logger.info("No new payment request emails found.")
            return

        # Filter out emails from the approver, bank, or Morgan Stanley
        # (those are handled in other phases)
        known_addresses = {
            self.config["approver_email"].lower(),
            self.config["bank_email"].lower(),
            self.config["morgan_stanley_email"].lower(),
        }
        vendor_emails = [
            e for e in emails
            if self._extract_email(e.get("from", "")).lower() not in known_addresses
        ]

        if not vendor_emails:
            logger.info("No vendor emails in this batch (all from known addresses).")
            return

        # Parse payment requests
        requests = self.parser.parse_emails(vendor_emails)
        complete_requests = [r for r in requests if r.is_complete]
        incomplete_requests = [r for r in requests if not r.is_complete]

        if incomplete_requests:
            logger.warning(
                "%d emails could not be fully parsed:", len(incomplete_requests)
            )
            for r in incomplete_requests:
                logger.warning("  %s", r.summary())

        if not complete_requests:
            logger.info("No complete payment requests to process.")
            # Mark emails as read even if incomplete
            for email in vendor_emails:
                self.gmail.mark_as_read(email["id"])
                self.gmail.add_label(
                    email["id"], self.config.get("processed_label", "AP_PROCESSED")
                )
            return

        # Log to ledger and track rows
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_rows = []
        for req in complete_requests:
            row = self.ledger.log_payment_request(req)
            batch_rows.append({
                "row": row,
                "vendor_name": req.vendor_name,
                "vendor_email": req.vendor_email,
                "amount": req.amount,
                "invoice_reference": req.invoice_reference,
                "routing_number": req.routing_number,
                "account_number": req.account_number,
                "bank_name": req.bank_name,
            })
            logger.info("Logged: %s — $%s (row %d)", req.vendor_name, req.amount, row)

        # Compose and send approval email
        approval_body = self.composer.compose_approval_email(complete_requests)
        total_amount = sum(r.amount for r in complete_requests)
        subject = (
            f"[AP Agent] Wire Draft for Approval — "
            f"{len(complete_requests)} wire(s), ${total_amount:,.2f}"
        )

        sent = self.gmail.send_email(
            to=self.config["approver_email"],
            subject=subject,
            body=approval_body,
        )
        logger.info("Approval email sent to %s (thread: %s)",
                     self.config["approver_email"], sent.get("threadId"))

        # Update ledger status
        for entry in batch_rows:
            self.ledger.update_status(entry["row"], "AWAITING_APPROVAL")

        # Save batch state for tracking approval replies
        self.state["pending_batches"].append({
            "batch_id": batch_id,
            "thread_id": sent.get("threadId"),
            "message_id": sent.get("id"),
            "rows": batch_rows,
            "status": "AWAITING_APPROVAL",
        })
        self._save_state()

        # Mark vendor emails as processed
        for email in vendor_emails:
            self.gmail.mark_as_read(email["id"])
            self.gmail.add_label(
                email["id"], self.config.get("processed_label", "AP_PROCESSED")
            )

        logger.info("Phase 1 complete: %d requests batched, awaiting approval.",
                     len(complete_requests))

    # ── Phase 2: Check Approvals ─────────────────────────────────────

    def phase_check_approvals(self):
        """Check if the approver has replied to any pending drafts."""
        logger.info("Phase 2: Checking for approval replies...")

        pending = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] == "AWAITING_APPROVAL"
        ]

        if not pending:
            logger.info("No batches awaiting approval.")
            return

        for batch in pending:
            thread_messages = self.gmail.get_replies_to(batch["thread_id"])

            # Look for a reply from the approver containing "APPROVED"
            for msg in thread_messages:
                if msg["id"] == batch["message_id"]:
                    continue  # skip our own sent message

                sender = self._extract_email(msg.get("from", ""))
                if sender.lower() != self.config["approver_email"].lower():
                    continue

                body = msg.get("body", "").upper()
                if "APPROVED" in body:
                    logger.info("Batch %s APPROVED!", batch["batch_id"])
                    self._send_wire_request(batch)
                    break
                elif "REJECTED" in body or "DENIED" in body:
                    logger.info("Batch %s REJECTED.", batch["batch_id"])
                    batch["status"] = "REJECTED"
                    for entry in batch["rows"]:
                        self.ledger.update_status(entry["row"], "REJECTED")
                    break

        self._save_state()

    def _send_wire_request(self, batch: dict):
        """Send the wire transfer email to bank and Morgan Stanley."""
        # Reconstruct PaymentRequests from batch data
        requests = []
        for entry in batch["rows"]:
            req = PaymentRequest(
                vendor_name=entry["vendor_name"],
                vendor_email=entry["vendor_email"],
                amount=entry["amount"],
                routing_number=entry["routing_number"],
                account_number=entry["account_number"],
                bank_name=entry.get("bank_name", ""),
                invoice_reference=entry.get("invoice_reference", ""),
            )
            requests.append(req)

        wire_body = self.composer.compose_wire_email(requests)
        total_amount = sum(r.amount for r in requests)
        subject = (
            f"Wire Transfer Request — "
            f"{len(requests)} wire(s), ${total_amount:,.2f}"
        )

        # Send to bank
        bank_sent = self.gmail.send_email(
            to=self.config["bank_email"],
            subject=subject,
            body=wire_body,
        )
        logger.info("Wire request sent to bank: %s", self.config["bank_email"])

        # Send to Morgan Stanley
        ms_sent = self.gmail.send_email(
            to=self.config["morgan_stanley_email"],
            subject=subject,
            body=wire_body,
        )
        logger.info("Wire request sent to Morgan Stanley: %s",
                     self.config["morgan_stanley_email"])

        # Update state and ledger
        batch["status"] = "WIRE_SENT"
        batch["bank_thread_id"] = bank_sent.get("threadId")
        batch["ms_thread_id"] = ms_sent.get("threadId")
        batch["ms_message_id"] = ms_sent.get("id")

        for entry in batch["rows"]:
            self.ledger.update_approval(entry["row"])
            self.ledger.update_wire_sent(entry["row"])

    # ── Phase 3: Check Confirmations ─────────────────────────────────

    def phase_check_confirmations(self):
        """Check for Morgan Stanley wire confirmation replies."""
        logger.info("Phase 3: Checking for wire confirmations...")

        sent_batches = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] == "WIRE_SENT"
        ]

        if not sent_batches:
            logger.info("No batches awaiting wire confirmation.")
            return

        for batch in sent_batches:
            ms_thread_id = batch.get("ms_thread_id")
            if not ms_thread_id:
                continue

            thread_messages = self.gmail.get_replies_to(ms_thread_id)

            for msg in thread_messages:
                if msg["id"] == batch.get("ms_message_id"):
                    continue

                sender = self._extract_email(msg.get("from", ""))
                if sender.lower() != self.config["morgan_stanley_email"].lower():
                    continue

                # Extract confirmation number(s) from the reply
                body = msg.get("body", "")
                confirmation_numbers = self._extract_confirmation_numbers(body)

                if confirmation_numbers:
                    logger.info(
                        "Confirmation received for batch %s: %s",
                        batch["batch_id"],
                        confirmation_numbers,
                    )
                    self._notify_vendors(batch, confirmation_numbers)
                    batch["status"] = "COMPLETED"
                    break

        self._save_state()

    def _extract_confirmation_numbers(self, body: str) -> list[str]:
        """Extract wire confirmation numbers from Morgan Stanley's reply."""
        patterns = [
            r"(?:confirmation|conf|wire)\s*(?:number|no|#|:)\s*[:\s]*([A-Z0-9\-]+)",
            r"#\s*([A-Z0-9\-]{6,})",
            r"(?:ref|reference)\s*(?:number|no|#)?[:\s]*([A-Z0-9\-]+)",
        ]
        numbers = []
        for pattern in patterns:
            matches = re.findall(pattern, body, re.IGNORECASE)
            numbers.extend(matches)

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for n in numbers:
            if n not in seen:
                seen.add(n)
                unique.append(n)
        return unique

    def _notify_vendors(self, batch: dict, confirmation_numbers: list[str]):
        """Send confirmation emails to each vendor in the batch."""
        date_processed = datetime.now().strftime("%Y-%m-%d")

        for i, entry in enumerate(batch["rows"]):
            # Assign confirmation numbers round-robin if fewer than vendors
            conf_num = (
                confirmation_numbers[i]
                if i < len(confirmation_numbers)
                else confirmation_numbers[-1] if confirmation_numbers else "N/A"
            )

            req = PaymentRequest(
                vendor_name=entry["vendor_name"],
                vendor_email=entry["vendor_email"],
                amount=entry["amount"],
                invoice_reference=entry.get("invoice_reference", ""),
            )

            body = self.composer.compose_vendor_confirmation(
                req, conf_num, date_processed
            )

            self.gmail.send_email(
                to=entry["vendor_email"],
                subject=f"Payment Confirmation — {entry.get('invoice_reference', 'Wire Transfer')}",
                body=body,
            )
            logger.info(
                "Vendor notified: %s (conf: %s)", entry["vendor_name"], conf_num
            )

            # Update ledger
            self.ledger.update_confirmation(entry["row"], conf_num)
            self.ledger.update_vendor_notified(entry["row"])

    # ── State Persistence ────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load persisted state from disk."""
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
        return {"pending_batches": []}

    def _save_state(self):
        """Save state to disk."""
        # Clean up completed/rejected batches older than 30 days
        active = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] not in ("COMPLETED", "REJECTED")
        ]
        # Keep recent completed ones for reference
        completed = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] in ("COMPLETED", "REJECTED")
        ][-50:]  # keep last 50

        self.state["pending_batches"] = active + completed

        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    @staticmethod
    def _extract_email(from_field: str) -> str:
        """Extract email address from a From header."""
        match = re.search(r"<(.+?)>", from_field)
        if match:
            return match.group(1)
        return from_field.strip()
