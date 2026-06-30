"""
VideoPro Backend API
- JWT tabanli kullanici girisi (coklu kullanici / coklu isletme)
- Claude script onizleme proxy (CORS sorununu cozer, API key tarayicida durmaz)
- Make.com webhook proxy (video olusturma istegini sunucu tarafindan gonderir)
- HeyGen video durum sorgulama proxy (gercek durum takibi)

Calistirma (lokal):
    pip install -r requirements.txt --break-system-packages
    uvicorn main:app --reload --port 8000

Railway'e deploy:
    1) Bu klasoru ayri bir GitHub reposuna push et (orn: videopro-backend)
    2) Railway'de "New Project" -> "Deploy from GitHub repo" ile sec
    3) Environment Variables sekmesinde asagidaki degiskenleri ekle:
       - JWT_SECRET            (rastgele uzun bir string, orn: openssl rand -hex 32)
       - ANTHROPIC_API_KEY     (Claude API anahtarin)
       - HEYGEN_API_KEY        (HeyGen API anahtarin)
       - MAKE_WEBHOOK_URL      (https://hook.eu1.make.com/mvntpni2p9pfhy2o7l831ypkka3ai86c)
       - DATABASE_URL          (Railway Postgres eklersen otomatik gelir; eklemezsen SQLite kullanilir)
       - ALLOWED_ORIGIN        (ragbetyazilim.com gibi frontend'in yayinda oldugu domain, * da olur ama onerilmez)
    4) Railway "Settings -> Networking -> Generate Domain" ile public URL al,
       index.html icindeki API_BASE_URL'i bu URL ile guncelle.
"""

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import bcrypt
from jose import jwt, JWTError
from pydantic import BaseModel

# ---------- Ayarlar ----------
JWT_SECRET = os.environ.get("JWT_SECRET", "DEGISTIR-bu-cok-onemli-uretimde")
JWT_ALGO = "HS256"
JWT_EXPIRE_HOURS = 24 * 7  # 7 gun

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEYGEN_API_KEY = os.environ.get("HEYGEN_API_KEY", "")
MAKE_WEBHOOK_URL = os.environ.get(
    "MAKE_WEBHOOK_URL",
    "https://hook.eu1.make.com/mvntpni2p9pfhy2o7l831ypkka3ai86c",
)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
DB_PATH = os.environ.get("SQLITE_PATH", "videopro.db")

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


bearer_scheme = HTTPBearer()

app = FastAPI(title="VideoPro API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- DB (basit SQLite - istersen sonra Postgres'e tasinir) ----------
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                business_name TEXT,
                role TEXT DEFAULT 'user',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                heygen_video_id TEXT,
                business_name TEXT,
                service TEXT,
                status TEXT DEFAULT 'processing',
                video_url TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Ilk admin kullanicisi yoksa olustur (kullanici adi/sifreyi ilk girişten sonra degistir!)
        existing = db.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO users (username, password_hash, business_name, role) VALUES (?, ?, ?, ?)",
                ("admin", hash_password("DegistirilecekSifre123!"), "VideoPro Admin", "admin"),
            )


init_db()


# ---------- Modeller ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    business_name: str = ""
    role: str = "user"


class ScriptPreviewRequest(BaseModel):
    business: str
    service: str
    audience: str = "Genel"
    message: str = "Hemen iletişime geçin"
    tone: str = "samimi"
    duration: str = "60"
    lang_label: str = "Türkçe"


class GenerateVideoRequest(BaseModel):
    business_name: str
    service: str
    target_audience: str = "Genel"
    message: str = "Hemen iletişime geçin"
    tone: str = "samimi"
    notes: str = ""
    avatar_id: str = "Daisy-inskirt-20220818"
    voice_id: str = ""
    language: str = "tr"
    script: str = ""


