from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import os, uuid, subprocess, requests, json, re
from gtts import gTTS
from PIL import Image
import httpx
import imageio_ffmpeg

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

app = FastAPI(title="VideoPro Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = "/tmp/videopro"
os.makedirs(OUTPUT_DIR, exist_ok=True)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

PLATFORM_SIZES = {
    "youtube":   (854, 480),
    "tiktok":    (480, 854),
    "reels":     (480, 854),
    "instapost": (480, 480),
}

class VideoRequest(BaseModel):
    platform: str
    karakter_isim: str = ""
    karakter_unvan: str = ""
    gorsel_prompt: str = ""
    senaryo: str = ""
    sahne_aciklamasi: str = ""
    dil: str = "tr"
    sure: int = 60

class AIRequest(BaseModel):
    platform: str
    char_prompt: str
    content_prompt: str
    tone: str = "Profesyonel"
    dil: str = "tr"
    sure: int = 60


@app.get("/")
def root():
    return {"status": "VideoPro Backend çalışıyor ✓"}


@app.get("/uygulama")
async def uygulama():
    html_path = os.path.join(os.path.dirname(__file__), "videopro-creator.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/ai-uret")
async def ai_uret(req: AIRequest):
    platform_names = {
        "youtube": "YouTube (yatay 16:9)",
        "tiktok": "TikTok (dikey 9:16, kısa)",
        "reels": "Instagram Reels (dikey 9:16)",
        "instapost": "Instagram Post (kare 1:1)"
    }
    lang_name = "Türkçe" if req.dil == "tr" else "İngilizce"
    dur_label = f"{req.sure} saniye" if req.sure < 60 else f"{req.sure // 60} dakika"

    prompt = f"""Platform: {platform_names.get(req.platform, req.platform)}
Dil: {lang_name}
Süre: {dur_label}
Ton: {req.tone}

KARAKTER PROMPT: {req.char_prompt}
İÇERİK: {req.content_prompt}

Şu JSON formatında yanıt ver, başka hiçbir şey yazma:
{{
  "karakter": {{
    "isim": "...",
    "unvan": "...",
    "biyografi": "...(2 cümle)...",
    "gorsel_prompt_en": "professional portrait photo of [description], studio lighting, clean background"
  }},
  "senaryo": "...({lang_name} dilinde, {dur_label} sürelik konuşma metni)...",
  "sahne_aciklamasi": "...(arka plan, ortam açıklaması)..."
}}"""

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 1000
            }
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Groq hatası: {resp.text}")

    data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    clean = re.sub(r'```json|```', '', raw).strip()
    parsed = json.loads(clean)
    return parsed


@app.post("/video-olustur")
async def video_olustur(req: VideoRequest):
    job_id = str(uuid.uuid4())[:8]
    job_dir = f"{OUTPUT_DIR}/{job_id}"
    os.makedirs(job_dir, exist_ok=True)

    try:
        w, h = PLATFORM_SIZES.get(req.platform, (1920, 1080))

        # 1. GÖRSEL İNDİR
        img_prompt = req.gorsel_prompt + ", " + req.sahne_aciklamasi
        img_url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(img_prompt)}?width={w}&height={h}&nologo=true"

        async with httpx.AsyncClient(timeout=40) as client:
            img_resp = await client.get(img_url)

        img_path = f"{job_dir}/bg.jpg"
        with open(img_path, "wb") as f:
            f.write(img_resp.content)

        img = Image.open(img_path).resize((w, h), Image.LANCZOS)
        img.save(img_path, "JPEG", quality=90)

        # 2. SESLENDİRME
        lang_code = "tr" if req.dil == "tr" else "en"
        tts = gTTS(text=req.senaryo, lang=lang_code, slow=False)
        audio_path = f"{job_dir}/ses.mp3"
        tts.save(audio_path)

        # Ses süresi (ffmpeg ile)
        probe = subprocess.run(
            [FFMPEG_PATH, "-i", audio_path, "-f", "null", "-"],
            capture_output=True, text=True
        )
        import re as _re
        dur_match = _re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe.stderr)
        if dur_match:
            h2, m2, s2 = dur_match.groups()
            audio_duration = int(h2)*3600 + int(m2)*60 + float(s2)
        else:
            audio_duration = float(req.sure)

        # 3. ALTYAZI
        srt_path = f"{job_dir}/altyazi.srt"
        _create_srt(req.senaryo, audio_duration, srt_path)

        # 4. FFmpeg MP4
        output_path = f"{job_dir}/video.mp4"
        ffmpeg_cmd = [
            FFMPEG_PATH, "-y",
            "-loop", "1", "-i", img_path,
            "-i", audio_path,
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-shortest",
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
    return FileResponse(video_path, media_type="video/mp4", filename="videopro_output.mp4")


def _create_srt(text: str, duration: float, path: str):
    words = text.split()
    chunk_size = max(6, len(words) // max(1, int(duration / 4)))
    chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
    time_per_chunk = duration / max(len(chunks), 1)
    with open(path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunks):
            start = i * time_per_chunk
            end = min((i + 1) * time_per_chunk, duration)
            f.write(f"{i+1}\n{_fmt_time(start)} --> {_fmt_time(end)}\n{chunk}\n\n")


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"
