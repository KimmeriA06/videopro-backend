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

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
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
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
MAKE_WEBHOOK_URL = os.environ.get(
    "MAKE_WEBHOOK_URL",
    "https://hook.eu1.make.com/mvntpni2p9pfhy2o7l831ypkka3ai86c",
)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
DB_PATH = os.environ.get("SQLITE_PATH", "videopro.db")
UPLOADS_DIR = os.environ.get("UPLOADS_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "uploads"))
os.makedirs(UPLOADS_DIR, exist_ok=True)
# Bu backend'in disaridan erisilebilir adresi (paylasilan medya linklerinde kullanilir)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")

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
                business_id INTEGER,
                role TEXT DEFAULT 'user',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        existing_user_cols = [r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()]
        if "business_id" not in existing_user_cols:
            db.execute("ALTER TABLE users ADD COLUMN business_id INTEGER")
        db.execute("""
            CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                instagram TEXT,
                ig_account_id TEXT,
                ig_access_token TEXT,
                fb_page_id TEXT,
                fb_page_token TEXT,
                yt_refresh_token TEXT,
                yt_channel_id TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        existing_biz_cols = [r["name"] for r in db.execute("PRAGMA table_info(businesses)").fetchall()]
        for col in ["ig_account_id", "ig_access_token", "fb_page_id", "fb_page_token", "yt_refresh_token", "yt_channel_id"]:
            if col not in existing_biz_cols:
                db.execute(f"ALTER TABLE businesses ADD COLUMN {col} TEXT")
        db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                business_id INTEGER,
                content_type TEXT DEFAULT 'video',
                heygen_video_id TEXT,
                business_name TEXT,
                service TEXT,
                status TEXT DEFAULT 'processing',
                video_url TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Eski kurulumlarda yoksa kolonlari ekle (migration)
        existing_cols = [r["name"] for r in db.execute("PRAGMA table_info(videos)").fetchall()]
        if "business_id" not in existing_cols:
            db.execute("ALTER TABLE videos ADD COLUMN business_id INTEGER")
        if "content_type" not in existing_cols:
            db.execute("ALTER TABLE videos ADD COLUMN content_type TEXT DEFAULT 'video'")
        if "ig_posted" not in existing_cols:
            db.execute("ALTER TABLE videos ADD COLUMN ig_posted INTEGER DEFAULT 0")
        if "posted_platforms" not in existing_cols:
            db.execute("ALTER TABLE videos ADD COLUMN posted_platforms TEXT DEFAULT ''")
        # Ilk isletmeyi olustur (yoksa)
        if not db.execute("SELECT id FROM businesses LIMIT 1").fetchone():
            db.execute("INSERT INTO businesses (name) VALUES (?)", ("Esra Güzellik Salonu",))
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


class CreateBusinessRequest(BaseModel):
    name: str
    instagram: str = ""
    notes: str = ""
    ig_account_id: str = ""
    ig_access_token: str = ""
    fb_page_id: str = ""
    fb_page_token: str = ""
    yt_refresh_token: str = ""
    yt_channel_id: str = ""


class UpdateBusinessRequest(BaseModel):
    name: str = ""
    instagram: str = ""
    notes: str = ""
    ig_account_id: str = ""
    ig_access_token: str = ""
    fb_page_id: str = ""
    fb_page_token: str = ""
    yt_refresh_token: str = ""
    yt_channel_id: str = ""


class CreateUserRequest(BaseModel):
    username: str
    password: str
    business_name: str = ""
    business_id: int = 0
    role: str = "user"


class ScriptPreviewRequest(BaseModel):
    business: str
    service: str
    audience: str = "Genel"
    message: str = "Hemen iletişime geçin"
    tone: str = "samimi"
    duration: str = "60"
    lang_label: str = "Türkçe"


class ChangeCredentialsRequest(BaseModel):
    current_password: str
    new_username: str = ""
    new_password: str = ""



class GenerateVideoRequest(BaseModel):
    business_name: str
    business_id: int = 0
    content_type: str = "video"  # video | image_silent | image_voiceover | slideshow
    images: list[str] = []  # base64 data URL'leri (kucuk gorseller icin)
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


@app.post("/auth/change-credentials")
def change_credentials(body: ChangeCredentialsRequest, user=Depends(get_current_user)):
    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Mevcut şifre hatalı")

    new_username = body.new_username.strip() or user["username"]
    updates = {}

    if new_username != user["username"]:
        with get_db() as db:
            exists = db.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?", (new_username, user["id"])
            ).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten kullanılıyor")
        updates["username"] = new_username

    if body.new_password:
        if len(body.new_password) < 6:
            raise HTTPException(status_code=400, detail="Yeni şifre en az 6 karakter olmalı")
        updates["password_hash"] = hash_password(body.new_password)

    if not updates:
        raise HTTPException(status_code=400, detail="Değiştirilecek bir alan girilmedi")

    with get_db() as db:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", (*updates.values(), user["id"]))
        updated = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    new_token = create_token(updated)
    return {
        "ok": True,
        "token": new_token,
        "username": updated["username"],
        "business_name": updated["business_name"],
        "role": updated["role"],
    }



@app.get("/api/businesses")
def list_businesses(user=Depends(get_current_user)):
    with get_db() as db:
        if user["role"] == "admin":
            rows = db.execute("SELECT * FROM businesses ORDER BY name").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM businesses WHERE id = ?", (user["business_id"],)
            ).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        item["has_ig_token"] = bool(item.get("ig_access_token"))
        item["has_fb_token"] = bool(item.get("fb_page_token"))
        item["has_yt_token"] = bool(item.get("yt_refresh_token"))
        for secret_field in ["ig_access_token", "fb_page_token", "yt_refresh_token"]:
            item.pop(secret_field, None)  # token'lari ag uzerinden hic gondermiyoruz
        result.append(item)
    return {"businesses": result}


@app.post("/api/businesses")
def create_business(body: CreateBusinessRequest, admin=Depends(require_admin)):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO businesses "
            "(name, instagram, notes, ig_account_id, ig_access_token, fb_page_id, fb_page_token, yt_refresh_token, yt_channel_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body.name, body.instagram, body.notes, body.ig_account_id, body.ig_access_token,
                body.fb_page_id, body.fb_page_token, body.yt_refresh_token, body.yt_channel_id,
            ),
        )
        new_id = cur.lastrowid
    return {"ok": True, "id": new_id}



@app.patch("/api/businesses/{business_id}")
def update_business(business_id: int, body: UpdateBusinessRequest, admin=Depends(require_admin)):
    updates = {k: v for k, v in body.dict().items() if v}
    if not updates:
        raise HTTPException(status_code=400, detail="Değiştirilecek bir alan girilmedi")
    with get_db() as db:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        db.execute(f"UPDATE businesses SET {set_clause} WHERE id = ?", (*updates.values(), business_id))
    return {"ok": True}


@app.post("/auth/users")
def create_user(body: CreateUserRequest, admin=Depends(require_admin)):
    """Sadece admin yeni musteri/kullanici hesabi acabilir."""
    with get_db() as db:
        exists = db.execute("SELECT id FROM users WHERE username = ?", (body.username,)).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="Bu kullanici adi zaten var")
        db.execute(
            "INSERT INTO users (username, password_hash, business_name, business_id, role) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                body.username,
                hash_password(body.password),
                body.business_name,
                body.business_id or None,
                body.role,
            ),
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
    # Yetki kontrolu: admin herhangi bir isletme icin uretebilir,
    # normal kullanici sadece kendi isletmesi icin uretebilir.
    business_id = body.business_id or user["business_id"]
    if user["role"] != "admin" and business_id != user["business_id"]:
        raise HTTPException(status_code=403, detail="Bu işletme için içerik oluşturma yetkin yok")

    payload = body.dict()
    payload["images_count"] = len(body.images)  # buyuk base64 datayi loglarda gormemek icin ozet
    try:
        resp = requests.post(MAKE_WEBHOOK_URL, json=payload, timeout=30)
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
            "INSERT INTO videos (user_id, business_id, content_type, heygen_video_id, business_name, service, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], business_id or None, body.content_type, heygen_video_id, body.business_name, body.service, "processing"),
        )
        video_row_id = cur.lastrowid

    return {"ok": resp.ok, "make_status": resp.status_code, "video_row_id": video_row_id, "heygen_video_id": heygen_video_id}


@app.post("/api/videos/upload")
def upload_own_video(
    request: Request,
    business_id: int = Form(...),
    business_name: str = Form(...),
    service: str = Form(""),
    file: UploadFile = File(...),
    user=Depends(get_current_user),
):
    """Kullanicinin kendi bilgisayarindan yukledigi videoyu kaydeder,
    HeyGen/Make.com'a hic gitmez, direkt 'tamamlandi' olarak isaretlenir."""
    if user["role"] != "admin" and business_id != user["business_id"]:
        raise HTTPException(status_code=403, detail="Bu işletme için içerik yükleme yetkin yok")

    allowed_ext = {".mp4", ".mov", ".webm", ".m4v"}
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(status_code=400, detail="Sadece video dosyaları (.mp4, .mov, .webm, .m4v) yükleyebilirsin")

    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(UPLOADS_DIR, filename)
    with open(filepath, "wb") as f:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    base_url = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    video_url = f"{base_url}/media/{filename}"

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO videos (user_id, business_id, content_type, business_name, service, status, video_url) "
            "VALUES (?, ?, 'own_upload', ?, ?, 'completed', ?)",
            (user["id"], business_id, business_name, service, video_url),
        )
        video_row_id = cur.lastrowid

    return {"ok": True, "video_row_id": video_row_id, "video_url": video_url}


@app.get("/media/{filename}")
def serve_media(filename: str):
    safe_name = os.path.basename(filename)  # path traversal koruması
    filepath = os.path.join(UPLOADS_DIR, safe_name)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı")
    return FileResponse(filepath, media_type="video/mp4")



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
        if user["role"] == "admin":
            rows = db.execute(
                "SELECT * FROM videos ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        else:
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


# ---------- Video silme (DB + HeyGen kuyrugundan) ----------
def delete_heygen_video(heygen_video_id: str) -> bool:
    """HeyGen tarafindaki videoyu da siler. Basarili olursa True doner."""
    if not HEYGEN_API_KEY or not heygen_video_id:
        return False
    try:
        resp = requests.delete(
            "https://api.heygen.com/v1/video.delete",
            params={"video_id": heygen_video_id},
            headers={"X-Api-Key": HEYGEN_API_KEY},
            timeout=15,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


@app.delete("/api/videos/{video_row_id}")
def delete_video(video_row_id: int, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE id = ?", (video_row_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Video bulunamadı")
    if user["role"] != "admin" and row["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Bu videoyu silme yetkin yok")

    # HeyGen tarafinda da varsa, orada da sil (kuyrukta/render'da bekleyen dahil)
    heygen_deleted = None
    if row["heygen_video_id"]:
        heygen_deleted = delete_heygen_video(row["heygen_video_id"])

    # Kendi yuklenmis (own_upload) dosyasi varsa, sunucudaki dosyayi da sil
    if row["content_type"] == "own_upload" and row["video_url"]:
        try:
            filename = row["video_url"].rsplit("/", 1)[-1]
            filepath = os.path.join(UPLOADS_DIR, filename)
            if os.path.isfile(filepath):
                os.remove(filepath)
        except OSError:
            pass

    with get_db() as db:
        db.execute("DELETE FROM videos WHERE id = ?", (video_row_id,))

    return {"ok": True, "heygen_deleted": heygen_deleted}


class PublishRequest(BaseModel):
    platforms: list[str] = ["instagram"]  # instagram | facebook | youtube


def _publish_to_instagram(business, media_url, is_video, caption):
    if not business["ig_account_id"] or not business["ig_access_token"]:
        return False, "Instagram hesabı bağlanmamış."
    ig_account_id = business["ig_account_id"]
    access_token = business["ig_access_token"]
    create_params = {"caption": caption, "access_token": access_token}
    if is_video:
        create_params["media_type"] = "REELS"
        create_params["video_url"] = media_url
    else:
        create_params["image_url"] = media_url
    create_resp = requests.post(
        f"https://graph.facebook.com/v21.0/{ig_account_id}/media", data=create_params, timeout=60
    )
    create_data = create_resp.json()
    if "id" not in create_data:
        return False, f"Instagram container hatası: {create_data}"
    creation_id = create_data["id"]
    if is_video:
        for _ in range(15):
            status_resp = requests.get(
                f"https://graph.facebook.com/v21.0/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=15,
            )
            status_code = status_resp.json().get("status_code")
            if status_code == "FINISHED":
                break
            if status_code == "ERROR":
                return False, "Instagram video işleme hatası"
            time.sleep(2)
    publish_resp = requests.post(
        f"https://graph.facebook.com/v21.0/{ig_account_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    publish_data = publish_resp.json()
    if "id" not in publish_data:
        return False, f"Instagram yayınlama hatası: {publish_data}"
    return True, publish_data["id"]


def _publish_to_facebook(business, media_url, is_video, caption):
    if not business["fb_page_id"] or not business["fb_page_token"]:
        return False, "Facebook sayfası bağlanmamış."
    page_id = business["fb_page_id"]
    access_token = business["fb_page_token"]
    endpoint = "videos" if is_video else "photos"
    params = {"access_token": access_token, "caption": caption}
    params["file_url" if is_video else "url"] = media_url
    resp = requests.post(
        f"https://graph.facebook.com/v21.0/{page_id}/{endpoint}", data=params, timeout=60
    )
    data = resp.json()
    if "id" not in data and "post_id" not in data:
        return False, f"Facebook yayınlama hatası: {data}"
    return True, data.get("post_id") or data.get("id")


def _publish_to_youtube(business, media_url, caption):
    if not business["yt_refresh_token"]:
        return False, "YouTube hesabı bağlanmamış."
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return False, "Sunucuda GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET tanımlı değil."

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": business["yt_refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return False, f"YouTube token yenileme hatası: {token_data}"

    video_resp = requests.get(media_url, timeout=60)
    if video_resp.status_code != 200:
        return False, "Video dosyası indirilemedi (YouTube yüklemesi için)."
    video_bytes = video_resp.content

    metadata = {
        "snippet": {"title": caption[:100], "description": caption},
        "status": {"privacyStatus": "public"},
    }
    upload_resp = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=multipart&part=snippet,status",
        headers={"Authorization": f"Bearer {access_token}"},
        files={
            "metadata": (None, json.dumps(metadata), "application/json"),
            "media": ("video.mp4", video_bytes, "video/mp4"),
        },
        timeout=300,
    )
    data = upload_resp.json()
    if "id" not in data:
        return False, f"YouTube yükleme hatası: {data}"
    return True, data["id"]


@app.post("/api/videos/{video_row_id}/publish")
def publish_content(video_row_id: int, body: PublishRequest, user=Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE id = ?", (video_row_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="İçerik bulunamadı")
        if user["role"] != "admin" and row["user_id"] != user["id"] and row["business_id"] != user["business_id"]:
            raise HTTPException(status_code=403, detail="Bu içeriği paylaşma yetkin yok")
        if row["status"] != "completed" or not row["video_url"]:
            raise HTTPException(status_code=400, detail="İçerik henüz hazır değil")
        business = db.execute("SELECT * FROM businesses WHERE id = ?", (row["business_id"],)).fetchone()
        if not business:
            raise HTTPException(status_code=400, detail="İşletme bulunamadı")

    media_url = row["video_url"]
    is_video = row["content_type"] in ("video", "image_voiceover", "slideshow")
    caption = f"{row['business_name']} — {row['service']}"

    results = {}
    for platform in body.platforms:
        try:
            if platform == "instagram":
                ok, info = _publish_to_instagram(business, media_url, is_video, caption)
            elif platform == "facebook":
                ok, info = _publish_to_facebook(business, media_url, is_video, caption)
            elif platform == "youtube":
                ok, info = _publish_to_youtube(business, media_url, caption)
            else:
                ok, info = False, "Bilinmeyen platform"
        except requests.RequestException as e:
            ok, info = False, f"Bağlantı hatası: {e}"
        results[platform] = {"ok": ok, "detail": info}

    posted_ok = [p for p, r in results.items() if r["ok"]]
    if posted_ok:
        with get_db() as db:
            existing = db.execute(
                "SELECT posted_platforms FROM videos WHERE id = ?", (video_row_id,)
            ).fetchone()
            current = set((existing["posted_platforms"] or "").split(",")) - {""}
            current.update(posted_ok)
            db.execute(
                "UPDATE videos SET posted_platforms = ?, ig_posted = ? WHERE id = ?",
                (",".join(sorted(current)), 1 if "instagram" in current else 0, video_row_id),
            )

    return {"results": results}



@app.get("/health")
def health():
    return {"ok": True, "time": time.time()}
