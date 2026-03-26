import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


# ─── Conexiones WebSocket ────────────────────────────────────────────────────

class ScreenManager:
    def __init__(self):
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)
        logger.info(f"Pantalla conectada — total: {len(self._clients)}")

    def disconnect(self, ws: WebSocket):
        self._clients.remove(ws)
        logger.info(f"Pantalla desconectada — total: {len(self._clients)}")

    async def broadcast(self, payload: dict):
        dead = []
        for client in self._clients:
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.remove(client)

    @property
    def count(self):
        return len(self._clients)


manager = ScreenManager()
photo_queue: list[dict] = []
queue_lock = asyncio.Lock()


# ─── Subida a Drive ──────────────────────────────────────────────────────────

async def upload_to_drive_bg(file_path: Path, filename: str):
    try:
        from services.drive import upload_file
        loop = asyncio.get_event_loop()
        file_id = await loop.run_in_executor(None, upload_file, file_path, filename)
        logger.info(f"Drive OK — {filename} (id: {file_id})")
    except Exception as e:
        logger.error(f"Drive FAIL — {filename}: {e}")


# ─── App ─────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("QR-15 backend iniciado")
    yield
    logger.info("QR-15 backend detenido")

app = FastAPI(title="QR-15", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", "/frontend"))

app.mount("/photos", StaticFiles(directory=str(UPLOADS_DIR)), name="photos")
app.mount("/mobile", StaticFiles(directory=str(FRONTEND_DIR / "mobile"), html=True), name="mobile")
app.mount("/screen", StaticFiles(directory=str(FRONTEND_DIR / "screen"), html=True), name="screen")


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_photo(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    content_type = file.content_type or ""
    if content_type not in ALLOWED_TYPES and not content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes.")

    content = await file.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(400, "La imagen supera el límite de 10 MB.")

    raw_ext = (file.filename or "foto.jpg").rsplit(".", 1)[-1].lower()
    ext = raw_ext if raw_ext in ("jpg", "jpeg", "png", "webp", "heic") else "jpg"
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
    file_path = UPLOADS_DIR / filename

    file_path.write_bytes(content)
    logger.info(f"Foto guardada: {filename} ({len(content) // 1024} KB)")

    photo = {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "url": f"/photos/{filename}",
        "ts": datetime.now().isoformat(),
    }

    async with queue_lock:
        photo_queue.append(photo)

    await manager.broadcast({"type": "new_photo", "photo": photo})
    background_tasks.add_task(upload_to_drive_bg, file_path, filename)

    return JSONResponse({"ok": True, "msg": "¡Tu foto ya está en pantalla! 🎉"})


@app.get("/queue")
async def get_queue():
    """Fallback de polling para pantallas sin WebSocket."""
    return {"photos": photo_queue}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "screens": manager.count,
        "photos_in_queue": len(photo_queue),
    }


@app.websocket("/ws/screen")
async def ws_screen(ws: WebSocket):
    await manager.connect(ws)
    try:
        # Enviar cola actual al conectar
        await ws.send_json({"type": "init", "photos": photo_queue})
        while True:
            # Mantener viva la conexión; el cliente puede enviar pings
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
