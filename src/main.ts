import { marked } from "marked";

// ── DOM elements ─────────────────────────────────────────
const $ = <T extends HTMLElement>(id: string): T =>
  document.getElementById(id) as T;

const chat = $<HTMLDivElement>("chat");
const msgInput = $<HTMLTextAreaElement>("msgInput");
const sendBtn = $<HTMLButtonElement>("sendBtn");
const micBtn = $<HTMLButtonElement>("micBtn");
const camBtn = $<HTMLButtonElement>("camBtn");
const clearBtn = $<HTMLButtonElement>("clearBtn");
const webcamCtr = $<HTMLDivElement>("webcam-container");
const webcamEl = $<HTMLVideoElement>("webcam");
const captureBtn = $<HTMLButtonElement>("captureBtn");
const closeWcBtn = $<HTMLButtonElement>("closeWebcam");
const uploadBtn = $<HTMLButtonElement>("uploadBtn");
const uploadInput = $<HTMLInputElement>("uploadInput");
const fileBtn = $<HTMLButtonElement>("fileBtn");
const fileInput = $<HTMLInputElement>("fileInput");
const streamBtn = $<HTMLButtonElement>("streamBtn");
const captionBtn = $<HTMLButtonElement>("captionBtn");
const streamCtr = $<HTMLDivElement>("stream-container");
const streamUrl = $<HTMLInputElement>("streamUrl");
const streamUrlBtn = $<HTMLButtonElement>("streamUrlBtn");
const streamFileBtn = $<HTMLButtonElement>("streamFileBtn");
const streamFileInput = $<HTMLInputElement>("streamFileInput");
const closeStreamBtn = $<HTMLButtonElement>("closeStream");

let mediaRecorder: MediaRecorder | null = null;
let audioChunks: Blob[] = [];
let webcamStream: MediaStream | null = null;
let compareCaptions = false;

