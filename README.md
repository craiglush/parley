# Meeting Service

A self-hosted **meeting capture, transcription, and AI-analysis** app — with a built-in
**Obsidian-style notes & tasks workspace**. Upload an audio recording and it transcribes,
diarizes, cleans up, and analyzes the conversation locally; take linked Markdown notes and
track tasks alongside it. Everything runs against your own local models (Ollama + WhisperX
+ Qdrant) — no third-party API required.

> Backend: **FastAPI** (Python). Frontend: a dependency-light **vanilla-JS PWA** with an
> offline-bundled **CodeMirror 6** editor. Vector search via **Qdrant**, LLM + embeddings
> via **Ollama**, speech-to-text via **WhisperX / faster-whisper**.

## Features

**Meetings**
- Audio upload → transcription with **speaker diarization** (`SPEAKER_00`, …).
- **LLM transcript cleanup** — fixes ASR mishearings/punctuation while preserving wording.
- **Speaker identification** — infers real names/roles from conversation content.
- **6-pass structured analysis** (Ollama JSON-schema structured output): title & summary,
  topics, action items, decisions & open questions, concerns/risks, figures, sentiment.
- **Auto-tagging** — category, keywords, and entities (people/companies/projects/tech/dates).
- **Hierarchical summarization** for long meetings; **cross-meeting insights** across a set.
- **Semantic search** and a **RAG chat** ("ask your meetings").
- Thinking-model aware (sets `think:false`) and **bounded `num_ctx`** so long transcripts
  aren't silently truncated.

**Notes & Tasks**
- Markdown notes (`.md` + YAML frontmatter) with a **CodeMirror 6** editor: headings, bold,
  checkboxes, wiki-links (`[[note]]`) + backlinks, autocomplete, live preview.
- Folders, daily journal, rename/move, trash; substring **and semantic** search.
- **Tasks dashboard** — GFM checkboxes + Obsidian-style inline metadata
  (`@owner 📅 due ⏫`), rollup across notes, toggle write-back, and ingestion of meeting
  action items.
- **Auto-tagging** of notes (idle-gated background worker) and **automatic semantic linking**
  between notes and related meetings (and vice-versa).
- **File attachments** (drag / drop / paste) and a **built-in SVG sketch canvas**.
- Installable **PWA** with an offline app shell; light/dark theme.

## Architecture

```
            ┌──────────────┐      audio       ┌────────────┐
 browser ──▶│ FastAPI app  │ ───────────────▶ │  WhisperX  │  (STT + diarization)
   (PWA)    │  (app.py)    │                  └────────────┘
            │              │ ── prompts ────▶ ┌────────────┐
            │              │   embeddings     │   Ollama   │  (LLM + embeddings)
            │              │ ───────────────▶ └────────────┘
            │              │ ── vectors ────▶ ┌────────────┐
            └──────────────┘                  │   Qdrant   │  (semantic search/linking)
                                              └────────────┘
```

Notes and meetings are plain files on disk (`MEETINGS_DIR`, `NOTES_DIR`) — portable and
Obsidian-compatible. Vector indexing is best-effort and off the request path.

## Requirements

- **Docker** (or Python 3.11 + `ffmpeg` to run it directly).
- An **Ollama** instance with an LLM (e.g. `qwen3.5:9b`) and an embedding model
  (`qwen3-embedding:0.6b`, 1024-dim).
- A **Qdrant** instance.
- A **WhisperX / faster-whisper** OpenAI-compatible STT endpoint (only needed for meeting
  transcription; the notes/tasks features work without it).

## Quick start

```bash
cp .env.example .env                      # adjust URLs/model names as needed
cp docker-compose.example.yml docker-compose.yml
docker compose up -d --build
# open http://localhost:8191
```

Point `WHISPERX_URL` and `OLLAMA_URL` at your own instances (Ollama usually runs natively
on the host — reached from the container via `host.docker.internal`).

## Configuration

All via environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama base URL |
| `OLLAMA_MODEL` | `qwen3.5:9b` | LLM for analysis/cleanup/chat |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b` | Embedding model (1024-dim) |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant base URL |
| `WHISPERX_URL` | `http://whisperx:8000` | STT endpoint |
| `OPENWEBUI_URL` / `OPENWEBUI_API_KEY` | – | Optional: route chat via OpenWebUI |
| `MEETINGS_DIR` / `NOTES_DIR` | `/data/meetings` `/data/notes` | Data dirs (mount as volumes) |
| `MAX_UPLOAD_SIZE` | `524288000` | Max upload bytes |
| `ALLOWED_CORS_ORIGINS` / `ALLOWED_FRAME_ORIGINS` | `*` | CORS / framing |

Prompts, model, and temperatures are also editable at runtime via the in-app settings
(persisted to `MEETINGS_DIR/settings.json`).

## Building the editor bundle

The CodeMirror 6 editor is vendored as a prebuilt offline bundle at
`static/vendor/codemirror.bundle.js` (already included). To rebuild it:

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
app.py            FastAPI app: routes + meeting pipeline
llm.py            Ollama prompt/response helpers (num_ctx sizing, JSON parsing, think:false)
stt.py            WhisperX client + audio pre-processing
vector.py         Qdrant + Ollama embeddings (meetings collection)
notes_store.py    Notes CRUD (.md + frontmatter), folders, trash, attachments, auto-tags
notes_vectors.py  Notes semantic index (separate Qdrant collection)
tasks_store.py    GFM-checkbox + inline-metadata task parsing / rollup / write-back
storage.py        Pure helpers (artifact IDs, SRT, atomic writes)
static/           Vanilla-JS PWA (index.html, app.js, notes-tasks.*, service worker, icons)
frontend-build/   esbuild source for the CodeMirror bundle (host-only, not shipped in image)
tests/            pytest suite
```

## Third-party

The editor bundle includes [CodeMirror 6](https://codemirror.net/),
[marked](https://marked.js.org/), and [DOMPurify](https://github.com/cure53/DOMPurify) (all MIT).

## License

No license file is included yet — add one (e.g. MIT) before relying on this publicly.
Until then, default copyright applies.
