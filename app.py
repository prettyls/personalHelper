from __future__ import annotations

import os
import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
from typing import Optional
from urllib.parse import urljoin, urlparse
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from openai import OpenAI
from PyPDF2 import PdfReader

app = FastAPI()
logger = logging.getLogger(__name__)

# --- Config ---
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4")              # text model
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "qwen3.5:9b")  # vision model
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
CCEXTRACTOR_BIN = os.getenv("CCEXTRACTOR_BIN", "ccextractor")
STREAM_MAX_SECONDS = int(os.getenv("STREAM_MAX_SECONDS", "120"))  # max media analysed
STREAM_FRAME_COUNT = int(os.getenv("STREAM_FRAME_COUNT", "4"))    # frames sent to vision model

# Restrict ffmpeg inputs so uploaded/remote media can't pull in local files.
_CLIP_DEMUXERS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,flv,mp3,wav,ogg,aac,flac"
_STREAM_PROTOCOLS = "http,https,tcp,tls,crypto,rtmp,rtmps,rtsp,udp,rtp"
_STREAM_SCHEMES = {"http", "https", "rtmp", "rtmps", "rtsp"}

# Conversation history (in-memory, per-server-lifetime)
conversation_history: list[dict] = []


# ---------- helpers ----------
def _ollama_generate(messages: list[dict], model: Optional[str] = None):
    """Stream tokens from Ollama /api/chat."""
    model = model or OLLAMA_MODEL
    payload = {"model": model, "messages": messages, "stream": True}

    def _stream():
        with httpx.stream(
            "POST",
            f"{OLLAMA_BASE}/api/chat",
            json=payload,
            timeout=120.0,
        ) as resp:
            resp.raise_for_status()
            full_response = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                token = data.get("message", {}).get("content", "")
                if token:
                    full_response += token
                    yield token
            # save assistant reply to history
            conversation_history.append({"role": "assistant", "content": full_response})

    return StreamingResponse(_stream(), media_type="text/plain")


class MediaError(Exception):
    pass


def _run(
    cmd: list[str], timeout: float, input_data: Optional[bytes] = None
) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, input=input_data)
    except FileNotFoundError:
        raise MediaError(f"{cmd[0]} not found — install ffmpeg")
    except subprocess.TimeoutExpired:
        raise MediaError(f"{os.path.basename(cmd[0])} timed out")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise MediaError(err[-4000:] if err else f"{cmd[0]} failed")
    return proc


def _input_args(src: str) -> list[str]:
    return ["-protocol_whitelist", "file", "-format_whitelist", _CLIP_DEMUXERS, "-i", src]


def _load_stream(url: str, dest: str, include_subtitles: bool = False) -> None:
    """Record up to STREAM_MAX_SECONDS of a remote stream into a local file."""
    output_args = ["-t", str(STREAM_MAX_SECONDS), "-map", "0:v:0?", "-map", "0:a:0?"]
    if include_subtitles:
        output_args += ["-map", "0:s:0?", "-c:s", "srt"]
    output_args += ["-c:v", "copy", "-c:a", "copy", dest]
    input_options = (
        ["-analyzeduration", "100000", "-probesize", "32768"]
        if include_subtitles else []
    )
    try:
        _run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
             "-protocol_whitelist", _STREAM_PROTOCOLS, "-rw_timeout", "15000000",
             *input_options, "-i", url, *output_args],
            timeout=STREAM_MAX_SECONDS + 60,
        )
        return
    except MediaError as e:
        if (
            "Master Playlist tag found in a Media Playlist" not in str(e)
            or urlparse(url).scheme.lower() not in {"http", "https"}
        ):
            raise
        original_error = e

    try:
        response = httpx.get(url, timeout=15.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise MediaError(f"Could not reload HLS master playlist: {e}") from e

    lines = [line.strip() for line in response.text.splitlines()]
    if (
        not lines or lines[0] != "#EXTM3U"
        or not any(line.startswith("#EXT-X-STREAM-INF:") for line in lines)
        or not any(line.startswith("#EXT-X-START:") for line in lines)
    ):
        raise original_error

    # FFmpeg 9 treats EXT-X-START as a media-only tag, rejecting valid masters.
    # stdin has no URL base, so retain all renditions with absolute references.
    playlist = []
    for line in lines:
        if line.startswith("#EXT-X-START:"):
            continue
        if line.startswith("#"):
            line = re.sub(
                r'URI="([^"]*)"',
                lambda match: f'URI="{urljoin(str(response.url), match[1])}"',
                line,
            )
        elif line:
            line = urljoin(str(response.url), line)
        playlist.append(line)

    logger.warning("Retrying HLS master without EXT-X-START for FFmpeg compatibility")
    _run(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
         "-protocol_whitelist", f"pipe,{_STREAM_PROTOCOLS}", "-rw_timeout", "15000000",
         *input_options, "-f", "hls", "-i", "pipe:0", *output_args],
        timeout=STREAM_MAX_SECONDS + 60,
        input_data=("\n".join(playlist) + "\n").encode("utf-8"),
    )


