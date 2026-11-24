# 🤖 Personal Assistant

A web-based personal AI assistant with multiple input modes — text, voice, webcam, and image upload — powered by a local Ollama LLM and OpenAI Whisper for audio transcription.

## Architecture

```mermaid
graph LR
    subgraph Frontend["Web UI (HTML/TypeScript)"]
        A[Text Input]
        B[Audio Recording]
        C[Webcam Capture]
        D[Image Upload]
        K[Stream / Clip Analysis]
        P[Caption Comparison]
        Q[Transcript and Prompt Assembly]
        E[Response Display]
    end
    subgraph Backend["FastAPI Server"]
        F["/api/chat — Text"]
        G["/api/transcribe — Audio"]
        H["/api/chat-video — Image(s)+Text"]
        L["/api/demux"]
        M["ffmpeg / ffprobe"]
        N["CCExtractor\n(embedded CEA-608/708)"]
    end
    subgraph External["AI Services"]
        I["OpenAI Whisper API\n(Audio → Text)"]
        J["Local Ollama\n(LLM Brain)"]
    end
    A -->|POST| F
    B -->|POST audio| G
    G -->|audio file| I
    I -->|verbose transcription| G
    G -->|transcript| Q
    C -->|POST frame| H
    D -->|POST image| H
    K -->|POST file / URL| L
    P -->|POST file / URL + compare_captions| L
    L -->|demux| M
    L -->|embedded captions if needed| N
    L -->|audio, frames or captions| Q
    Q -->|audio track| G
    Q -->|frames + transcript| H
    Q -->|captions + transcript for comparison| F
    Q -->|audio-only transcript| F
    F -->|prompt| J
    H -->|prompt+image| J
    J -->|streamed text| F
    J -->|streamed vision response| H
    F -->|response| E
    H -->|response| E
```

## Prerequisites

- **Python 3.9+**
- **Node.js 16+** (for building TypeScript)
- **Ollama** running locally — https://ollama.com
- **OpenAI API key** (for Whisper audio transcription)
- **ffmpeg / ffprobe** on `PATH` (for stream/clip analysis) — `brew install ffmpeg`
- **CCExtractor** on `PATH` (for embedded CEA-608/708 caption comparison) — `brew install ccextractor`

### Pull Ollama models

```bash
# Text model (default)
ollama pull gemma4

# Vision model (default) — for webcam/image features
ollama pull qwen3.5:9b
```

## Setup & Run

### 1. Install dependencies

```bash
# Python
pip install -r requirements.txt

# TypeScript build tools
npm install
```

### 2. Build the frontend

```bash
npm run build
```

For development with auto-rebuild on file changes:

```bash
npm run watch
```

### 3. Set environment variables

```bash
export OPENAI_API_KEY="your-openai-api-key"
```

### 4. Start the server

```bash
python3 -m uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** in your browser.

## Changing LLM Models

All models are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_MODEL` | `gemma4` | Text chat model |
| `OLLAMA_VISION_MODEL` | `qwen3.5:9b` | Vision model (webcam & image upload) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OPENAI_API_KEY` | *(none)* | Required for audio transcription |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | ffmpeg binaries used for stream/clip demuxing |
| `CCEXTRACTOR_BIN` | `ccextractor` | Caption extractor for embedded CEA-608/708 |
| `STREAM_MAX_SECONDS` | `120` | Max seconds of a stream/clip that is loaded and analysed |
| `STREAM_FRAME_COUNT` | `4` | Number of evenly spaced frames sent to the vision model |

### Examples

```bash
# Use a different text model
export OLLAMA_MODEL="llama3.2"

# Use a different vision model
export OLLAMA_VISION_MODEL="llava"

# Point to a remote Ollama instance
export OLLAMA_BASE_URL="http://192.168.1.100:11434"
```

Then restart the server to apply changes.

## Input Modes

| Mode | Button | Description |
|---|---|---|
| **Text** | ➤ | Type a message and press Enter or click send |
| **Voice** | 🎤 | Click to start recording, click again to stop. Audio is transcribed via OpenAI Whisper, then sent to the LLM |
| **Webcam** | 📷 | Opens camera preview. Optionally type a question, then click "Capture & Ask" |
| **Image Upload** | 🖼️ | Pick an image file from disk. Optionally type a question before clicking |
| **Stream / Clip** | 🎞️ | Enter a stream URL (http/https/HLS, rtmp, rtsp) or choose a local video/audio clip. ffmpeg demuxes it: the audio track goes through the voice path (Whisper), sampled frames go through the webcam path (vision model) together with the transcript. Optionally type a question first |
| **Caption comparison** | 🔤 | Choose a clip or enter a stream URL. Captions and the Whisper audio transcript are sent to text chat for comparison. An audio track and readable captions are required. |

Caption comparison uses the first subtitle track if present; otherwise it
checks embedded CEA-708 service 1, then CEA-608 field 1, using CCExtractor.
It supports embedded captions in H.264, HEVC, and MPEG-2 video. Image-based
subtitle tracks cannot be converted to text. Only the first
`STREAM_MAX_SECONDS` are compared. FFmpeg may omit or misalign HLS WebVTT cues
using `X-TIMESTAMP-MAP`; timestamp-sensitive HLS comparisons are not reliable.

`POST /api/transcribe` requests Whisper's `verbose_json` and returns its full
response, including `text`, language, duration, and segment timestamps when
provided. The UI continues to use the `text` field for chat.

If FFmpeg rejects an HLS master containing `#EXT-X-START` with
“Master Playlist tag found in a Media Playlist”, the loader retries without
that start hint, retaining all video variants and audio renditions. The retry
uses FFmpeg's default live start position; local-file access remains disabled.
Other FFmpeg failures include the underlying diagnostics in the error message.

## Project Structure

```
personalHelper/
├── app.py               # FastAPI backend
├── requirements.txt     # Python dependencies
├── package.json         # Node.js build scripts
├── tsconfig.json        # TypeScript config
├── src/
│   └── main.ts          # Frontend source (TypeScript)
└── static/
    ├── index.html       # Web UI
    ├── style.css        # Styling
    └── main.js          # Bundled JS (generated — do not edit)
```
