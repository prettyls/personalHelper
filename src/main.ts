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

let mediaRecorder: MediaRecorder | null = null;
let audioChunks: Blob[] = [];
let webcamStream: MediaStream | null = null;

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
async function sendText(text: string): Promise<void> {
  const trimmed = text.trim();
  if (!trimmed) return;

  addMsg("user", trimmed);
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

      const fd = new FormData();
      fd.append("audio", blob, "recording.webm");

      try {
        const res = await fetch("/api/transcribe", { method: "POST", body: fd });
        const data: { text?: string; error?: string } = await res.json();
        if (data.error) {
          addMsg("assistant", "❌ " + data.error);
          return;
        }
        // replace last user msg with transcript
        const msgs = chat.querySelectorAll(".msg.user");
        const lastUser = msgs[msgs.length - 1];
        lastUser.querySelector(".text")!.textContent = data.text!;
        await sendText(data.text!);
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
async function sendImageWithQuestion(b64: string): Promise<void> {
  const userText = msgInput.value.trim() || "What do you see?";
  const userBubble = addMsg("user", userText);
  const img = document.createElement("img");
  img.src = `data:image/jpeg;base64,${b64}`;
  img.className = "chat-img";
  userBubble.querySelector(".text")!.before(img);
  msgInput.value = "";

  const bubble = addMsg("assistant", "⏳ analysing image…");
  try {
    const res = await fetch("/api/chat-video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: userText, image: b64 }),
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
  await sendImageWithQuestion(b64);
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
    await sendImageWithQuestion(b64);
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

// ── clear ────────────────────────────────────────────────
clearBtn.addEventListener("click", async () => {
  await fetch("/api/clear", { method: "POST" });
  chat.innerHTML = "";
});
