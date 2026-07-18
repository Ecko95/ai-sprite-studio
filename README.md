# AI Sprite Studio

A **local, single-user** workflow for turning a character image into reproducible,
engine-neutral pixel-art sprite sheets. It wraps a pinned copy of the
[`sprite-gen`](https://github.com/aldegad/sprite-gen) engine and the prompt/reference
workflow from
[`ai-pixel-snapped-game-sprites`](https://github.com/chongdashu/ai-pixel-snapped-game-sprites),
and adds a filesystem-backed project store, an immutable artifact/provenance model,
explicit approval gates, and a loopback-only web app.

Everything runs on your machine. The fully-local path — **upload → snap → curate →
atlas → inspect** — needs no AI provider once dependencies are installed.

---

## Project status

This repository is being built task-by-task against
`docs/superpowers/plans/2026-07-17-ai-sprite-studio.md`. What exists today:

| Area | Status |
| --- | --- |
| Upstream asset pinning + provenance lock (`upstream-lock.json`) | ✅ |
| Validated contracts + atomic, descriptor-pinned project storage | ✅ |
| Loopback web app, typed HTTP API, durable job runner | ✅ |
| Versioned prompt registry + pipeline DAG / approval gates / preflight | ✅ |
| **Canonical true-pixel sprite engine + local upload path (`SpriteEngine`)** | ✅ |
| OpenAI image generation/editing, direction/action gen, curator UI, Higgsfield walk, chat orchestration, QA/export, packaging | ⏳ Tasks 6–12, not yet built |

Because the AI provider **job handlers** (Tasks 6+) are not wired in yet, the job queue
accepts and persists jobs but marks them `attention_required` (`no_job_handler`) when
run. The **`SpriteEngine`** below is fully functional today as a Python library.

---

## Design principles

These are hard constraints, enforced in code and tests:

- **Local only.** Binds to `127.0.0.1`, never `0.0.0.0`. No database, broker, Docker,
  Electron, or cloud storage.
- **One canonical pixel engine:** the pinned `sprite-gen` revision. No competing snapper.
- **Immutable history.** Raw uploads and prior candidates are preserved; regeneration
  never overwrites accepted work. Every stored artifact is content-addressed
  (SHA-256) and records its `kind`, `variant`, and dependency lineage.
- **Reproducible & provenance-checked.** On-disk run state is validated by artifact
  **role** (kind/variant) and **lineage** (dependencies) and by byte-equality against
  the immutable store before it is trusted.
- **Explicit gates.** External generation requires a visible request-count preflight
  and an explicit user approval per gate (`base`, `directions`, `frames`, `motion`,
  `export`).
- **Locked canonical output:** 128 logical-pixel grid, integer 2× nearest-neighbour
  export, binary alpha, 12-color shared palette, integer anchors, foot anchor
  `(128, 255)` on a `256×256` cell.

---

## Requirements

- **Python `>=3.12,<3.13`**
- [`uv`](https://docs.astral.sh/uv/) for dependency management (the lockfile pins
  everything, including the exact `sprite-gen` git revision).

---

## Install

```bash
uv sync --locked --all-groups
```

This installs runtime deps (Starlette, Uvicorn, Pydantic, Pillow, `sprite-gen`,
OpenAI SDK, MCP SDK, `imageio-ffmpeg`) plus the `dev` group (pytest, ruff,
Playwright).

Verify the pinned upstream assets match the lockfile:

```bash
uv run python scripts/sync_upstreams.py --check
```

---

## Run the app

```bash
uv run ai-sprite-studio serve            # ephemeral port, opens a browser
uv run ai-sprite-studio serve --port 8765
uv run ai-sprite-studio serve --workspace /path/to/workspace
```

`serve` binds to `127.0.0.1`, prints the actual URL (e.g. `http://127.0.0.1:8765/`)
to stdout, and attempts to open a browser. Stop with `Ctrl-C`.

### Generate a base character (gpt-image)

`genbase` renders the pinned `base_generation` prompt (from
`ai-pixel-snapped-game-sprites`) with your concept, calls OpenAI `gpt-image-1` at the
locked `1024×1024` / `high` settings, writes the chroma-green PNG (plus a
`.prompt.md` provenance sidecar), and ingests it as an immutable `input` artifact so
the canonical snap (`extract`) can run next.

Set `OPENAI_API_KEY` in the environment or a local `.env` (git-ignored, loaded
automatically):

```bash
echo 'OPENAI_API_KEY=sk-...' > .env    # or: export OPENAI_API_KEY=sk-...
uv run ai-sprite-studio genbase --concept "young pirate boy in a black tricorn hat" --out base.png
uv run ai-sprite-studio genbase --concept "..." --project <existing-project-id> --workspace /path/to/ws
```

`--costume` and `--silhouette` refine the identity tokens; `--model`/`--quality`
override the defaults. This spends OpenAI credits — it is the only path here that
calls a provider. Feed the result into `SpriteEngine.prepare(...)` → `extract(...)`
to snap it to the grid.

**Keyless / manual (`--dry-run`)** — to use your ChatGPT (or Codex TUI) subscription
instead of an API key, `--dry-run` prints the ready-to-paste prompt and writes the
`.prompt.md` provenance sidecar **without** calling any provider (no key, no store
writes). Paste it into ChatGPT / the interactive Codex image tool, save the PNG, then
bring it back with `prep` → upload. (There is no keyless *headless* path: the Codex
built-in image tool is interactive-TUI-only, and its CLI fallback needs
`OPENAI_API_KEY`.)

```bash
uv run ai-sprite-studio genbase --dry-run --concept "young pirate boy..." --out base.png
uv run ai-sprite-studio genactions --dry-run --project <id> --state attack --out attack.png
```

### Generate an action pose board (gpt-image)

`genactions` turns a short action intent into the full-spec `action_poseboards`
prompt (prompt 04) — filling per-frame motion beats, grid, and the locked direction
phrasing — then calls gpt-image as a **multi-image edit** (your snapped anchor + the
pinned pose-board guide) to produce a `1536×1024` pose board on chroma green:

```bash
uv run ai-sprite-studio genactions --project <project-id> --state attack --out attack.png
uv run ai-sprite-studio genactions --project <id> --state idle --anchor down-snapped.png \
  --frames-desc "10-frame idle: slow breathing, tiny head bob" --out idle.png
```

States come from the project (`idle`/`attack`/`hurt`/`jump`/`death`); frame counts are
the locked presets. `--direction` is `down` only until directional anchors exist. The
board is an **intermediate** — recover + snap its frames via the frame-recovery stage;
never grid-crop it (poses cross cell borders by design). Spends OpenAI credits.

### Assemble frames image-by-image (`combine`)

If you have each frame as a **separate image** (or several rows/sheets to join),
`combine` stitches them into one 1×N row in the order given (each normalised to the
largest common cell on chroma green):

```bash
uv run ai-sprite-studio combine --out row.png frame1.png frame2.png frame3.png
```

Then upload `row.png` with **frames = N**.

### Reshape a grid pose board into a row (`regrid`)

The snap is a **component-row** engine — it reads frames from a **single horizontal
row (1×N)**. A generated pose board is usually a 2D grid (e.g. 4×3), which the snap
mis-slices into full-height columns (two stacked poses per "frame"). `regrid` cuts the
first `--frames` cells in reading order and lines them up in one row:

```bash
uv run ai-sprite-studio regrid --in poseboard.png --out row.png --cols 4 --rows 3 --frames 8
```

Then upload `row.png` with **frames = N**. (The proper upstream path is the
frame-recovery stage; `regrid` is the local bridge until that's built.)

### Prep an existing image for snapping

The snap keys alpha off the flat `#00FF00` background. An image on white (or any
flat colour) won't key — the background survives as opaque pixels, and because the
whole cell is then "the sprite", the snap can't center it either. `prep` floods the
edge-connected background to chroma green (interior same-colour regions survive) and
pads to a square, so the existing snap keys **and** centers it (`align_x`
alpha-centroid, `align_y` bottom) automatically:

```bash
uv run ai-sprite-studio prep --in ghost.png --out ghost-green.png
uv run ai-sprite-studio prep --in ghost.png --out ghost-green.png --tolerance 60 --pad 0.15
```

Then upload `ghost-green.png` through the curator UI (or `SpriteEngine.ingest_upload`).
Raise `--tolerance` if a tinted background isn't fully removed.

### Workspace / data layout

The default workspace is platform-specific:

- Linux: `${XDG_DATA_HOME:-~/.local/share}/ai-sprite-studio`
- macOS: `~/Library/Application Support/ai-sprite-studio`
- Windows: `%LOCALAPPDATA%\ai-sprite-studio`

All projects, artifacts, jobs, events, and per-project sprite-gen `run/` directories
live under that workspace as plain files. Nothing leaves your machine.

---

## Security model (web app)

The app is meant to be reachable only from your own machine. The middleware enforces:

- **Loopback `Host` only** — non-loopback hosts get `400 invalid_host` (blocks
  DNS-rebinding).
- **Same-origin, CSRF-protected mutations** — `POST/PATCH/PUT/DELETE` under
  `/api/v1/` require a loopback same-origin `Origin` header **and** a valid CSRF
  token. `GET /` issues a `Strict`, `HttpOnly` session cookie and embeds the token in
  `<meta name="csrf-token">`; send it back in the `X-CSRF-Token` header.
- **Hardened responses** — `Content-Security-Policy: default-src 'self'` and
  `X-Content-Type-Options: nosniff` on every response.

---

## HTTP API

All routes are JSON and rooted at `/api/v1`. Errors share the shape
`{"code", "message", "retryable", "details"}`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/projects` | List projects |
| `POST` | `/api/v1/projects` | Create a project `{ "name": "..." }` |
| `GET` | `/api/v1/projects/{id}` | Project detail (config + artifacts + approvals) |
| `PATCH` | `/api/v1/projects/{id}` | Rename `{ "name": "..." }` |
| `POST` | `/api/v1/projects/{id}/jobs` | Enqueue a job `{ "command", "payload" }` |
| `GET` | `/api/v1/jobs/{id}` | Job record / status |
| `GET` | `/api/v1/jobs/{id}/events` | Server-Sent Events stream (`Last-Event-ID` supported) |
| `POST` | `/api/v1/jobs/{id}/cancel` | Request cancellation |

**Jobs** are serialized, durable, and idempotent: an identical `(command, payload)`
that already `succeeded` is returned instead of re-run (keyed by a SHA-256
`input_hash`). Statuses: `queued → running → succeeded | failed | canceled`, plus
`waiting_approval`, `cancel_requested`, and `attention_required` (e.g. after an
interrupted run, or when no handler is configured).

Job **commands** (`JobCommand`) span the full pipeline —
`normalize_input`, `generate_base`, `edit_candidate`, `snap_candidate`,
`generate_directions`, `generate_poseboards`, `generate_walk`, `ingest_walk_video`,
`recover_frames`, `save_curation`, `ai_edit_frame`, `run_qa`, `export_pack`.
The handlers that execute the provider-backed commands arrive in Tasks 6–12.

### Example: create a project (with CSRF)

```bash
# 1. GET / to obtain the session cookie and CSRF token, then reuse both.
curl -s -c jar.txt http://127.0.0.1:8765/ | grep csrf-token   # read the token
curl -s -b jar.txt -X POST http://127.0.0.1:8765/api/v1/projects \
  -H 'Origin: http://127.0.0.1:8765' \
  -H 'X-CSRF-Token: <token-from-meta-tag>' \
  -H 'Content-Type: application/json' \
  -d '{"name":"My ranger"}'
```

---

## The sprite engine (Python library)

`SpriteEngine` is the local, provider-free heart of the studio. It takes a single
static PNG/JPEG/WebP upload and drives the canonical pipeline. Every step verifies
inputs, persists immutable artifacts with lineage, and validates canonical geometry
(binary alpha, ≤12-color palette, foot anchor, logical grid).

```python
from pathlib import Path
from ai_sprite_studio.project_store import ProjectStore
from ai_sprite_studio.contracts import ProjectConfig
from ai_sprite_studio.sprite_engine import SpriteEngine

store = ProjectStore("/tmp/asworkspace")
project = store.create(ProjectConfig(name="My ranger"))
engine = SpriteEngine(store)

# 1. Ingest one immutable source image (validated + content-addressed).
data = Path("ranger.png").read_bytes()
uploaded = engine.ingest_upload(
    project.id, data, media_type="image/png", filename="ranger.png"
)

# 2. Prepare a numeric, upload-only run (1–12 frames laid out in a row).
prepared = engine.prepare(project.id, uploaded.id, frames=4)

# 3. Extract the frames. "components" recovers separated poses; use
#    "projection" explicitly for complex inputs.
extracted = engine.extract(project.id, segmentation="components")

# 4. Curate: load the sidecar + its revision, then stamp an edited curation.
snapshot = engine.load_curation(project.id)
engine.stamp_curation(project.id, {
    "version": 1,
    "kind": "sprite-gen-curation",
    "runRevision": snapshot.run_revision,
    "states": {"upload": {"selected": [0, 1, 2, 3]}},
})

# 5. Compose the atlas + manifest, and produce a QA inspection report.
composed = engine.compose(project.id)
inspection = engine.inspect(project.id)
```

Pipeline stages and what they guarantee:

- **`ingest_upload`** — validates format/declared type/dimensions (rejects animated,
  oversized, or decompression-bomb images) and stores one immutable `input`/`raw`
  artifact.
- **`prepare`** — builds a fresh `sprite-request.json`, writes the immutable raw
  source into the run's raw slot, and records the request artifact.
- **`extract`** — re-binds the *verified* immutable source before extraction (so a
  mutated on-disk raw file is never consumed), then recovers per-frame `plain` and
  `pixel` twins, validates canonical geometry, and emits preview contact
  sheets/GIFs. Re-extraction clears stale curation/atlas/inspection lineage.
- **`load_curation` / `stamp_curation`** — read/write the curation sidecar with
  optimistic revision checking; every stamp is validated (selected/deleted/order
  indices, bounded pixel edits, allowed transforms) and stored as an artifact whose
  dependencies pin the exact pixel frames it was made against.
- **`compose`** — bakes selected frames into a canonical atlas + manifest, validating
  palette, alpha, grid, and geometry.
- **`inspect`** — a redacted, immutable QA report.

Every state-pointed artifact is re-checked by **role and lineage** and by
byte-equality against the store on each operation, so a tampered `run/` directory or
engine-state file fails closed rather than producing a mislabelled or mis-parented
sprite.

---

## Contracts & storage

- **`contracts.py`** — Pydantic models with `extra="forbid"`: `ProjectConfig`
  (states, cell, palette/alpha locks, artifacts, approvals), `ArtifactRef`
  (id/kind/variant/sha256/dependencies/`stale`), `Approval`, `JobRecord`, `ApiError`.
  Core state presets (`idle`, `walk`, `attack`, `hurt`, `jump`, `death`) and the
  top-down four-way direction order are locked.
- **`project_store.py`** — atomic, crash-safe, descriptor-pinned filesystem storage.
  Artifacts are content-addressed and verified on read; a single-writer runner lock
  serializes job execution; approvals can invalidate dependent artifacts. Relative
  paths are validated against traversal on both POSIX and Windows.

---

## Prompts & pipeline (provider-free scaffolding)

- **`prompts.py`** — the eight upstream prompts converted into a versioned stage
  registry. Each rendered prompt records source repo, locked revision, source path
  and SHA-256, substitutions, and a rendered digest, so any generation is traceable
  back to an exact upstream template.
- **`pipeline.py`** — the artifact dependency DAG, approval-gate policy, and a pure
  **request-count preflight** (no side effects) so the number of provider calls is
  visible before anything runs.

---

## Upstream assets & licensing

Reference assets from the two upstream projects are vendored under
`src/ai_sprite_studio/assets/upstream/` and pinned by revision + SHA-256 in
`upstream-lock.json`. `scripts/sync_upstreams.py --check` verifies they are intact.

- `ai-pixel-snapped-game-sprites` — MIT
- `sprite-gen` — Apache-2.0

See `THIRD_PARTY_NOTICES.md`. **New application code in this repo is not yet
licensed** — the third-party notices are mandatory regardless.

---

## Development

```bash
uv run pytest -q          # full test suite
uv run ruff check .       # lint
```

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, a wheel build, the upstream
provenance check, `pytest`, and `ruff` on Python 3.12.

### Repository layout

```
src/ai_sprite_studio/
  cli.py            # `ai-sprite-studio serve` entry point
  app.py            # Starlette app, loopback + CSRF security middleware
  api.py            # typed JSON routes
  jobs.py           # durable, serialized JobRunner + SSE events
  sprite_engine.py  # canonical local upload → snap → curate → atlas engine
  contracts.py      # Pydantic domain contracts
  project_store.py  # atomic content-addressed filesystem store
  prompts.py        # versioned prompt stage registry
  pipeline.py       # dependency DAG, approval gates, request preflight
  assets/upstream/  # pinned, provenance-locked upstream reference assets
docs/superpowers/   # implementation plan + workflow playbook
scripts/            # sync_upstreams.py
tests/              # pytest suite (mirrors each module)
```
