import os
from dotenv import load_dotenv

load_dotenv()

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Google Sheets
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

# Gmail
GMAIL_CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
GMAIL_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "token.json")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# Polling
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Company info
COMPANY_NAME = os.getenv("COMPANY_NAME", "Funner")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "").replace("\\n", "\n")
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "")
COMPANY_PHONE = os.getenv("COMPANY_PHONE", "")

# PO numbering
PO_PREFIX = os.getenv("PO_PREFIX", "PO")

# Sheets tab names
SUPPLIERS_SHEET = "Suppliers"
PRODUCTS_SHEET = "Products"
PO_LOG_SHEET = "PO Log"
CONFIG_SHEET = "Config"
