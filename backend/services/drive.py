"""
Subida de fotos a Google Drive usando una Service Account.

Prioridad de credenciales:
1. GOOGLE_SERVICE_ACCOUNT_B64 — JSON codificado en base64 (recomendado para producción)
2. GOOGLE_SERVICE_ACCOUNT_FILE — ruta al archivo JSON (fallback)
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
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64", "").strip()
    if b64:
        # Agregar padding si falta
        b64 += "=" * (-len(b64) % 4)
        info = json.loads(base64.b64decode(b64).decode("utf-8"))
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        filepath = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        if not Path(filepath).exists():
            raise RuntimeError(f"No se encontró el archivo de credenciales: {filepath}")
        creds = service_account.Credentials.from_service_account_file(filepath, scopes=SCOPES)

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
