from __future__ import annotations

import os
import base64
import json
import tempfile
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from openai import OpenAI

app = FastAPI()

# --- Config ---
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4")              # text model
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "qwen3.5:9b")  # vision model
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

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
    """Transcribe audio using OpenAI Whisper API, then return text."""
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
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=f
            )
        return {"text": transcript.text}
    finally:
        os.unlink(tmp_path)


@app.post("/api/chat-video")
async def chat_video(request: Request):
    """Chat with a webcam frame. Expects JSON {message: string, image: base64}."""
    body = await request.json()
    user_msg = body.get("message", "").strip() or "What do you see in this image?"
    image_b64 = body.get("image", "")

    if not image_b64:
        return {"error": "no image provided"}

    # Build message with image for Ollama vision model
    messages = list(conversation_history)
    messages.append({
        "role": "user",
        "content": user_msg,
        "images": [image_b64],
    })
    conversation_history.append({"role": "user", "content": f"[image] {user_msg}"})
    return _ollama_generate(messages, model=OLLAMA_VISION_MODEL)


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
