<div align="center">

# 🪶 Parley

### Record a meeting. Get a clean transcript, a sharp summary, and your action items — all on your own hardware.

**Parley** is a self-hosted meeting recorder and AI notebook. It captures audio, transcribes it with speaker labels, cleans it up, and runs a multi-pass analysis that pulls out the summary, decisions, action items, risks, and figures — then files them next to a full Obsidian-style notes & tasks workspace. No cloud. No API keys. No subscription. Your conversations never leave your machine.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Local-first](https://img.shields.io/badge/local--first-no%20cloud-success)
![PWA](https://img.shields.io/badge/PWA-installable-5A0FC8)

</div>

---

## Why Parley?

Meeting-AI tools are everywhere — but they ship your most sensitive conversations to someone else's servers and bill you monthly for the privilege. Parley does the opposite:

- **It's yours.** Runs entirely on your own hardware against local models (Ollama, WhisperX, Qdrant). Nothing is uploaded anywhere.
- **It's honest about your data.** Notes and meetings are plain Markdown files on disk — open them in Obsidian, grep them, back them up, leave any time.
- **It actually does the work.** Not just a transcript dump: speaker identification, transcript cleanup, a six-pass analysis, auto-tagging, and semantic links between your notes and the meetings they relate to.
- **It's a notebook, not just a recorder.** A real CodeMirror editor with wiki-links, backlinks, a tasks dashboard, file attachments, and a built-in sketch canvas.

> Built for a homelab, in the spirit of Immich and Paperless: self-hosted, private, and genuinely pleasant to use.

## What it does

### 🎙️ Capture
- Upload (or record) audio → transcription with **speaker diarization** (`SPEAKER_00`, …).
- **LLM transcript cleanup** — fixes ASR mishearings, acronyms and punctuation while preserving every word.
- **Speaker identification** — infers real names and roles from what people actually say.

### 🧠 Understand
- A **six-pass analysis** (structured JSON, enforced) extracting: title & summary, topics, **action items**, decisions & open questions, concerns & risks, key figures, and sentiment.
- **Auto-tagging** — category, keywords, and entities (people / companies / projects / tech / dates).
- **Hierarchical summarisation** for long meetings and **cross-meeting insights** across a set.
- **Semantic search** and a **RAG chat** — ask questions across everything you've recorded.

### 📝 Notes & Tasks
- Markdown notes with a **CodeMirror 6** editor: headings, checkboxes, **`[[wiki-links]]`** + backlinks, autocomplete, live preview.
- A **Tasks dashboard** with full CRUD — create, edit, complete, and group tasks by due date / owner / priority. Meeting action items are tracked right alongside your own.
- **Automatic linking** between notes and the meetings they relate to (and back again).
- **File attachments** (drag / drop / paste) and a built-in **SVG sketch canvas**.
- Installable **PWA** with an offline app shell and a light/dark theme.

## How it works

```
             ┌──────────────┐      audio       ┌────────────┐
  browser ──▶│   Parley     │ ───────────────▶ │  WhisperX  │  speech-to-text
    (PWA)    │  (FastAPI)   │                  └────────────┘  + diarization
             │              │ ── prompts ────▶ ┌────────────┐
             │              │   embeddings     │   Ollama   │  LLM + embeddings
             │              │ ───────────────▶ └────────────┘
             │              │ ── vectors ────▶ ┌────────────┐
             └──────────────┘                  │   Qdrant   │  semantic search
                                               └────────────┘  & auto-linking
```

Your meetings and notes are plain files on disk (`MEETINGS_DIR`, `NOTES_DIR`) — portable and Obsidian-compatible. Vector indexing is best-effort and off the request path, so the UI stays snappy.

## Flexible by design

Parley runs entirely on **local Ollama** out of the box — that's the whole point. But the AI backends are swappable:

- **Analysis & embeddings** (transcript cleanup, the six-pass extraction, auto-tagging, summaries, semantic search) run on your local **Ollama** models. **OpenWebUI is not required** — it's only an optional alternative backend for the chat box.
- **Chat** ("ask your meetings") can point at Ollama (default), an existing **OpenWebUI** instance, or **any OpenAI-compatible endpoint** — so you can use a different local server, or a cloud model, just for chat if you want.
- **Transcription** uses your local **WhisperX**.

Nothing leaves your machine unless *you* explicitly configure the chat box to use a remote endpoint.

## Quick start

You'll need **Docker**, an **Ollama** instance (with an LLM such as `qwen3.5:9b` and an embedding model like `qwen3-embedding:0.6b`), a **Qdrant** instance, and a **WhisperX / faster-whisper** endpoint for transcription. (Notes & Tasks work without WhisperX — that's only needed to transcribe audio.)

```bash
git clone https://github.com/craiglush/parley.git
cd parley
cp .env.example .env                          # point URLs/models at your stack
cp docker-compose.example.yml docker-compose.yml
docker compose up -d --build
# open http://localhost:8191
```

Ollama usually runs natively on the host — reach it from the container via `host.docker.internal`.

## Configuration

Everything is environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama base URL |
| `OLLAMA_MODEL` | `qwen3.5:9b` | LLM for analysis / cleanup / chat |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Embedding model (1024-dim) |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant base URL |
| `WHISPERX_URL` | `http://whisperx:8000` | Speech-to-text endpoint |
| `OPENWEBUI_URL` / `OPENWEBUI_API_KEY` | – | Optional: route chat via an OpenWebUI instance |
| `MEETINGS_DIR` / `NOTES_DIR` | `/data/meetings` `/data/notes` | Data dirs (mount as volumes) |
| `MAX_UPLOAD_SIZE` | `524288000` | Max upload size (bytes) |
| `ALLOWED_CORS_ORIGINS` / `ALLOWED_FRAME_ORIGINS` | `*` | CORS / iframe embedding |

Prompts, model, and temperatures are also editable at runtime in the in-app settings (persisted to `MEETINGS_DIR/settings.json`).

## Building the editor bundle

The CodeMirror 6 editor ships as a prebuilt offline bundle (`static/vendor/codemirror.bundle.js`, already included). To rebuild it:

```bash
cd frontend-build
npm install
node build.mjs        # → ../static/vendor/codemirror.bundle.js
```

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

## Project layout

```
app.py            FastAPI app: routes + the meeting pipeline
llm.py            Ollama prompt/response helpers (num_ctx sizing, JSON parsing, think:false)
stt.py            WhisperX client + audio pre-processing
vector.py         Qdrant + Ollama embeddings (meetings collection)
notes_store.py    Notes CRUD (.md + frontmatter), folders, trash, attachments, auto-tags
notes_vectors.py  Notes semantic index (separate Qdrant collection)
tasks_store.py    GFM-checkbox + inline-metadata task parsing / rollup / CRUD
storage.py        Pure helpers (artifact IDs, SRT, atomic writes)
static/           Vanilla-JS PWA (index.html, app.js, notes-tasks.*, service worker, icons)
frontend-build/   esbuild source for the CodeMirror bundle (host-only, not shipped in the image)
tests/            pytest suite
```

## Built with

[FastAPI](https://fastapi.tiangolo.com/) · [Ollama](https://ollama.com/) · [WhisperX](https://github.com/m-bain/whisperX) · [Qdrant](https://qdrant.tech/) · a dependency-light vanilla-JS PWA · and [CodeMirror 6](https://codemirror.net/), [marked](https://marked.js.org/), and [DOMPurify](https://github.com/cure53/DOMPurify) (all MIT) for the editor.

## License

[MIT](LICENSE) © 2026 craiglush — do what you like, no warranty.
