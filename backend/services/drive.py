"""
Subida de fotos a Google Drive usando una Service Account.

Las credenciales se leen desde la variable de entorno GOOGLE_SERVICE_ACCOUNT_B64
(JSON de la service account codificado en base64).
"""

import base64
import json
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")


def _service():
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64", "")
    if not b64:
        raise RuntimeError("Falta la variable de entorno GOOGLE_SERVICE_ACCOUNT_B64")

    info = json.loads(base64.b64decode(b64).decode("utf-8"))
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
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