# ---------- Auth yardimcilari ----------
def create_token(user_row) -> str:
    payload = {
        "sub": str(user_row["id"]),
        "username": user_row["username"],
        "role": user_row["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    token = creds.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except JWTError:
        raise HTTPException(status_code=401, detail="Gecersiz veya suresi dolmus oturum")
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?", (payload["sub"],)).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="Kullanici bulunamadi")
    return user


def require_admin(user=Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Bu islem icin yetkin yok")
    return user


# ---------- Auth endpointleri ----------
@app.post("/auth/login")
def login(body: LoginRequest):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Kullanici adi veya sifre hatali")
    token = create_token(user)
    return {
        "token": token,
        "username": user["username"],
        "business_name": user["business_name"],
        "role": user["role"],
    }


@app.get("/auth/me")
def me(user=Depends(get_current_user)):
    return {
        "username": user["username"],
        "business_name": user["business_name"],
        "role": user["role"],
    }


@app.post("/auth/users")
def create_user(body: CreateUserRequest, admin=Depends(require_admin)):
    """Sadece admin yeni musteri/kullanici hesabi acabilir."""
    with get_db() as db:
        exists = db.execute("SELECT id FROM users WHERE username = ?", (body.username,)).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="Bu kullanici adi zaten var")
        db.execute(
            "INSERT INTO users (username, password_hash, business_name, role) VALUES (?, ?, ?, ?)",
            (body.username, hash_password(body.password), body.business_name, body.role),
        )
    return {"ok": True}


# ---------- Claude script onizleme proxy ----------
@app.post("/api/script-preview")
def script_preview(body: ScriptPreviewRequest, user=Depends(get_current_user)):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="Sunucuda ANTHROPIC_API_KEY tanimli degil")

    prompt = (
        f"Sen bir video script yazarisin. Asagidaki bilgilere gore {body.duration} saniyelik kisa, "
        f"etkileyici bir {body.lang_label} video senaryosu yaz. Senaryo {body.tone} bir tonla, dogal "
        f"konusma dilinde olsun. Avatar tarafindan seslendirilecek.\n\n"
        f"Isletme adi: {body.business}\nHizmet/Urun: {body.service}\n"
        f"Hedef kitle: {body.audience}\nOzel mesaj: {body.message}\n\n"
        f"Sadece senaryoyu yaz, baska aciklama ekleme. Tirnak isareti kullanma."
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Claude API hatasi: {resp.text}")
    data = resp.json()
    script = (data.get("content") or [{}])[0].get("text", "")
    return {"script": script}


# ---------- Video olusturma (Make.com proxy) ----------
@app.post("/api/videos/generate")
def generate_video(body: GenerateVideoRequest, user=Depends(get_current_user)):
    payload = body.dict()
    try:
        resp = requests.post(MAKE_WEBHOOK_URL, json=payload, timeout=20)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Make.com baglanti hatasi: {e}")

    heygen_video_id = None
    try:
        resp_json = resp.json()
        heygen_video_id = resp_json.get("video_id") or resp_json.get("data", {}).get("video_id")
    except Exception:
        pass

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO videos (user_id, heygen_video_id, business_name, service, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (user["id"], heygen_video_id, body.business_name, body.service, "processing"),
        )
        video_row_id = cur.lastrowid

    return {"ok": resp.ok, "make_status": resp.status_code, "video_row_id": video_row_id, "heygen_video_id": heygen_video_id}


# ---------- Video listeleme + HeyGen gercek durum sorgulama ----------
def fetch_heygen_status(heygen_video_id: str):
    if not HEYGEN_API_KEY or not heygen_video_id:
        return None
    try:
        resp = requests.get(
            "https://api.heygen.com/v1/video_status.get",
            params={"video_id": heygen_video_id},
            headers={"X-Api-Key": HEYGEN_API_KEY},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        return {"status": data.get("status"), "video_url": data.get("video_url")}
    except requests.RequestException:
        return None


@app.get("/api/videos")
def list_videos(user=Depends(get_current_user)):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM videos WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (user["id"],),
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        if item["status"] == "processing" and item["heygen_video_id"]:
            live = fetch_heygen_status(item["heygen_video_id"])
            if live and live["status"]:
                heygen_status = live["status"]
                # HeyGen durumlari: pending / processing / completed / failed
                if heygen_status == "completed":
                    item["status"] = "completed"
                    item["video_url"] = live["video_url"]
                    with get_db() as db:
                        db.execute(
                            "UPDATE videos SET status = ?, video_url = ? WHERE id = ?",
                            ("completed", live["video_url"], item["id"]),
                        )
                elif heygen_status == "failed":
                    item["status"] = "failed"
                    with get_db() as db:
                        db.execute("UPDATE videos SET status = ? WHERE id = ?", ("failed", item["id"]))
        result.append(item)
    return {"videos": result}


@app.get("/api/videos/{video_row_id}/status")
def get_video_status(video_row_id: int, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM videos WHERE id = ? AND user_id = ?", (video_row_id, user["id"])
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video bulunamadi")
    live = fetch_heygen_status(row["heygen_video_id"]) if row["heygen_video_id"] else None
    return {"db_status": row["status"], "live": live}


@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}