def _probe(src: str) -> dict:
    proc = _run(
        [FFPROBE_BIN, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams",
         "-protocol_whitelist", "file", "-format_whitelist", _CLIP_DEMUXERS, src],
        timeout=30,
    )
    info = json.loads(proc.stdout or b"{}")
    streams = info.get("streams", [])
    try:
        duration = float(info.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        duration = 0.0
    return {
        "duration": duration,
        "has_video": any(
            s.get("codec_type") == "video"
            and not s.get("disposition", {}).get("attached_pic")
            for s in streams
        ),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "video_codec": next(
            (s.get("codec_name") for s in streams
             if s.get("codec_type") == "video"
             and not s.get("disposition", {}).get("attached_pic")),
            None,
        ),
        "subtitles": [
            {"codec": s.get("codec_name"), "language": s.get("tags", {}).get("language")}
            for s in streams if s.get("codec_type") == "subtitle"
        ],
    }


def _demux_frames(src: str, duration: float, workdir: str) -> list[str]:
    """Extract evenly spaced JPEG frames (base64), like webcam captures."""
    span = min(duration, STREAM_MAX_SECONDS)
    count = max(1, STREAM_FRAME_COUNT) if span > 0 else 1
    frames: list[str] = []
    for i in range(count):
        ts = span * (i + 0.5) / count if span > 0 else 0
        out = os.path.join(workdir, f"frame_{i:02d}.jpg")
        _run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{ts:.3f}", *_input_args(src),
             "-map", "0:v:0", "-frames:v", "1",
             "-vf", "scale='min(768,iw)':-2", "-q:v", "4", out],
            timeout=60,
        )
        if os.path.exists(out):
            with open(out, "rb") as f:
                frames.append(base64.b64encode(f.read()).decode("ascii"))
    return frames


