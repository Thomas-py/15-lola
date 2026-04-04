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
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
VOTES_FILE = UPLOADS_DIR / "votes.json"

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

# votos: { photo_id: { "❤️": 3, "🔥": 1, ... } }
votes: dict[str, dict[str, int]] = {}

# deduplicación server-side: { photo_id: set(ip) }
votes_by_ip: dict[str, set[str]] = defaultdict(set)


# ─── Persistencia de votos ───────────────────────────────────────────────────

def load_votes():
    """Carga votos desde disco al iniciar."""
    global votes
    if VOTES_FILE.exists():
        try:
            votes = json.loads(VOTES_FILE.read_text())
            logger.info(f"Votos cargados desde disco: {len(votes)} fotos")
        except Exception as e:
            logger.error(f"Error cargando votos: {e}")

def save_votes():
    """Persiste votos en disco (sin bloquear el event loop)."""
    try:
        VOTES_FILE.write_text(json.dumps(votes))
    except Exception as e:
        logger.error(f"Error guardando votos: {e}")


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_votes()
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
        "id": uuid.uuid4().hex,
        "filename": filename,
        "url": f"/photos/{filename}",
        "ts": datetime.now().isoformat(),
    }

    async with queue_lock:
        photo_queue.append(photo)

    await screen_mgr.broadcast({"type": "new_photo", "photo": photo})
    background_tasks.add_task(upload_to_drive_bg, file_path, filename)

    return JSONResponse({"ok": True, "msg": "¡Tu foto ya está en pantalla! 🎉"})


# ─── Votos ───────────────────────────────────────────────────────────────────

class VoteRequest(BaseModel):
    photo_id: str
    emoji: str

VALID_EMOJIS = {"❤️", "🔥", "😂", "😮"}

@app.post("/vote")
async def cast_vote(request: Request, body: VoteRequest):
    if body.emoji not in VALID_EMOJIS:
        raise HTTPException(400, "Emoji no válido.")

    # Solo se puede votar la foto que está en pantalla ahora mismo
    if not current_photo or body.photo_id != current_photo["id"]:
        raise HTTPException(400, "Esa foto ya no está en pantalla.")

    # Deduplicación server-side por IP
    ip = request.client.host if request.client else "unknown"
    if ip in votes_by_ip[body.photo_id]:
        raise HTTPException(400, "Ya votaste esta foto.")
    votes_by_ip[body.photo_id].add(ip)

    if body.photo_id not in votes:
        votes[body.photo_id] = {}
    votes[body.photo_id][body.emoji] = votes[body.photo_id].get(body.emoji, 0) + 1

    total = sum(votes[body.photo_id].values())
    logger.info(f"Voto — {body.photo_id[:8]} {body.emoji} (total: {total}) — IP: {ip}")

    save_votes()

    await screen_mgr.broadcast({
        "type": "vote",
        "photo_id": body.photo_id,
        "emoji": body.emoji,
        "counts": votes[body.photo_id],
    })

    return JSONResponse({"ok": True, "total": total})


@app.get("/votes")
async def get_votes():
    """Todos los votos — usado por la ruleta."""
    result = []
    for photo in photo_queue:
        pid = photo["id"]
        photo_votes = votes.get(pid, {})
        total = sum(photo_votes.values())
        result.append({**photo, "votes": photo_votes, "total_votes": total})
    result.sort(key=lambda x: x["total_votes"], reverse=True)
    return {"photos": result}


@app.get("/current-photo")
async def get_current_photo():
    if not current_photo:
        return {"photo": None}
    pid = current_photo["id"]
    return {"photo": {**current_photo, "votes": votes.get(pid, {})}}


# ─── Misc ────────────────────────────────────────────────────────────────────

@app.post("/roulette/start")
async def start_roulette():
    """El admin dispara la ruleta desde su celular."""
    await screen_mgr.broadcast({"type": "start_roulette"})
    return JSONResponse({"ok": True})


@app.post("/photo/skip")
async def skip_photo():
    """El admin saca la foto actual de pantalla y la elimina de la ruleta."""
    global current_photo
    if current_photo:
        pid = current_photo["id"]
        async with queue_lock:
            photo_queue[:] = [p for p in photo_queue if p["id"] != pid]
        votes.pop(pid, None)
        votes_by_ip.pop(pid, None)
        save_votes()
        current_photo = None
    await screen_mgr.broadcast({"type": "skip_photo"})
    await mobile_mgr.broadcast({"type": "photo_skipped"})
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
                    await mobile_mgr.broadcast({"type": "now_showing", "photo": photo})
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
