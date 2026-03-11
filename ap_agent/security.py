"""Security utilities for the AP Agent.

Handles:
- Email sender verification (header inspection)
- Approval code generation and validation
- State and ledger integrity hashing
- Wire amount limits and anomaly detection
"""

import hashlib
import hmac
import os
import re
import secrets
import string
from datetime import datetime


class ApprovalCodeGenerator:
    """Generate and validate unique approval codes.

    Instead of accepting a plain "APPROVED" reply (which can be spoofed),
    each approval draft includes a unique one-time code the approver must
    reply with.
    """

    CODE_LENGTH = 8

    @staticmethod
    def generate() -> str:
        """Generate a cryptographically random approval code."""
        alphabet = string.ascii_uppercase + string.digits
        # Remove ambiguous characters (0/O, 1/I/L)
        alphabet = alphabet.replace("O", "").replace("0", "")
        alphabet = alphabet.replace("I", "").replace("1", "").replace("L", "")
        return "".join(secrets.choice(alphabet) for _ in range(ApprovalCodeGenerator.CODE_LENGTH))

    @staticmethod
    def is_valid_format(code: str) -> bool:
        """Check if a string looks like a valid approval code."""
        return bool(re.match(r"^[A-HJ-NP-Z2-9]{8}$", code))


class EmailVerifier:
    """Verify email sender authenticity by inspecting headers."""

    # Known legitimate domains for critical senders
    TRUSTED_DOMAINS = set()

    @classmethod
    def configure(cls, approver_email: str, bank_email: str, ms_email: str):
        """Set up trusted domains from config."""
        for email in [approver_email, bank_email, ms_email]:
            domain = email.split("@")[-1].lower()
            cls.TRUSTED_DOMAINS.add(domain)

    @staticmethod
    def extract_domain(email_address: str) -> str:
        """Extract domain from an email address."""
        match = re.search(r"@([\w.-]+)", email_address)
        return match.group(1).lower() if match else ""

    @staticmethod
    def verify_sender_headers(message_headers: dict) -> dict:
        """Inspect email headers for authentication results.

        Returns a dict with verification details.
        """
        auth_results = message_headers.get("authentication-results", "")
        result = {
            "spf": "none",
            "dkim": "none",
            "dmarc": "none",
            "suspicious": False,
            "warnings": [],
        }

        if "spf=pass" in auth_results.lower():
            result["spf"] = "pass"
        elif "spf=fail" in auth_results.lower():
            result["spf"] = "fail"
            result["suspicious"] = True
            result["warnings"].append("SPF check failed — sender may be spoofed")

        if "dkim=pass" in auth_results.lower():
            result["dkim"] = "pass"
        elif "dkim=fail" in auth_results.lower():
            result["dkim"] = "fail"
            result["suspicious"] = True
            result["warnings"].append("DKIM check failed — email may be tampered")

        if "dmarc=pass" in auth_results.lower():
            result["dmarc"] = "pass"
        elif "dmarc=fail" in auth_results.lower():
            result["dmarc"] = "fail"
            result["suspicious"] = True
            result["warnings"].append("DMARC check failed — domain authentication failed")

        return result


class IntegrityChecker:
    """Tamper detection for state and ledger files using HMAC."""

    def __init__(self, secret_key: str | None = None):
        self.secret_key = (secret_key or os.environ.get(
            "AP_INTEGRITY_KEY", "change-this-default-key"
        )).encode()

    def compute_hash(self, file_path: str) -> str:
        """Compute HMAC-SHA256 of a file."""
        h = hmac.new(self.secret_key, digestmod=hashlib.sha256)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def save_hash(self, file_path: str):
        """Save the hash of a file alongside it."""
        hash_value = self.compute_hash(file_path)
        hash_path = file_path + ".hash"
        with open(hash_path, "w") as f:
            f.write(hash_value)

    def verify_hash(self, file_path: str) -> bool:
        """Verify a file hasn't been tampered with."""
        hash_path = file_path + ".hash"
        if not os.path.exists(hash_path):
            return True  # No hash file yet (first run)
        with open(hash_path) as f:
            stored_hash = f.read().strip()
        return hmac.compare_digest(stored_hash, self.compute_hash(file_path))


class WireLimits:
    """Enforce wire transfer limits and detect anomalies."""

    def __init__(
        self,
        max_single_wire: float = 100_000.0,
        max_daily_total: float = 500_000.0,
        max_wires_per_batch: int = 10,
    ):
        self.max_single_wire = max_single_wire
        self.max_daily_total = max_daily_total
        self.max_wires_per_batch = max_wires_per_batch

    def check(self, amounts: list[float], daily_total_so_far: float = 0.0) -> dict:
        """Validate a batch of wire amounts against limits.

        Returns:
            dict with 'approved' (bool) and 'violations' (list of strings).
        """
        violations = []

        # Check batch size
        if len(amounts) > self.max_wires_per_batch:
            violations.append(
                f"Batch contains {len(amounts)} wires "
                f"(limit: {self.max_wires_per_batch})"
            )

        # Check individual amounts
        for i, amount in enumerate(amounts, 1):
            if amount <= 0:
                violations.append(f"Wire #{i}: invalid amount ${amount:,.2f}")
            if amount > self.max_single_wire:
                violations.append(
                    f"Wire #{i}: ${amount:,.2f} exceeds single wire limit "
                    f"of ${self.max_single_wire:,.2f}"
                )

        # Check daily total
        batch_total = sum(amounts)
        new_daily_total = daily_total_so_far + batch_total
        if new_daily_total > self.max_daily_total:
            violations.append(
                f"Daily total would be ${new_daily_total:,.2f} "
                f"(limit: ${self.max_daily_total:,.2f})"
            )

        return {
            "approved": len(violations) == 0,
            "violations": violations,
            "batch_total": batch_total,
            "new_daily_total": new_daily_total,
        }
