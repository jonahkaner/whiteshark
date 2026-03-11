# Automated AP Agent — ShopFinder

Fully automated Accounts Payable agent that reads vendor payment requests from Gmail, drafts wire transfers, gets your approval, sends to the bank + Morgan Stanley, and notifies vendors with confirmation numbers. Everything is logged in an Excel spreadsheet.

## How It Works

```
Vendor emails → Parse payment details → Draft wire request
    → Email you for approval → You reply "APPROVED"
    → Send wire to bank + Morgan Stanley
    → Morgan Stanley confirms → Notify vendors with confirmation #
    → Log everything in Excel
```

### Workflow States

| Status | Meaning |
|--------|---------|
| `PENDING_APPROVAL` | Payment request received, logged |
| `AWAITING_APPROVAL` | Draft sent to you for review |
| `APPROVED` | You approved the wire |
| `WIRE_SENT` | Wire request sent to bank + Morgan Stanley |
| `CONFIRMED` | Morgan Stanley confirmed the wire |
| `COMPLETED` | Vendors notified, fully processed |
| `REJECTED` | You rejected the draft |

## Setup

### 1. Prerequisites

- Python 3.11+
- A Google Cloud project with Gmail API enabled

### 2. Enable Gmail API

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Enable the **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable
4. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Download the JSON file and save it as `credentials.json` in the project root

### 3. Install Dependencies

```bash
pip install -r requirements.txt
pip install pyyaml
```

### 4. Configure

Edit `config.yaml` with your actual email addresses:

```yaml
shopfinder_email: "accounts@shopfinder.com"
approver_email: "your-email@shopfinder.com"
bank_email: "wires@yourbank.com"
morgan_stanley_email: "advisor@morganstanley.com"
```

### 5. First Run (Local Authentication)

The first run must be done locally to complete OAuth:

```bash
python main.py
```

This opens a browser for Google OAuth consent. After authenticating, a `token.json` file is created for future runs.

### 6. Test Run

```bash
# Run with verbose logging
python main.py -v

# Run specific phases
python main.py --phase 1  # Only read & parse new emails
python main.py --phase 2  # Only check for approvals
python main.py --phase 3  # Only check for confirmations
```

## Cloud Deployment (Run Every 24 Hours)

### Google Cloud Functions + Cloud Scheduler

**1. Create a GCS bucket for config and data:**

```bash
gsutil mb gs://ap-agent-config
gsutil cp config.yaml gs://ap-agent-config/
```

**2. Store credentials in Secret Manager:**

```bash
gcloud secrets create ap-agent-gmail-credentials \
    --data-file=credentials.json

gcloud secrets create ap-agent-gmail-token \
    --data-file=token.json
```

**3. Deploy the Cloud Function:**

```bash
gcloud functions deploy ap-agent \
    --runtime python311 \
    --trigger-http \
    --entry-point run_ap_agent \
    --timeout 300 \
    --memory 256MB \
    --source . \
    --set-env-vars CONFIG_BUCKET=ap-agent-config,GCP_PROJECT=$(gcloud config get-value project)
```

**4. Set up Cloud Scheduler (runs daily at 8 AM):**

```bash
gcloud scheduler jobs create http ap-agent-daily \
    --schedule "0 8 * * *" \
    --uri "https://REGION-PROJECT.cloudfunctions.net/ap-agent" \
    --http-method POST \
    --oidc-service-account-email YOUR_SA@PROJECT.iam.gserviceaccount.com
```

Replace `REGION`, `PROJECT`, and `YOUR_SA` with your actual values.

## Project Structure

```
whiteshark/
├── ap_agent/
│   ├── __init__.py           # Package init
│   ├── gmail_client.py       # Gmail API (read, send, label)
│   ├── invoice_parser.py     # Parse vendor emails for payment details
│   ├── wire_composer.py      # Compose wire and notification emails
│   ├── ledger.py             # Excel ledger management
│   └── agent.py              # Main orchestrator (state machine)
├── deploy/
│   └── cloud_function.py     # Google Cloud Function entry point
├── main.py                   # Local CLI entry point
├── config.yaml               # Configuration & email templates
├── requirements.txt          # Python dependencies
└── README.md
```

## Customization

All email templates are in `config.yaml`. You can customize:
- **Wire request format** — `wire_email_template` and `wire_detail_template`
- **Approval email** — `approval_email_template`
- **Vendor confirmation** — `vendor_confirmation_template`

Use `{placeholders}` like `{vendor_name}`, `{amount}`, `{confirmation_number}`, etc.

## Approving Wires

When the agent sends you a draft, simply reply to the email with the word **APPROVED** to authorize the wire. Reply with **REJECTED** to deny it.
