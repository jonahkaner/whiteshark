# Funner PO Bot — Setup Guide

## Overview

Email a Gmail address with something like:
> "Make a PO for 16 kg of fragrance from Giveran"

The bot parses the request, looks up supplier info from Google Sheets, generates a professional PDF, and emails it back to you.

## Prerequisites

- Python 3.10+
- A Google account (Gmail + Google Sheets)
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))

## Step 1: Google Cloud Setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g., "Funner PO Bot")
3. Enable these APIs:
   - **Gmail API**
   - **Google Sheets API**
4. Create **OAuth 2.0 credentials**:
   - Go to APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Download the JSON file and save it as `credentials.json` in this project folder

## Step 2: Google Sheets Setup

1. Create a new Google Sheet
2. Create these 4 tabs (exact names matter):

### Tab: `Suppliers`
| supplier_id | name | contact_name | address | email | phone | payment_terms | default_currency |
|---|---|---|---|---|---|---|---|
| GIVERAN | Giveran Fragrances | Jane Smith | 123 Scent Blvd, Paris | jane@giveran.com | +33-1-2345 | Net 30 | EUR |

### Tab: `Products`
| product_id | product_name | supplier_id | unit | default_price | currency | category |
|---|---|---|---|---|---|---|
| FRAG-001 | Fragrance | GIVERAN | kg | 45.00 | EUR | Raw Materials |

### Tab: `PO Log`
Add this header row (the bot fills in data):
| po_number | date_created | supplier_id | supplier_name | items_json | subtotal | tax | total | status | source_email_id |
|---|---|---|---|---|---|---|---|---|---|

### Tab: `Config`
| Setting | Value |
|---|---|
| next_po_number | 0 |

3. Copy the **Sheet ID** from the URL: `https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit`

## Step 3: Environment Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in environment variables
cp .env.example .env
```

Edit `.env` with your values:
```
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_SHEET_ID=your-sheet-id
COMPANY_NAME=Funner
COMPANY_ADDRESS=Your Address Line 1\nCity, State ZIP
COMPANY_EMAIL=purchasing@funner.com
```

## Step 4: First Run (OAuth)

```bash
python main.py --once
```

On first run, a browser window opens for Google OAuth. Sign in and grant access to Gmail and Sheets. A `token.json` file is saved for future runs.

## Step 5: Start the Bot

```bash
# Poll continuously (checks every 60 seconds)
python main.py

# Or process once and exit
python main.py --once
```

## Usage

Send an email to the Gmail account with your PO request. Examples:

- "Make a PO for 16 kg of fragrance from Giveran"
- "Need 500 units of packaging from AcmePack, and 200 labels from LabelCo"
- "Order 3 pallets of raw cocoa butter from TropSource at $12/kg"

The bot will:
1. Parse your request using Claude
2. Match suppliers and products from your Google Sheet
3. Generate a professional PDF
4. Reply to your email with the PDF attached

## Running as a Service (Optional)

To keep the bot running 24/7, you can use systemd, Docker, or a cloud service:

### Systemd (Linux)
```bash
sudo tee /etc/systemd/system/po-bot.service << EOF
[Unit]
Description=Funner PO Bot

[Service]
WorkingDirectory=/path/to/whiteshark
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now po-bot
```

### Google Cloud Run (serverless)
For zero-cost-when-idle hosting, deploy as a Cloud Run job triggered by Cloud Scheduler every minute.

## Troubleshooting

- **"No suppliers found"** — Make sure your Google Sheet has the `Suppliers` tab with data
- **OAuth errors** — Delete `token.json` and re-run to re-authenticate
- **PDF looks wrong** — Check that `COMPANY_NAME` and `COMPANY_ADDRESS` are set in `.env`
- **Supplier not matched** — Add the supplier to the `Suppliers` tab in Google Sheets
