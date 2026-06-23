from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os, uuid, subprocess, textwrap, requests
from gtts import gTTS
from PIL import Image
import httpx

app = FastAPI(title="VideoPro Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = "/tmp/videopro"
os.makedirs(OUTPUT_DIR, exist_ok=True)

PLATFORM_SIZES = {
    "youtube":   (1920, 1080),
    "tiktok":    (1080, 1920),
    "reels":     (1080, 1920),
    "instapost": (1080, 1080),
}

class VideoRequest(BaseModel):
    platform: str          # youtube | tiktok | reels | instapost
    karakter_isim: str
    karakter_unvan: str
    gorsel_prompt: str     # İngilizce görsel prompt
    senaryo: str           # Konuşma metni
    sahne_aciklamasi: str
    dil: str = "tr"        # tr | en
    sure: int = 60         # saniye


@app.get("/")
def root():
    return {"status": "VideoPro Backend çalışıyor ✓"}


@app.post("/video-olustur")
async def video_olustur(req: VideoRequest):
    job_id = str(uuid.uuid4())[:8]
    job_dir = f"{OUTPUT_DIR}/{job_id}"
    os.makedirs(job_dir, exist_ok=True)

    try:
        # 1. GÖRSEL İNDİR (Pollinations.ai)
        w, h = PLATFORM_SIZES.get(req.platform, (1920, 1080))
        img_prompt = req.gorsel_prompt + ", " + req.sahne_aciklamasi
        img_url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(img_prompt)}?width={min(w,1024)}&height={min(h,1024)}&nologo=true"

        async with httpx.AsyncClient(timeout=30) as client:
            img_resp = await client.get(img_url)

        img_path = f"{job_dir}/bg.jpg"
        with open(img_path, "wb") as f:
            f.write(img_resp.content)

        # Boyutu platforma göre ayarla
        img = Image.open(img_path).resize((w, h), Image.LANCZOS)
        img.save(img_path, "JPEG", quality=90)

        # 2. SESLENDİRME (gTTS)
        lang_code = "tr" if req.dil == "tr" else "en"
        tts = gTTS(text=req.senaryo, lang=lang_code, slow=False)
        audio_path = f"{job_dir}/ses.mp3"
        tts.save(audio_path)

        # Ses süresini al
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True
        )
        audio_duration = float(probe.stdout.strip() or req.sure)

        # 3. ALTYAZI DOSYASI (.srt)
        srt_path = f"{job_dir}/altyazi.srt"
        _create_srt(req.senaryo, audio_duration, srt_path)

        # 4. FFmpeg ile MP4 OLUŞTUR
        output_path = f"{job_dir}/video.mp4"
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", img_path,
            "-i", audio_path,
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                   f"subtitles={srt_path}:force_style='FontSize=24,PrimaryColour=&Hffffff,OutlineColour=&H000000,Outline=2,Alignment=2'",
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            "-t", str(audio_duration + 1),
            output_path
        ]

        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            raise Exception(f"FFmpeg hatası: {result.stderr[-500:]}")

        return {
            "status": "ok",
            "job_id": job_id,
            "download_url": f"/indir/{job_id}",
            "sure": round(audio_duration, 1),
            "platform": req.platform,
            "boyut": f"{w}x{h}"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/indir/{job_id}")
def indir(job_id: str):
    video_path = f"{OUTPUT_DIR}/{job_id}/video.mp4"
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video bulunamadı")
    return FileResponse(
        video_path,
        media_type="video/mp4",
        filename="videopro_output.mp4"
    )


@app.get("/durum/{job_id}")
def durum(job_id: str):
    video_path = f"{OUTPUT_DIR}/{job_id}/video.mp4"
    return {"hazir": os.path.exists(video_path)}


def _create_srt(text: str, duration: float, path: str):
    """Metni eşit parçalara bölerek SRT altyazı dosyası oluşturur."""
    words = text.split()
    chunk_size = max(6, len(words) // max(1, int(duration / 4)))
    chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
    time_per_chunk = duration / max(len(chunks), 1)

    with open(path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks):
            start = i * time_per_chunk
            end = min((i + 1) * time_per_chunk, duration)
            f.write(f"{i+1}\n")
            f.write(f"{_fmt_time(start)} --> {_fmt_time(end)}\n")
            f.write(f"{chunk}\n\n")


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import os

@app.get("/uygulama")
async def uygulama():
    html_path = os.path.join(os.path.dirname(__file__), "videopro-creator.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())