// ── helpers ──────────────────────────────────────────────
function addMsg(role: "user" | "assistant", text: string): HTMLDivElement {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="label">${role}</div><span class="text"></span>`;
  div.querySelector(".text")!.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

async function streamResponse(
  response: Response,
  bubble: HTMLDivElement
): Promise<void> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  const span = bubble.querySelector(".text") as HTMLSpanElement;
  span.textContent = "";
  let fullText = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    fullText += decoder.decode(value, { stream: true });
    // Show raw text while streaming for responsiveness
    span.textContent = fullText;
    chat.scrollTop = chat.scrollHeight;
  }

  // Render final markdown to HTML
  span.innerHTML = marked.parse(fullText) as string;
  chat.scrollTop = chat.scrollHeight;
}

// ── text send ────────────────────────────────────────────
async function sendText(text: string, displayText?: string): Promise<void> {
  const trimmed = text.trim();
  if (!trimmed) return;

  addMsg("user", displayText ?? trimmed);
  msgInput.value = "";
  msgInput.style.height = "auto";

  const bubble = addMsg("assistant", "⏳ thinking…");
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: trimmed }),
    });
    await streamResponse(res, bubble);
  } catch (e) {
    bubble.querySelector(".text")!.textContent =
      "❌ " + (e instanceof Error ? e.message : String(e));
  }
}

sendBtn.addEventListener("click", () => sendText(msgInput.value));
msgInput.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendText(msgInput.value);
  }
});
msgInput.addEventListener("input", () => {
  msgInput.style.height = "auto";
  msgInput.style.height = `${msgInput.scrollHeight}px`;
});

// ── audio recording (OpenAI Whisper) ─────────────────────
interface Transcription {
  text?: string;
  segments?: { start: number; end: number; text: string }[];
  error?: string;
}

async function transcribeAudio(blob: Blob, filename: string): Promise<Transcription> {
  const fd = new FormData();
  fd.append("audio", blob, filename);
  const res = await fetch("/api/transcribe", { method: "POST", body: fd });
  const data: Transcription = await res.json();
  if (data.error) throw new Error(data.error);
  if (!res.ok) throw new Error(`Transcription failed (HTTP ${res.status})`);
  if (typeof data.text !== "string") throw new Error("Transcription response has no text");
  return data;
}

async function transcribeBlob(blob: Blob, filename: string): Promise<string> {
  const data = await transcribeAudio(blob, filename);
  return data.text!;
}

micBtn.addEventListener("click", async () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];

    mediaRecorder.ondataavailable = (e: BlobEvent) => audioChunks.push(e.data);

    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      micBtn.classList.remove("recording");

      const blob = new Blob(audioChunks, { type: "audio/webm" });
      addMsg("user", "🎤 (transcribing audio…)");

      try {
        const text = await transcribeBlob(blob, "recording.webm");
        // replace last user msg with transcript
        const msgs = chat.querySelectorAll(".msg.user");
        const lastUser = msgs[msgs.length - 1];
        lastUser.querySelector(".text")!.textContent = text;
        await sendText(text);
      } catch (e) {
        addMsg(
          "assistant",
          "❌ Transcription failed: " +
            (e instanceof Error ? e.message : String(e))
        );
      }
    };

    mediaRecorder.start();
    micBtn.classList.add("recording");
  } catch {
    alert("Microphone access denied");
  }
});

// ── webcam ───────────────────────────────────────────────
camBtn.addEventListener("click", async () => {
  if (webcamStream) {
    closeWebcam();
    return;
  }
  try {
    webcamStream = await navigator.mediaDevices.getUserMedia({ video: true });
    webcamEl.srcObject = webcamStream;
    webcamCtr.classList.remove("hidden");
  } catch {
    alert("Camera access denied");
  }
});

function closeWebcam(): void {
  webcamStream?.getTracks().forEach((t) => t.stop());
  webcamStream = null;
  webcamEl.srcObject = null;
  webcamCtr.classList.add("hidden");
}

closeWcBtn.addEventListener("click", closeWebcam);

// ── shared image send ────────────────────────────────────
async function sendImageWithQuestion(
  images: string[],
  question?: string,
  displayText?: string
): Promise<void> {
  const userText = question ?? (msgInput.value.trim() || "What do you see?");
  const userBubble = addMsg("user", displayText ?? userText);
  const holder = document.createElement("div");
  if (images.length > 1) holder.className = "chat-frames";
  for (const b64 of images) {
    const img = document.createElement("img");
    img.src = `data:image/jpeg;base64,${b64}`;
    img.className = "chat-img";
    holder.appendChild(img);
  }
  userBubble.querySelector(".text")!.before(holder);
  msgInput.value = "";

  const bubble = addMsg(
    "assistant",
    images.length > 1 ? "⏳ analysing frames…" : "⏳ analysing image…"
  );
  try {
    const res = await fetch("/api/chat-video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: userText, images }),
    });
    await streamResponse(res, bubble);
  } catch (e) {
    bubble.querySelector(".text")!.textContent =
      "❌ " + (e instanceof Error ? e.message : String(e));
  }
}

captureBtn.addEventListener("click", async () => {
  const canvas = document.createElement("canvas");
  canvas.width = webcamEl.videoWidth;
  canvas.height = webcamEl.videoHeight;
  canvas.getContext("2d")!.drawImage(webcamEl, 0, 0);

  const dataUrl = canvas.toDataURL("image/jpeg", 0.8);
  const b64 = dataUrl.split(",")[1];
  await sendImageWithQuestion([b64]);
});

// ── upload image ─────────────────────────────────────────
uploadBtn.addEventListener("click", () => uploadInput.click());

uploadInput.addEventListener("change", async () => {
  const file = uploadInput.files?.[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = async () => {
    const dataUrl = reader.result as string;
    const b64 = dataUrl.split(",")[1];
    await sendImageWithQuestion([b64]);
  };
  reader.readAsDataURL(file);
  uploadInput.value = "";
});

// ── file upload (PDF / text) ─────────────────────────────
fileBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
  const file = fileInput.files?.[0];
  if (!file) return;

  const userText = msgInput.value.trim() || "Please summarise this document.";
  addMsg("user", `📄 [${file.name}] ${userText}`);
  msgInput.value = "";

  const bubble = addMsg("assistant", "⏳ reading file…");

  const fd = new FormData();
  fd.append("file", file);
  fd.append("message", userText);

  try {
    const res = await fetch("/api/chat-file", { method: "POST", body: fd });
    await streamResponse(res, bubble);
  } catch (e) {
    bubble.querySelector(".text")!.textContent =
      "❌ " + (e instanceof Error ? e.message : String(e));
  }

  fileInput.value = "";
});

// ── stream / clip analysis (ffmpeg demux) ────────────────
interface DemuxResult {
  source?: string;
  duration?: number;
  frames?: string[];
  audio?: string | null;
  audio_mime?: string | null;
  captions?: { text: string; source: string; language?: string | null } | null;
  error?: string;
}

function base64ToBlob(b64: string, type: string): Blob {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}

async function analyseMedia(
  fd: FormData, label: string, comparison: boolean
): Promise<void> {
  const question =
    msgInput.value.trim() || (comparison
      ? "Compare the captions with the audio transcript."
      : "Describe and summarise this video clip.");
  if (comparison) fd.append("compare_captions", "true");
  msgInput.value = "";
  closeStreamPanel();

  const status = addMsg("assistant", `⏳ loading & demuxing ${label} with ffmpeg…`);
  const statusText = status.querySelector(".text")!;
  try {
    const res = await fetch("/api/demux", { method: "POST", body: fd });
    const data: DemuxResult = await res.json();
    if (data.error) throw new Error(data.error);

    const frames = data.frames ?? [];
    const source = data.source || label;

    if (comparison) {
      if (!data.captions?.text || !data.audio) {
        throw new Error("Both captions and audio are required for comparison");
      }
      statusText.textContent = "⏳ transcribing audio track…";
      const transcript = await transcribeAudio(
        base64ToBlob(data.audio, data.audio_mime || "audio/webm"),
        "stream-audio.webm"
      );
      if (!transcript.text?.trim()) {
        throw new Error("No speech was transcribed; cannot compare captions");
      }
      const captionSource = data.captions.source +
        (data.captions.language ? ` (${data.captions.language})` : "");
      const timedTranscript = transcript.segments?.length
        ? `\n\nTranscript segments (seconds):\n${JSON.stringify(transcript.segments)}`
        : "";
      const timingWarning = fd.has("url")
        ? "\n\nStream captions may be incomplete or misaligned; do not conclude that speech is missing solely from timestamps or absent cues."
        : "";
      const prompt = `${question}\n\nCompare the caption text and audio transcript below. ` +
        "Identify matching speech, missing or extra words, and meaningful discrepancies. " +
        "Treat timestamps as approximate and do not invent missing content." + timingWarning +
        `\n\nCaptions (${captionSource}, SRT):\n${data.captions.text}` +
        `\n\nAudio transcript:\n${transcript.text}${timedTranscript}`;
      status.remove();
      await sendText(
        prompt,
        `🔤 [${source}] ${question}\n\nCaptions (${captionSource}):\n${data.captions.text}` +
          `\n\n🎧 ${transcript.text}` +
          (timingWarning ? "\n\n⚠️ Stream captions may be incomplete or misaligned." : "")
      );
      return;
    }

    // audio track → same path as mic recording
    let transcript = "";
    if (data.audio) {
      statusText.textContent = "⏳ transcribing audio track…";
      try {
        transcript = (
          await transcribeBlob(
            base64ToBlob(data.audio, data.audio_mime || "audio/webm"),
            "stream-audio.webm"
          )
        ).trim();
      } catch (e) {
        addMsg(
          "assistant",
          "⚠️ Audio transcription failed, continuing without it: " +
            (e instanceof Error ? e.message : String(e))
        );
      }
    }
    status.remove();

    const dur = data.duration ? ` (${data.duration.toFixed(1)}s)` : "";
    let prompt = question;
    if (frames.length) {
      prompt += `\n\nThe images are ${frames.length} frames sampled in order from the clip "${source}"${dur}.`;
    }
    if (transcript) {
      prompt += `\n\nAudio transcript of the clip:\n"""\n${transcript}\n"""`;
    } else if (!frames.length) {
      throw new Error("No usable video frames or speech found in the clip");
    }
    const display =
      `🎞️ [${source}]${dur} ${question}` +
      (transcript ? `\n\n🎧 ${transcript}` : "");

    // video frames → same path as webcam capture
    if (frames.length) {
      await sendImageWithQuestion(frames, prompt, display);
    } else {
      await sendText(prompt, display);
    }
  } catch (e) {
    if (!status.isConnected) chat.appendChild(status);
    statusText.textContent =
      "❌ " + (e instanceof Error ? e.message : String(e));
  }
}

function closeStreamPanel(): void {
  streamCtr.classList.add("hidden");
  streamUrl.value = "";
}

function openStreamPanel(comparison: boolean): void {
  if (!streamCtr.classList.contains("hidden") && compareCaptions === comparison) {
    closeStreamPanel();
    return;
  }
  compareCaptions = comparison;
  streamUrlBtn.textContent = comparison ? "▶ Compare URL" : "▶ Analyse URL";
  streamFileBtn.textContent = comparison ? "📁 Compare clip" : "📁 Choose clip";
  streamCtr.classList.remove("hidden");
  streamUrl.focus();
}
streamBtn.addEventListener("click", () => openStreamPanel(false));
captionBtn.addEventListener("click", () => openStreamPanel(true));
closeStreamBtn.addEventListener("click", closeStreamPanel);

streamUrlBtn.addEventListener("click", () => {
  const url = streamUrl.value.trim();
  if (!url) {
    streamUrl.focus();
    return;
  }
  const fd = new FormData();
  fd.append("url", url);
  analyseMedia(fd, "stream", compareCaptions);
});
streamUrl.addEventListener("keydown", (e: KeyboardEvent) => {
  if (e.key === "Enter") streamUrlBtn.click();
});

streamFileBtn.addEventListener("click", () => streamFileInput.click());
streamFileInput.addEventListener("change", () => {
  const file = streamFileInput.files?.[0];
  streamFileInput.value = "";
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  analyseMedia(fd, file.name, compareCaptions);
});

// ── clear ────────────────────────────────────────────────
clearBtn.addEventListener("click", async () => {
  await fetch("/api/clear", { method: "POST" });
  chat.innerHTML = "";
});
