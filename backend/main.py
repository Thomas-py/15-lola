import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

MAX_IMAGE_SIZE = 10 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}

# Rate limiting: máx 5 fotos por IP por minuto
RATE_LIMIT_MAX  = 5
RATE_LIMIT_SECS = 60
upload_timestamps: dict[str, list[float]] = defaultdict(list)


# ─── Managers WebSocket ──────────────────────────────────────────────────────

class WSManager:
    def __init__(self, name: str):
        self._clients: list[WebSocket] = []
        self._name = name

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)
        logger.info(f"{self._name} conectado — total: {len(self._clients)}")

    def disconnect(self, ws: WebSocket):
        if ws in self._clients:
            self._clients.remove(ws)
        logger.info(f"{self._name} desconectado — total: {len(self._clients)}")

    async def broadcast(self, payload: dict):
        dead = []
        for client in self._clients:
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)
        for c in dead:
            if c in self._clients:
                self._clients.remove(c)

    async def send(self, ws: WebSocket, payload: dict):
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    @property
    def count(self):
        return len(self._clients)


screen_mgr = WSManager("Pantalla")
mobile_mgr = WSManager("Móvil")

# ─── Estado global ───────────────────────────────────────────────────────────

photo_queue: list[dict] = []
queue_lock  = asyncio.Lock()

# foto que se está mostrando en pantalla ahora mismo
current_photo: dict | None = None


# ─── Drive ───────────────────────────────────────────────────────────────────

async def upload_to_drive_bg(file_path: Path, filename: str):
    try:
        from services.drive import upload_file
        loop = asyncio.get_event_loop()
        file_id = await loop.run_in_executor(None, upload_file, file_path, filename)
        logger.info(f"Drive OK — {filename} (id: {file_id})")
    except Exception as e:
        logger.error(f"Drive FAIL — {filename}: {e}")


# ─── App ─────────────────────────────────────────────────────────────────────

def _load_existing_photos():
    """Carga las fotos ya subidas al arrancar, para que la ruleta las tenga."""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(
        [f for f in UPLOADS_DIR.iterdir() if f.suffix.lower() in exts],
        key=lambda f: f.stat().st_mtime,
    )
    for f in files:
        photo_queue.append({
            "id": f.stem,
            "filename": f.name,
            "url": f"/photos/{f.name}",
            "ts": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    logger.info(f"Fotos cargadas al inicio: {len(photo_queue)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_existing_photos()
    logger.info("QR-15 backend iniciado")
    yield

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


# ─── Upload ──────────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_photo(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    # Rate limiting por IP
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    timestamps = upload_timestamps[ip]
    upload_timestamps[ip] = [t for t in timestamps if now - t < RATE_LIMIT_SECS]
    if len(upload_timestamps[ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(429, "Demasiadas fotos seguidas. Esperá un momento.")
    upload_timestamps[ip].append(now)

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
    logger.info(f"Foto guardada: {filename} ({len(content) // 1024} KB) — IP: {ip}")

    photo = {
        "id": Path(filename).stem,
        "filename": filename,
        "url": f"/photos/{filename}",
        "ts": datetime.now().isoformat(),
    }

    async with queue_lock:
        photo_queue.append(photo)

    await screen_mgr.broadcast({"type": "new_photo", "photo": photo})
    background_tasks.add_task(upload_to_drive_bg, file_path, filename)

    return JSONResponse({"ok": True, "msg": "¡Tu foto ya está en pantalla! 🎉"})


@app.get("/current-photo")
async def get_current_photo():
    if not current_photo:
        return {"photo": None}
    return {"photo": current_photo}


# ─── Misc ────────────────────────────────────────────────────────────────────

@app.post("/roulette/start")
async def start_roulette():
    """El admin dispara la ruleta desde su celular."""
    await screen_mgr.broadcast({"type": "start_roulette"})
    return JSONResponse({"ok": True})


@app.post("/photo/skip")
async def skip_photo():
    """El admin avanza la foto actual en pantalla. La foto queda en la cola para la ruleta."""
    global current_photo
    current_photo = None
    await screen_mgr.broadcast({"type": "skip_photo"})
    return JSONResponse({"ok": True})


@app.get("/queue")
async def get_queue():
    return {"photos": photo_queue}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "screens": screen_mgr.count,
        "mobiles": mobile_mgr.count,
        "photos": len(photo_queue),
    }


# ─── WebSocket pantalla ──────────────────────────────────────────────────────

@app.websocket("/ws/screen")
async def ws_screen(ws: WebSocket):
    global current_photo
    await screen_mgr.connect(ws)
    try:
        await screen_mgr.send(ws, {"type": "init", "photos": []})
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("type") == "now_showing":
                    photo = msg.get("photo")
                    current_photo = photo
                    logger.info(f"Mostrando: {photo['id'][:8] if photo else 'ninguna'}")
            except (json.JSONDecodeError, KeyError):
                pass  # ping de texto plano
    except WebSocketDisconnect:
        screen_mgr.disconnect(ws)


# ─── WebSocket móvil ─────────────────────────────────────────────────────────

@app.websocket("/ws/mobile")
async def ws_mobile(ws: WebSocket):
    await mobile_mgr.connect(ws)
    try:
        if current_photo:
            pid = current_photo["id"]
            await mobile_mgr.send(ws, {
                "type": "now_showing",
                "photo": {**current_photo, "votes": votes.get(pid, {})},
            })
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        mobile_mgr.disconnect(ws)