def _demux_audio(src: str, workdir: str) -> str:
    """Extract the first audio track as mono 16 kHz Opus/WebM (base64), like mic recordings."""
    out = os.path.join(workdir, "audio.webm")
    _run(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
         *_input_args(src), "-t", str(STREAM_MAX_SECONDS),
         "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libopus", "-b:a", "32k", out],
        timeout=STREAM_MAX_SECONDS + 60,
    )
    with open(out, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _demux_captions(src: str, info: dict, workdir: str) -> dict:
    """Extract the first text subtitle track or embedded CEA-608/708 captions."""
    if info["subtitles"]:
        subtitle = info["subtitles"][0]
        proc = _run(
            [FFMPEG_BIN, "-hide_banner", "-loglevel", "error",
             *_input_args(src), "-t", str(STREAM_MAX_SECONDS),
             "-map", "0:s:0", "-c:s", "srt", "-f", "srt", "pipe:1"],
            timeout=STREAM_MAX_SECONDS + 60,
        )
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        if text:
            return {"text": text, "source": "subtitle track",
                    "language": subtitle["language"]}

    if not info["has_video"]:
        raise MediaError("No captions found in the selected media interval")
    if info["video_codec"] not in {"h264", "hevc", "mpeg2video"}:
        raise MediaError(
            f"Embedded caption extraction is not supported for video codec: {info['video_codec']}"
        )
    if not shutil.which(CCEXTRACTOR_BIN):
        raise MediaError("ccextractor not found; install it to extract embedded CEA-608/708 captions")

    transport = os.path.join(workdir, "captions.ts")
    _run(
        [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
         *_input_args(src), "-t", str(STREAM_MAX_SECONDS),
         "-map", "0:v:0", "-c:v", "copy", "-f", "mpegts", transport],
        timeout=STREAM_MAX_SECONDS + 60,
    )
    for label, selection in (("CEA-708 service 1", ["--service", "1"]),
                             ("CEA-608 field 1", ["-1"])):
        try:
            proc = subprocess.run(
                [CCEXTRACTOR_BIN, "--stdout", "--out", "srt", "--no-typesetting",
                 *selection, transport],
                capture_output=True, timeout=STREAM_MAX_SECONDS + 60,
            )
        except FileNotFoundError as e:
            raise MediaError("ccextractor not found; install it to extract embedded captions") from e
        except subprocess.TimeoutExpired as e:
            raise MediaError("ccextractor timed out") from e
        # CCExtractor uses exit code 10 for inputs with no captions.
        if proc.returncode not in (0, 10):
            error = proc.stderr.decode("utf-8", errors="replace").strip()
            raise MediaError(f"ccextractor failed: {error[-4000:]}")
        text = proc.stdout.decode("utf-8-sig", errors="replace").strip()
        if text:
            return {"text": text, "source": label, "language": None}
    raise MediaError("No captions found in the selected media interval")


# ---------- endpoints ----------
@app.post("/api/chat")
async def chat(request: Request):
    """Text chat. Expects JSON {message: string}."""
    body = await request.json()
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return {"error": "empty message"}

    conversation_history.append({"role": "user", "content": user_msg})
    return _ollama_generate(list(conversation_history))


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Transcribe audio using OpenAI Whisper and return verbose JSON."""
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY not set"}

    # Save uploaded audio to a temp file
    suffix = os.path.splitext(audio.filename or "audio.webm")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        with open(tmp_path, "rb") as f:
            response = client.audio.transcriptions.with_raw_response.create(
                model="whisper-1", file=f, response_format="verbose_json"
            )
        return response.http_response.json()
    finally:
        os.unlink(tmp_path)


@app.post("/api/chat-video")
async def chat_video(request: Request):
    """Chat with image(s). Expects JSON {message, image: base64} or {message, images: [base64]}."""
    body = await request.json()
    user_msg = body.get("message", "").strip() or "What do you see in this image?"
    images = body.get("images") or ([body["image"]] if body.get("image") else [])

    if not images:
        return {"error": "no image provided"}

    # Build message with image(s) for Ollama vision model
    messages = list(conversation_history)
    messages.append({
        "role": "user",
        "content": user_msg,
        "images": images,
    })
    tag = "[image]" if len(images) == 1 else f"[{len(images)} images]"
    conversation_history.append({"role": "user", "content": f"{tag} {user_msg}"})
    return _ollama_generate(messages, model=OLLAMA_VISION_MODEL)


@app.post("/api/demux")
def demux(
    file: Optional[UploadFile] = File(None),
    url: str = Form(""),
    compare_captions: bool = Form(False),
):
    """Load an uploaded clip or a stream URL and demux it with ffmpeg.

    Returns video frames and audio, or captions and audio for comparison.
    """
    url = url.strip()
    if not file and not url:
        return {"error": "provide a file or a stream URL"}
    if url and urlparse(url).scheme.lower() not in _STREAM_SCHEMES:
        return {"error": f"Unsupported stream URL scheme (use {', '.join(sorted(_STREAM_SCHEMES))})"}

    workdir = tempfile.mkdtemp(prefix="demux_")
    try:
        if file:
            suffix = os.path.splitext(file.filename or "")[1] or ".bin"
            src = os.path.join(workdir, f"input{suffix}")
            with open(src, "wb") as out:
                shutil.copyfileobj(file.file, out)
        else:
            src = os.path.join(workdir, "input.mkv")
            _load_stream(url, src, include_subtitles=compare_captions)

        info = _probe(src)
        if not info["has_video"] and not info["has_audio"]:
            return {"error": "No audio or video streams found"}

        if compare_captions and not info["has_audio"]:
            return {"error": "No audio track found for caption comparison"}
        try:
            captions = _demux_captions(src, info, workdir) if compare_captions else None
        except MediaError as e:
            if not file and "No captions found" in str(e):
                raise MediaError(
                    f"{e}. FFmpeg may omit HLS WebVTT cues with X-TIMESTAMP-MAP."
                ) from e
            raise
        frames = (
            _demux_frames(src, info["duration"], workdir)
            if info["has_video"] and not compare_captions else []
        )
        audio = _demux_audio(src, workdir) if info["has_audio"] else None
        return {
            "source": file.filename if file else url,
            "duration": info["duration"],
            "frames": frames,
            "audio": audio,
            "audio_mime": "audio/webm" if audio else None,
            "captions": captions,
        }
    except MediaError as e:
        return {"error": f"ffmpeg: {e}"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/chat-file")
async def chat_file(
    file: UploadFile = File(...),
    message: str = Form(""),
):
    """Upload a PDF or text file, extract content, and send to the text LLM."""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    raw = await file.read()

    if ext == ".pdf":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        try:
            reader = PdfReader(tmp_path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        finally:
            os.unlink(tmp_path)
    elif ext in (".txt", ".text", ".md", ".csv", ".json", ".xml", ".html", ".log"):
        text = raw.decode("utf-8", errors="replace")
    else:
        return {"error": f"Unsupported file type: {ext}"}

    if not text.strip():
        return {"error": "Could not extract any text from the file"}

    prompt = message.strip() if message.strip() else "Please summarise the following document."
    user_content = f"{prompt}\n\n---\n\n{text}"
    conversation_history.append({"role": "user", "content": user_content})
    return _ollama_generate(list(conversation_history))


@app.post("/api/clear")
async def clear():
    conversation_history.clear()
    return {"status": "cleared"}


# ---------- serve frontend ----------
@app.get("/")
async def index():
    with open("static/index.html", "r") as f:
        return HTMLResponse(f.read())

app.mount("/static", StaticFiles(directory="static"), name="static")
