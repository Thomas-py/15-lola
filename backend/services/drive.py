"""
Subida de fotos a Google Drive usando una Service Account.

Configurar en .env:
  GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
  GOOGLE_DRIVE_FOLDER_ID=<id de la carpeta compartida>
"""

import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")


def _service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(file_path: Path, filename: str) -> str:
    """Sube un archivo a Drive y devuelve su file ID."""
    svc = _service()

    metadata = {"name": filename}
    if DRIVE_FOLDER_ID:
        metadata["parents"] = [DRIVE_FOLDER_ID]

    media = MediaFileUpload(str(file_path), resumable=True)
    result = svc.files().create(body=metadata, media_body=media, fields="id").execute()
    return result["id"]
