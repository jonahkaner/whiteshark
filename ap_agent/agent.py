"""Main AP Agent orchestrator — state machine that processes all phases."""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from ap_agent.gmail_client import GmailClient
from ap_agent.invoice_parser import InvoiceParser, PaymentRequest
from ap_agent.ledger import Ledger
from ap_agent.security import (
    ApprovalCodeGenerator,
    EmailVerifier,
    IntegrityChecker,
    WireLimits,
)
from ap_agent.wire_composer import WireComposer

logger = logging.getLogger(__name__)

# File to persist state between runs (batch tracking)
STATE_FILE = "ap_state.json"

# Payment timing modes
TIMING_ON_DUE_DATE = "on_due_date"       # Send wire on the due date
TIMING_DAYS_EARLY = "days_early"          # Send wire N days before due date
TIMING_IMMEDIATE = "immediate"            # Send wire as soon as approved


class APAgent:
    """Automated Accounts Payable agent.

    Runs as a state machine across multiple invocations:
      Phase 1: Intake — read vendor emails, parse, log to ledger
      Phase 2: Check Approvals — look for approval replies
      Phase 3: Scheduled Send — send approved wires when timing is right
      Phase 4: Check Confirmations — MS confirmation → notify vendors
    """

    def __init__(self, config: dict):
        self.config = config
        self.gmail = GmailClient(
            config["credentials_path"], config["token_path"]
        )
        self.parser = InvoiceParser()
        self.composer = WireComposer(config)
        self.ledger = Ledger(config["ledger_path"])
        self.integrity = IntegrityChecker(config.get("integrity_key"))
        self.wire_limits = WireLimits(
            max_single_wire=config.get("max_single_wire", 100_000),
            max_daily_total=config.get("max_daily_total", 500_000),
            max_wires_per_batch=config.get("max_wires_per_batch", 10),
        )

        # Payment timing configuration
        self.payment_timing = config.get("payment_timing", TIMING_IMMEDIATE)
        self.days_before_due = config.get("days_before_due", 7)

        # Configure email verification with trusted domains
        EmailVerifier.configure(
            config["approver_email"],
            config["bank_email"],
            config["morgan_stanley_email"],
        )

        # Verify state and ledger integrity before loading
        self._verify_file_integrity()
        self.state = self._load_state()

    def run(self):
        """Execute all phases in order. Each phase is idempotent."""
        logger.info("=== AP Agent Run Started: %s ===", datetime.now().isoformat())

        self.phase_intake()
        self.phase_check_approvals()
        self.phase_send_scheduled_wires()
        self.phase_check_confirmations()

        # Update integrity hashes after run
        self._update_file_integrity()

        logger.info("=== AP Agent Run Complete ===")

    def _verify_file_integrity(self):
        """Check that state and ledger files haven't been tampered with."""
        for path in [STATE_FILE, self.config["ledger_path"]]:
            if os.path.exists(path) and not self.integrity.verify_hash(path):
                logger.critical(
                    "INTEGRITY CHECK FAILED for %s — file may have been tampered with. "
                    "Agent halting.", path
                )
                raise SystemExit(
                    f"Security: integrity check failed for {path}. "
                    "Investigate before restarting."
                )

    def _update_file_integrity(self):
        """Update integrity hashes after a successful run."""
        for path in [STATE_FILE, self.config["ledger_path"]]:
            if os.path.exists(path):
                self.integrity.save_hash(path)

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
                "due_date": req.due_date.isoformat() if req.due_date else None,
                "payment_terms": req.payment_terms or "",
            })
            logger.info(
                "Logged: %s — $%s, due: %s (row %d)",
                req.vendor_name, req.amount,
                req.due_date.isoformat() if req.due_date else "N/A",
                row,
            )

        # Check wire limits
        amounts = [r.amount for r in complete_requests]
        limit_check = self.wire_limits.check(amounts)
        if not limit_check["approved"]:
            logger.warning("Wire limit violations detected:")
            for v in limit_check["violations"]:
                logger.warning("  - %s", v)
            violation_warning = (
                "\nWIRE LIMIT WARNINGS:\n"
                + "\n".join(f"  - {v}" for v in limit_check["violations"])
                + "\n\nReview carefully before approving.\n\n"
            )
        else:
            violation_warning = ""

        # Generate unique approval code
        approval_code = ApprovalCodeGenerator.generate()

        # Compose and send approval email with due date info
        approval_body = self.composer.compose_approval_email(complete_requests)

        # Add due date / scheduling summary
        schedule_summary = self._build_schedule_summary(complete_requests)

        approval_body = (
            violation_warning
            + approval_body
            + schedule_summary
            + f"\n\nTo approve, reply with this code: {approval_code}\n"
            + "(Do NOT just reply 'APPROVED' — use the code above for security.)\n"
        )
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

        # Save batch state
        self.state["pending_batches"].append({
            "batch_id": batch_id,
            "thread_id": sent.get("threadId"),
            "message_id": sent.get("id"),
            "approval_code": approval_code,
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

    def _build_schedule_summary(self, requests: list[PaymentRequest]) -> str:
        """Build a summary of when wires will be sent based on timing config."""
        lines = ["\n--- PAYMENT SCHEDULE ---"]

        timing = self.payment_timing
        if timing == TIMING_IMMEDIATE:
            lines.append("Mode: IMMEDIATE — wires will be sent as soon as approved.")
        elif timing == TIMING_ON_DUE_DATE:
            lines.append("Mode: ON DUE DATE — wires will be held until each due date.")
        elif timing == TIMING_DAYS_EARLY:
            lines.append(
                f"Mode: {self.days_before_due} DAYS EARLY — "
                f"wires will be sent {self.days_before_due} days before due date."
            )

        lines.append("")
        for i, req in enumerate(requests, 1):
            due_str = req.due_date.isoformat() if req.due_date else "No due date found"
            terms_str = req.payment_terms or "N/A"
            send_date = self._calculate_send_date(req.due_date)
            send_str = send_date.isoformat() if send_date else "Immediately on approval"

            days_left = req.days_until_due
            urgency = ""
            if days_left is not None and days_left < 0:
                urgency = f" *** OVERDUE by {abs(days_left)} days ***"
            elif days_left is not None and days_left <= 3:
                urgency = f" *** DUE IN {days_left} DAYS ***"

            lines.append(
                f"  Wire #{i}: {req.vendor_name}\n"
                f"    Terms: {terms_str}\n"
                f"    Due Date: {due_str}{urgency}\n"
                f"    Scheduled Send: {send_str}"
            )

        lines.append("--- END SCHEDULE ---\n")
        return "\n".join(lines)

    def _calculate_send_date(self, due_date: date | None) -> date | None:
        """Calculate when a wire should actually be sent based on timing config."""
        if self.payment_timing == TIMING_IMMEDIATE or due_date is None:
            return None  # Send immediately on approval

        if self.payment_timing == TIMING_ON_DUE_DATE:
            return due_date

        if self.payment_timing == TIMING_DAYS_EARLY:
            send_date = due_date - timedelta(days=self.days_before_due)
            # Don't schedule in the past
            if send_date <= date.today():
                return date.today()
            return send_date

        return None

    def _is_ready_to_send(self, batch_row: dict) -> bool:
        """Check if a payment is ready to send based on timing config."""
        if self.payment_timing == TIMING_IMMEDIATE:
            return True

        due_date_str = batch_row.get("due_date")
        if not due_date_str:
            # No due date found — send immediately
            return True

        due_date = date.fromisoformat(due_date_str)
        send_date = self._calculate_send_date(due_date)

        if send_date is None:
            return True

        return date.today() >= send_date

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
            expected_code = batch.get("approval_code", "")

            for msg in thread_messages:
                if msg["id"] == batch["message_id"]:
                    continue

                sender = self._extract_email(msg.get("from", ""))
                if sender.lower() != self.config["approver_email"].lower():
                    continue

                # Verify email authentication headers
                headers = msg.get("headers", {})
                verification = EmailVerifier.verify_sender_headers(headers)
                if verification["suspicious"]:
                    logger.warning(
                        "SUSPICIOUS approval email detected for batch %s: %s",
                        batch["batch_id"],
                        verification["warnings"],
                    )
                    self.gmail.send_email(
                        to=self.config["approver_email"],
                        subject="[AP Agent] SECURITY ALERT — Suspicious approval attempt",
                        body=(
                            f"A suspicious email was received attempting to approve "
                            f"batch {batch['batch_id']}.\n\n"
                            f"Warnings: {', '.join(verification['warnings'])}\n\n"
                            f"The approval was NOT processed. If this was you, "
                            f"please reply to the original draft with the approval code."
                        ),
                    )
                    continue

                body = msg.get("body", "")

                if expected_code and expected_code in body.upper():
                    logger.info("Batch %s APPROVED (code verified)!", batch["batch_id"])

                    # Update approval in ledger
                    for entry in batch["rows"]:
                        self.ledger.update_approval(entry["row"])

                    if self.payment_timing == TIMING_IMMEDIATE:
                        # Send immediately
                        self._send_wire_request(batch)
                    else:
                        # Schedule for later based on due dates
                        batch["status"] = "APPROVED_SCHEDULED"
                        logger.info(
                            "Batch %s approved and scheduled for timed sending.",
                            batch["batch_id"],
                        )
                    break
                elif "REJECTED" in body.upper() or "DENIED" in body.upper():
                    logger.info("Batch %s REJECTED.", batch["batch_id"])
                    batch["status"] = "REJECTED"
                    for entry in batch["rows"]:
                        self.ledger.update_status(entry["row"], "REJECTED")
                    break

        self._save_state()

    # ── Phase 3: Scheduled Wire Sending ──────────────────────────────

    def phase_send_scheduled_wires(self):
        """Send approved wires when their scheduled send date arrives."""
        logger.info("Phase 3: Checking scheduled wires...")

        scheduled = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] == "APPROVED_SCHEDULED"
        ]

        if not scheduled:
            logger.info("No scheduled wires pending.")
            return

        for batch in scheduled:
            # Check which rows in this batch are ready to send
            ready_rows = [r for r in batch["rows"] if self._is_ready_to_send(r)]
            not_ready = [r for r in batch["rows"] if not self._is_ready_to_send(r)]

            if not ready_rows:
                # Log when the next payment is due
                next_dates = []
                for r in batch["rows"]:
                    if r.get("due_date"):
                        send_date = self._calculate_send_date(
                            date.fromisoformat(r["due_date"])
                        )
                        if send_date:
                            next_dates.append(send_date)
                if next_dates:
                    logger.info(
                        "Batch %s: next send date is %s",
                        batch["batch_id"],
                        min(next_dates).isoformat(),
                    )
                continue

            if not_ready:
                # Split: send ready ones now, keep others scheduled
                logger.info(
                    "Batch %s: %d ready, %d waiting",
                    batch["batch_id"], len(ready_rows), len(not_ready),
                )
                # Create a sub-batch for the ready rows
                ready_batch = {
                    **batch,
                    "rows": ready_rows,
                    "batch_id": batch["batch_id"] + "_partial",
                }
                self._send_wire_request(ready_batch)
                # Keep remaining in schedule
                batch["rows"] = not_ready
            else:
                # All ready — send the full batch
                logger.info("Batch %s: all wires ready to send.", batch["batch_id"])
                self._send_wire_request(batch)

        self._save_state()

    def _send_wire_request(self, batch: dict):
        """Send the wire transfer email to bank and Morgan Stanley."""
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
            self.ledger.update_wire_sent(entry["row"])

    # ── Phase 4: Check Confirmations ─────────────────────────────────

    def phase_check_confirmations(self):
        """Check for Morgan Stanley wire confirmation replies."""
        logger.info("Phase 4: Checking for wire confirmations...")

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
        active = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] not in ("COMPLETED", "REJECTED")
        ]
        completed = [
            b for b in self.state.get("pending_batches", [])
            if b["status"] in ("COMPLETED", "REJECTED")
        ][-50:]

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
