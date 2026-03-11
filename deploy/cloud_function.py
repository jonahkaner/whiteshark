"""Google Cloud Function entry point for the AP Agent.

Deployment:
    gcloud functions deploy ap-agent \
        --runtime python311 \
        --trigger-http \
        --entry-point run_ap_agent \
        --timeout 300 \
        --memory 256MB \
        --source . \
        --set-env-vars CONFIG_BUCKET=your-bucket-name

Cloud Scheduler (24-hour trigger):
    gcloud scheduler jobs create http ap-agent-daily \
        --schedule "0 8 * * *" \
        --uri "https://REGION-PROJECT.cloudfunctions.net/ap-agent" \
        --http-method POST \
        --oidc-service-account-email YOUR_SA@PROJECT.iam.gserviceaccount.com
"""

import json
import logging
import os
import tempfile

import yaml
from google.cloud import secretmanager, storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_ap_agent(request):
    """Cloud Function entry point. Triggered by Cloud Scheduler."""
    try:
        # Load config and credentials from Cloud Storage
        bucket_name = os.environ.get("CONFIG_BUCKET", "ap-agent-config")
        config = _load_config_from_gcs(bucket_name)

        # Load OAuth token from Secret Manager
        token_data = _load_secret("ap-agent-gmail-token")
        token_path = _write_temp_file(token_data, "token.json")
        config["token_path"] = token_path

        # Load OAuth credentials from Secret Manager
        creds_data = _load_secret("ap-agent-gmail-credentials")
        creds_path = _write_temp_file(creds_data, "credentials.json")
        config["credentials_path"] = creds_path

        # Download current ledger from GCS (or create new)
        ledger_path = _download_ledger(bucket_name)
        config["ledger_path"] = ledger_path

        # Download state file from GCS
        state_path = _download_state(bucket_name)

        # Run the agent
        from ap_agent.agent import APAgent
        agent = APAgent(config)
        agent.run()

        # Upload updated ledger and state back to GCS
        _upload_file(bucket_name, ledger_path, "ap_ledger.xlsx")
        if os.path.exists("ap_state.json"):
            _upload_file(bucket_name, "ap_state.json", "ap_state.json")

        return json.dumps({"status": "success"}), 200

    except Exception as e:
        logger.exception("AP Agent failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)}), 500


def _load_config_from_gcs(bucket_name: str) -> dict:
    """Load config.yaml from Google Cloud Storage."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob("config.yaml")
    return yaml.safe_load(blob.download_as_text())


def _load_secret(secret_name: str) -> str:
    """Load a secret from Google Secret Manager."""
    project_id = os.environ.get("GCP_PROJECT")
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


def _write_temp_file(content: str, filename: str) -> str:
    """Write content to a temporary file and return the path."""
    path = os.path.join(tempfile.gettempdir(), filename)
    with open(path, "w") as f:
        f.write(content)
    return path


def _download_ledger(bucket_name: str) -> str:
    """Download the Excel ledger from GCS, or return a temp path for new one."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob("ap_ledger.xlsx")
    path = os.path.join(tempfile.gettempdir(), "ap_ledger.xlsx")
    if blob.exists():
        blob.download_to_filename(path)
    return path


def _download_state(bucket_name: str) -> str:
    """Download the state file from GCS."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob("ap_state.json")
    path = "ap_state.json"
    if blob.exists():
        blob.download_to_filename(path)
    return path


def _upload_file(bucket_name: str, local_path: str, remote_name: str):
    """Upload a file to GCS."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(remote_name)
    blob.upload_from_filename(local_path)
