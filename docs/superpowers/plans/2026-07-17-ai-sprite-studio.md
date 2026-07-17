# AI Sprite Studio v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task with review checkpoints.

**Goal:** Build a local AI sprite studio that generates or imports a character, converts it into reproducible true pixel art, creates four-way animation sets, supports AI and pixel-level editing, and exports an engine-neutral sprite pack.

**Architecture:** A Python 3.12 local web application wraps a pinned `sprite-gen` engine and the complete prompt/reference workflow from `ai-pixel-snapped-game-sprites`. OpenAI handles character images and poseboards; Higgsfield MCP handles walk videos. A filesystem-backed pipeline, immutable candidate history, and explicit quality gates keep every result recoverable and reproducible.

**Tech stack:** Python 3.12, Starlette, Uvicorn, Pydantic, OpenAI SDK, MCP Python SDK, Pillow, `imageio-ffmpeg`, vanilla HTML/CSS/ES modules, Canvas, pytest, Playwright, and `uv`.

## Research conclusions

- [`ai-pixel-snapped-game-sprites`](https://github.com/chongdashu/ai-pixel-snapped-game-sprites) is an MIT-licensed prompt, reference-image, and workflow repository rather than an executable application. Its eight prompts and reference assets should be preserved verbatim and converted into a versioned stage registry.
- [`sprite-gen`](https://github.com/aldegad/sprite-gen) is the executable foundation: an Apache-2.0 Python package with chroma removal, connected-component recovery, pixel-grid detection, palette and outline normalization, frame curation, atlas composition, manifests, and QA. Its pinned revision currently has a [passing upstream CI run](https://github.com/aldegad/sprite-gen/actions/runs/29475705279).
- The Excalidraw board remains the product workflow source: [AI pixel-snapped sprite pipeline](https://link.excalidraw.com/readonly/RE6Ajl2yEbDTpQrNiy8h).
- The requested “ChatGPT 2.0 image generation” API model is `gpt-image-2`. It supports generation, editing, multiple image references, and arbitrary valid dimensions, but not transparent backgrounds; therefore chroma generation and deterministic alpha recovery are required. The Image API is the preferred single-job interface, while the Responses API fits contextual chat. [OpenAI image-generation guide](https://developers.openai.com/api/docs/guides/image-generation), [GPT Image 2 model](https://developers.openai.com/api/docs/models/gpt-image-2).
- Higgsfield exposes its platform through OAuth MCP and includes Seedance 2.0 and other I2V models. Integration will use runtime tool discovery rather than hard-coded private tool names. [Higgsfield MCP](https://higgsfield.ai/mcp), [Higgsfield MCP guide](https://higgsfield.ai/academy/how-to-use/generate-ai-videos-from-claude-with-higgsfield-mcp), [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

## Global constraints

- Local, single-user application only; bind to loopback and never expose `0.0.0.0`.
- Use Python `>=3.12,<3.13` and lock all dependencies in `uv.lock`.
- Use one canonical pixel engine: pinned `sprite-gen`; do not add SpriteFusion or another competing snapper in v1.
- No React/Vite build, Electron/Tauri wrapper, Docker, database, Redis, broker, or cloud storage.
- Preserve raw assets and prior candidates; regeneration never overwrites accepted work.
- All external generation requires a visible request-count preflight and explicit user action.
- Store provider credentials outside project directories and never include them in exports.
- Keep original upstream prompts and documentation unmodified; derived templates must record their source SHA.
- Local upload, snapping, curation, QA, and export must work without any AI provider once dependencies are installed.
- New application code remains unlicensed until the repository owner selects a license; third-party licenses and notices are mandatory.

---

## Locked defaults

- View: top-down four-way, using `down`, `right`, `up`, `left`.
- Side policy: per-project symmetry toggle; default mirrored. Mirrored projects generate `right` and derive `left`; asymmetric projects generate both independently.
- Runtime cell: `256×256`; foot anchor: `(128,255)`.
- Logical pixel grid: `128×128`, exported at integer 2× nearest-neighbour scale. Advanced presets may select 64×64/4× or 256×256/1× before the base gate.
- Shared palette: 12 colors; binary alpha; integer transforms; no smoothing.
- Chroma: `#00FF00` unless `sprite-gen` detects unsafe proximity to character colors, then select the safest alternate and record it.
- OpenAI image model: `gpt-image-2-2026-04-21`, configurable.
- Chat/orchestration model: `gpt-5.6-terra`, configurable, with the returned model identifier recorded.
- One candidate per request; regeneration creates another candidate.
- Generation concurrency: one provider request at a time.
- Core animation preset:

| State | Frames | FPS | Loop | Generator |
|---|---:|---:|---|---|
| idle | 4 | 6 | Yes | GPT poseboard |
| walk | 8 | 10 | Yes | Higgsfield I2V |
| attack | 4 | 12 | No | GPT poseboard |
| hurt | 4 | 10 | No | GPT poseboard |
| jump | 4 | 10 | No | GPT poseboard |
| death | 6 | 8 | No | GPT poseboard |

The board-exact preset—idle 10, walk 8, attack 8, hurt 6, jump 6, death 10—remains available under Advanced with cost and frame-recovery warnings.

Custom states accept 1–12 frames, 1–30 FPS, loop/non-loop, a motion description, and `gpt_poseboard`, `higgsfield_i2v`, or `upload`. GPT states over six frames display a reliability warning.

## System workflow

| Stage | Processing | Output and gate |
|---|---|---|
| 0. Setup | Select workspace; configure OpenAI API or Codex fallback; connect Higgsfield OAuth; run provider diagnostics. | Provider capability report. |
| 1. Character source | Accept text description or PNG/JPEG/WebP upload. Uploads can be used directly or restaged by GPT Image into a centered down-facing full-body anchor. | Immutable source asset. |
| 2. Base generation | Render upstream prompt `01-south-anchor.md`; generate 1024×1024 high-quality PNG on safe chroma. | Raw base candidate. |
| 3. Base snap | Apply the logic represented by `02-pixel-snap.md` through `sprite-gen`: chroma peel, grid/pitch detection, dominant-block vote, palette, outline, binary alpha, integer placement. | Raw, plain-alpha, native pixel, 8× preview, and chroma variants. **Gate 1: approve base identity.** |
| 4. Directions | Render `03-directional-anchors.md` with the approved snapped anchor and 1024 alternating-pixel guide. Generate up/right and optionally independent left. | Four-direction turntable. **Gate 2: approve directions.** |
| 5. Poseboards | Render `04-action-spritesheet.md`; generate one 2048×1536 medium-quality poseboard per state and generated direction. | Immutable raw poseboards. |
| 6. Walk I2V | Render `06-walk-cycle-i2v.md`; request a fixed-camera, walking-in-place, chroma-background five-second clip from Higgsfield, preferring Seedance 2.0. | MP4 plus extracted candidate frames. |
| 7. Recovery | Apply `05-frame-recovery.md`: chroma alpha and connected components first; projection splitting for fused poses; never automatic grid-cell cropping. | Native recovered candidates. **Gate 3: choose/reorder frames.** |
| 8. Per-frame snap | Apply `07-per-frame-snap.md` from original recovered components, never from a previously resized output. | Canonical true-pixel frames and plain twins. |
| 9. Curation | Reuse the `sprite-gen` curator for reorder, clone, transform, alignment, pixel tools, undo, GIF preview, and candidate replacement. | `curation.json`; walk loop and seam preview. **Gate 4: approve motion.** |
| 10. Normalize | Apply `08-runtime-normalize-and-align.md`: despill, integer placement, shared anchor, native pixel validation, 256×256 cells. | Final per-frame PNGs. |
| 11. Atlas and QA | Compose sheets, paged atlas, GIFs, manifest, provenance, checksums, and structural/motion reports. | **Gate 5: approve export.** |
| 12. Export | Build an atomic, engine-neutral ZIP. | Downloadable sprite pack; previous exports remain intact. |

Changing an approved artifact invalidates only its dependants. Invalidated outputs remain available but are marked stale until regenerated or explicitly re-approved.

## Prompt and upstream integration

Pin these revisions:

- `chongdashu/ai-pixel-snapped-game-sprites@5f4c8b99a1de38a84af7dcc36af80622e5057cd6`
- `aldegad/sprite-gen@606db6d89beedace80863374859752739369ba19`

Use `sprite-gen` as a Git dependency pinned to that commit. Vendor the following application assets with hashes and license metadata:

- All eight original prompt files.
- All reference images and GIFs from the first repository.
- `sprite-gen/SKILL.md`, referenced workflow documentation, and original curator static files.
- MIT, Apache-2.0, and upstream NOTICE files.

`upstream-lock.json` records repository, revision, original path, destination path, SHA-256, and SPDX identifier. `scripts/sync_upstreams.py --check` verifies the snapshot without networking; `--update <checkout>` is the only updating mode.

Derived templates live separately and may only:

- Replace direction/state/frame/chroma variables.
- Insert the chosen palette, cell, and motion descriptions.
- Correct upstream cross-reference mistakes without altering the originals.
- Add a machine-readable provenance header.

All eight prompts map to exactly one pipeline stage, even when the underlying `sprite-gen` operation produces multiple artifact variants in one extraction pass.

## Filesystem model

Default workspace: the platform-specific user data directory for `ai-sprite-studio`, overridable with `--workspace`.

```text
projects/<project-uuid>/
  project.json
  inputs/<artifact-id>.<ext>
  candidates/<stage>/<candidate-id>/
    meta.json
    raw.png
    plain.png
    pixel.png
    preview.png
  run/
    sprite-request.json
    raw/
    frames/
    curation.json
  videos/<direction>/<candidate-id>/
    source.mp4
    candidates/
    selection.json
  prompts/<job-id>.json
  jobs/<job-id>.json
  events.ndjson
  exports/<revision>/
```

Every stored path is relative to the project root. Files are written to a sibling temporary file, flushed, and atomically replaced. Artifact IDs and SHA-256 hashes—not user-provided paths—cross the HTTP API.

## Public interfaces and types

Create Pydantic contracts in `src/ai_sprite_studio/contracts.py`:

```python
Direction = Literal["down", "right", "up", "left"]
SidePolicy = Literal["mirror", "independent"]
GeneratorKind = Literal["gpt_poseboard", "higgsfield_i2v", "upload"]

class StateSpec(BaseModel):
    id: str
    label: str
    frames: int
    fps: int
    loop: bool
    generator: GeneratorKind
    motion: str

class CellSpec(BaseModel):
    width: Literal[256] = 256
    height: Literal[256] = 256
    logical_size: Literal[64, 128, 256] = 128
    anchor_x: Literal[128] = 128
    anchor_y: Literal[255] = 255

class ArtifactRef(BaseModel):
    id: UUID
    kind: str
    relative_path: str
    sha256: str
    media_type: str
    width: int | None
    height: int | None
    variant: Literal["raw", "plain", "pixel", "chroma", "preview", "video"]
    source_job_id: UUID | None
    dependencies: list[UUID]

class Approval(BaseModel):
    gate: Literal["base", "directions", "frames", "motion", "export"]
    artifact_ids: list[UUID]
    artifact_hashes: list[str]
    created_at: datetime
    note: str = ""

class JobRecord(BaseModel):
    id: UUID
    project_id: UUID
    command: JobCommand
    status: Literal[
        "queued", "running", "waiting_approval", "succeeded",
        "failed", "cancel_requested", "canceled", "attention_required"
    ]
    payload: dict
    input_hash: str
    provider_request_ids: list[str]
    output_artifact_ids: list[UUID]
    progress: float
    error: ApiError | None
```

`JobCommand` values:

```text
normalize_input
generate_base
edit_candidate
snap_candidate
generate_directions
generate_poseboards
generate_walk
ingest_walk_video
recover_frames
save_curation
ai_edit_frame
run_qa
export_pack
```

Provider boundaries:

```python
class ImageProvider(Protocol):
    async def generate(self, request: ImageGenerateRequest) -> ProviderImageResult: ...
    async def edit(self, request: ImageEditRequest) -> ProviderImageResult: ...

class MotionProvider(Protocol):
    async def capabilities(self) -> list[MotionCapability]: ...
    async def generate(self, request: MotionGenerateRequest) -> ProviderVideoResult: ...

class SpriteEngine(Protocol):
    def prepare(self, project: ProjectConfig) -> Path: ...
    def extract(self, project: ProjectConfig, states: list[str]) -> ExtractionResult: ...
    def compose(self, project: ProjectConfig) -> CompositionResult: ...
    def inspect(self, project: ProjectConfig) -> QaReport: ...
```

HTTP API under `/api/v1`:

| Method | Route | Purpose |
|---|---|---|
| `GET/POST` | `/projects` | List or create projects. |
| `GET/PATCH` | `/projects/{id}` | Read or update validated project settings. |
| `POST` | `/projects/{id}/assets` | Multipart image/video upload. |
| `GET` | `/projects/{id}/artifacts/{artifact_id}` | Stream a contained artifact. |
| `POST` | `/projects/{id}/jobs` | Preflight and enqueue a typed command. |
| `GET` | `/jobs/{id}` | Read persistent status. |
| `GET` | `/jobs/{id}/events` | Server-Sent Event progress stream. |
| `POST` | `/jobs/{id}/cancel` | Request cancellation. |
| `POST` | `/projects/{id}/approvals` | Record a quality-gate approval. |
| `GET/PUT` | `/projects/{id}/curation` | Read or atomically save curator state. |
| `POST` | `/projects/{id}/chat` | Stream contextual chat and proposed commands. |
| `GET/POST` | `/oauth/higgsfield/*` | Start and complete PKCE OAuth. |
| `GET` | `/projects/{id}/exports` | List immutable export revisions. |

All errors use:

```json
{
  "code": "STABLE_MACHINE_CODE",
  "message": "User-facing explanation",
  "retryable": false,
  "details": {}
}
```

Chat may propose typed commands but cannot approve gates, replace canonical artifacts, invoke provider spending, or export without a subsequent explicit UI confirmation.

## Export contract

Produce:

```text
character-name/
  frames/<direction>/<state>/<index>.png
  sheets/<direction>/<state>.png
  atlases/atlas-000.png
  previews/<direction>/<state>.gif
  manifest.json
  provenance.json
  qa-report.json
  checksums.sha256
  THIRD_PARTY_NOTICES.md
```

Sheets use up to five columns, preserving the board’s 5×2 layout for ten-frame states. Atlases use deterministic row-major packing, a maximum page size of 4096×4096, direction order `down,right,up,left`, configured state order, then frame index.

`manifest.json` schema version 1 contains:

- Character/project identifiers.
- 256×256 cell and `(128,255)` anchor.
- Atlas page path, dimensions, and checksum.
- Animation direction, state, FPS, loop flag, and generator.
- Every frame’s atlas page, absolute `{x,y,w,h}` rectangle, duration in milliseconds, anchor, standalone PNG path, source artifact, and curation revision.
- Model, prompt hash, upstream revision, chroma, palette, and pixel-engine settings.

Runtime consumers must not infer frames from grid position.

## Higgsfield MCP contract

- Connect to `https://mcp.higgsfield.ai/mcp` using OAuth 2.1 PKCE and dynamic client registration through the official MCP SDK.
- Store refresh tokens atomically in the platform config directory with owner-only permissions where supported; rely on the user-profile ACL on Windows.
- Discover tools after login and store only redacted schemas and schema hashes.
- Permit only selected media upload, image-to-video generation, status, and result tools. Reject account, profile, sync, delete, publish, sharing, or social tools.
- Prefer a tool/model advertising Seedance 2.0. If unavailable, present the discovered I2V options for the user to select.
- Use `gpt-5.6-terra` only to map the fixed motion request into the selected tool’s current JSON schema. It cannot execute the tool.
- Show the selected tool and sanitized arguments for approval, validate them against the discovered schema, then invoke that exact tool.
- Accept immediate video content, HTTPS result URLs, or job-ID/status-tool flows. Poll resumably every ten seconds for at most twenty minutes.
- Download results immediately with a 500 MB cap, HTTPS enforcement, public-IP validation, checksum, and content-type verification.
- If MCP asset transport is unsupported or Higgsfield is unavailable, offer manual MP4 upload and the explicitly experimental GPT poseboard walk path; never switch providers automatically.

The walk processor extracts a 12 FPS candidate pool, discards the first and last 10%, and suggests eight frames by finding a 0.5–1.5 second start/end pair with low seam distance and non-trivial intermediate motion. The curator remains authoritative.

## File map

```text
pyproject.toml                         Package, dependencies, console command
uv.lock                               Resolved dependency lock
upstream-lock.json                    Upstream revisions and asset hashes
THIRD_PARTY_NOTICES.md                License and provenance notices
scripts/sync_upstreams.py             Check/update vendored source assets

src/ai_sprite_studio/cli.py           serve, doctor, export, verify commands
src/ai_sprite_studio/app.py           Starlette assembly, middleware, lifecycle
src/ai_sprite_studio/api.py           Typed HTTP/SSE/OAuth routes
src/ai_sprite_studio/contracts.py     Pydantic public contracts and enums
src/ai_sprite_studio/project_store.py Atomic filesystem persistence and invalidation
src/ai_sprite_studio/jobs.py          Persistent serialized queue and recovery
src/ai_sprite_studio/prompts.py       Eight-stage prompt registry and rendering
src/ai_sprite_studio/pipeline.py      Stage DAG, handlers, quality gates
src/ai_sprite_studio/sprite_engine.py Pinned sprite-gen adapter
src/ai_sprite_studio/openai_images.py GPT Image generation/edit provider
src/ai_sprite_studio/codex_images.py  Optional upstream Codex OAuth fallback
src/ai_sprite_studio/higgsfield.py    OAuth, MCP discovery, binding, calls
src/ai_sprite_studio/video.py         Safe decode, extraction, loop suggestion
src/ai_sprite_studio/chat.py          Responses API command orchestration
src/ai_sprite_studio/qa.py            Structural, identity, and motion checks
src/ai_sprite_studio/exporter.py      Frames, sheets, atlases, manifest, ZIP

src/ai_sprite_studio/static/index.html
src/ai_sprite_studio/static/app.css
src/ai_sprite_studio/static/app.js
src/ai_sprite_studio/static/editor.js
src/ai_sprite_studio/assets/upstream/
src/ai_sprite_studio/assets/templates/

tests/fixtures/
tests/test_contracts.py
tests/test_project_store.py
tests/test_jobs.py
tests/test_prompts.py
tests/test_sprite_engine.py
tests/test_openai_images.py
tests/test_directions_actions.py
tests/test_curation.py
tests/test_higgsfield.py
tests/test_video.py
tests/test_chat.py
tests/test_qa_export.py
tests/test_security.py
tests/e2e/test_studio.py
```

## Implementation tasks

### Task 1: Bootstrap and lock upstream sources

**Files:** `pyproject.toml`, `uv.lock`, `upstream-lock.json`, vendored assets, notices, sync script, CI.

**Interfaces:**
- Produces the importable package skeleton, pinned `sprite-gen` dependency, immutable upstream asset snapshot, and verification command consumed by all later tasks.

- [ ] Add failing tests asserting exactly eight original prompts, expected revisions, SHA-256 matches, license files, and importability of pinned `sprite-gen`.
- [ ] Configure the Git dependency at the exact `sprite-gen` revision and add runtime/dev dependencies.
- [ ] Vendor and hash the prompt, reference, skill, documentation, and curator assets.
- [ ] Add `sync_upstreams.py --check` and run `uv run pytest tests/test_upstream_assets.py -v`.
- [ ] Commit as `build: bootstrap studio and pin upstream assets`.

### Task 2: Contracts and atomic project storage

**Files:** `contracts.py`, `project_store.py`, `test_contracts.py`, `test_project_store.py`.

**Interfaces:**
- Consumes the package skeleton from Task 1.
- Produces the Pydantic contracts and `ProjectStore.create`, `load`, `save`, `put_artifact`, `get_artifact`, `approve`, and `invalidate_dependants` interfaces.

- [ ] Test all locked defaults, custom-state boundaries, UUID paths, atomic replacement, immutable candidate creation, approval hashes, and dependency invalidation.
- [ ] Implement the Pydantic contracts and exact workspace layout.
- [ ] Implement the project-store operations using contained relative paths and atomic replacement.
- [ ] Reject absolute paths, `..`, symlinks escaping the project, malformed JSON, and unknown schema versions.
- [ ] Run the two test modules and commit as `feat: add project contracts and atomic storage`.

### Task 3: Local server and persistent job runner

**Files:** `cli.py`, `app.py`, `api.py`, `jobs.py`, initial static shell, API/job tests.

**Interfaces:**
- Consumes `ProjectStore` and the contracts from Task 2.
- Produces `/api/v1`, `ai-sprite-studio serve`, SSE job events, and the persistent one-at-a-time job executor.

- [ ] Test OS-assigned port startup, loopback-only binding, SSE replay, single concurrency, cancellation, restart recovery, and idempotent completed-job reuse.
- [ ] Implement `ai-sprite-studio serve --workspace PATH --port 0`; print and open the actual listening URL.
- [ ] Add loopback Host/Origin checks, SameSite session cookie, CSRF token, CSP, MIME protections, and no CORS.
- [ ] Persist each job before execution. A crash during an unresolved provider call becomes `attention_required`, never an automatic potentially billable retry.
- [ ] Run tests and commit as `feat: add local app and persistent jobs`.

### Task 4: Prompt registry and pipeline DAG

**Files:** `prompts.py`, `pipeline.py`, templates, prompt/pipeline tests.

**Interfaces:**
- Consumes the project/job contracts and vendored prompt assets.
- Produces rendered prompts with provenance, deterministic guide images, dependency invalidation, and five approval gates.

- [ ] Snapshot-test all eight rendered stages with direction, state, count, chroma, palette, and motion substitutions.
- [ ] Generate deterministic 1024×1024 and 2048×1536 alternating-pixel guides with Pillow.
- [ ] Implement artifact-hash dependency edges and the five approval gates.
- [ ] Add preflight request counts for selected states/directions and forbid hidden provider fallback.
- [ ] Run tests and commit as `feat: add versioned prompt pipeline`.

### Task 5: Canonical true-pixel engine and upload-only path

**Files:** `sprite_engine.py`, curator API integration, engine and curation tests.

**Interfaces:**
- Consumes the stage registry and project run directory.
- Produces `SpriteEngine.prepare`, `extract`, `compose`, and `inspect`, plus raw/plain/pixel artifact variants.

- [ ] Build synthetic chroma fixtures containing separated and fused poses, detached accessories, palette outliers, fringe, and drift.
- [ ] Wrap `sprite_gen.prepare.run`, `extract.run`, curation functions, inspection, previews, and composition without forking them.
- [ ] Preserve raw, plain-alpha, and pixel-perfect twins; expose component and projection recovery but disable naive slot fallback.
- [ ] Enforce 128 logical pixels, 2× integer export, binary alpha, 12-color shared palette, integer anchors, and nearest-neighbour previews.
- [ ] Demonstrate an entirely local upload → snap → curate → atlas flow, then commit as `feat: add canonical sprite engine`.

### Task 6: OpenAI image generation and editing

**Files:** `openai_images.py`, `codex_images.py`, provider tests.

**Interfaces:**
- Consumes rendered prompts and approved artifacts.
- Produces `ImageProvider.generate`, `edit`, immutable provider results, request provenance, and the optional Codex adapter.

- [ ] Mock and assert exact Image API request shapes for generation, reference editing, and mask editing.
- [ ] Implement 1024×1024 high-quality base/direction generation and 2048×1536 medium-quality poseboards using the pinned model snapshot.
- [ ] Record request IDs, returned model, dimensions, quality, rendered prompt, prompt hash, elapsed time, and usage when returned.
- [ ] Retry only 429 and transient 5xx responses, respecting `Retry-After`, with at most three attempts; do not retry moderation, validation, or access failures.
- [ ] Add the optional Codex CLI provider by wrapping the pinned upstream provider and checking CLI authentication in `doctor`.
- [ ] Commit as `feat: add GPT Image generation and editing`.

### Task 7: Direction and action generation

**Files:** pipeline handlers, direction/action tests, UI stage views.

**Interfaces:**
- Consumes approved anchors, `ImageProvider`, and the prompt registry.
- Produces directional candidate sets, mirrored derivatives, poseboards, and row-specific regeneration.

- [ ] Test mirrored and independent left policies, generated-direction counts, core/custom state validation, and downstream invalidation.
- [ ] Generate and approve direction anchors before any action job can start.
- [ ] Generate one poseboard per state/generated direction; derive the complete left animation by flipping right only for mirrored projects.
- [ ] Regenerate individual failed state/direction rows without touching accepted siblings.
- [ ] Add turntable and poseboard review views, run tests, and commit as `feat: add directions and action poseboards`.

### Task 8: Guided curator and AI frame edits

**Files:** `editor.js`, curator routes, curation tests and browser tests.

**Interfaces:**
- Consumes sprite-engine frame variants and provider edits.
- Produces `curation.json`, non-destructive frame operations, previews, and AI-edited replacement candidates.

- [ ] Adapt the upstream curator with attribution rather than recreating its selection and transform engine.
- [ ] Support selection, candidate pools, reorder, clone, move, integer scale, rotate/shear preview, flip, pixel pen, eraser, eyedropper, undo, and GIF preview.
- [ ] Store all edits non-destructively in `curation.json`; composition is the only baking step.
- [ ] Implement AI frame editing by nearest-neighbour upscaling the selected frame onto chroma, applying a GPT Image edit/mask, then re-extracting and snapping it into the same slot as a new candidate.
- [ ] Confirm keyboard navigation, zoom without smoothing, and stale-revision conflict handling; commit as `feat: add guided frame curator`.

### Task 9: Higgsfield walk pipeline

**Files:** `higgsfield.py`, `video.py`, MCP/video tests.

**Interfaces:**
- Consumes approved directional anchors, project job persistence, and the curator.
- Produces OAuth state, discovered motion capabilities, `MotionProvider.generate`, persisted video, and an eight-frame walk candidate pool.

- [ ] Implement and test PKCE state/nonce verification, token refresh, schema discovery, safe-tool filtering, semantic binding, and resumable result polling against a mocked MCP server.
- [ ] Add connection UI showing discovered tool names, descriptions, model enums, schema hashes, and the selected I2V/status tools.
- [ ] Generate a five-second walking-in-place clip per generated direction and immediately persist the returned MP4.
- [ ] Decode without shell interpolation, extract the 12 FPS candidate pool, propose an eight-frame cycle, and pass frames through the same recovery/snap engine.
- [ ] Add manual MP4 and GPT poseboard fallbacks.
- [ ] Capture a redacted real Higgsfield tool-schema fixture after user OAuth and complete one live Seedance 2.0 acceptance run.
- [ ] Commit as `feat: add Higgsfield walk generation`.

### Task 10: Contextual chat orchestration

**Files:** `chat.py`, chat routes/UI, chat tests.

**Interfaces:**
- Consumes project summaries, selected previews, and validated job commands.
- Produces streamed assistant text and typed command proposals; it never directly approves or spends.

- [ ] Define structured commands for updating the brief, proposing prompts, scheduling generation, regenerating a target, explaining QA, and proposing export.
- [ ] Send only the project summary and selected 512px preview unless the user explicitly attaches more.
- [ ] Validate every returned command through Pydantic; reject filesystem paths, shell commands, unknown IDs, gate approvals, and unconfirmed spending.
- [ ] Stream prose and command proposals over SSE; show a separate confirmation card before enqueueing.
- [ ] Test prompt injection attempts and commit as `feat: add contextual studio chat`.

### Task 11: QA and engine-neutral export

**Files:** `qa.py`, `exporter.py`, QA/export tests.

**Interfaces:**
- Consumes curated canonical frames and project provenance.
- Produces blocking/warning QA reports, deterministic sheets/atlases, `manifest.json`, checksums, and atomic ZIP exports.

- [ ] Add blocking checks for missing frames, invalid dimensions, non-binary alpha, non-integer pixel blocks, visible chroma, anchor mismatch, empty frames, edge clipping, atlas bounds, and checksum mismatch.
- [ ] Add non-blocking identity warnings using aligned silhouette IoU and palette-histogram distance.
- [ ] Add motion warnings when normalized first/last seam distance exceeds `0.15` or intermediate motion is below `0.03`.
- [ ] Generate standalone PNGs, five-column sheets, paged atlases, GIFs, schema-versioned manifest, provenance, QA report, notices, and checksums.
- [ ] Build exports in a temporary revision directory and atomically publish the completed directory and ZIP.
- [ ] Run deterministic golden tests and commit as `feat: add QA and sprite pack export`.

### Task 12: Packaging, documentation, and full acceptance

**Files:** README, user/architecture docs, `doctor`, CI, Playwright flow.

**Interfaces:**
- Consumes the complete application and verification surfaces.
- Produces installable wheels, cross-platform CI, operator documentation, and provider-free/full acceptance evidence.

- [ ] Document installation, OpenAI organization verification, Codex fallback, Higgsfield OAuth, provider costs, project recovery, exports, and privacy boundaries.
- [ ] Implement `doctor`, `verify PROJECT_ID`, and `export PROJECT_ID`.
- [ ] Add a provider-free Playwright flow: create project → upload fixture → accept gates → curate → export.
- [ ] Run unit tests and wheel-install smoke tests on Linux, macOS, and Windows; run Playwright on Linux.
- [ ] Run opt-in live OpenAI and Higgsfield tests only with explicit environment flags.
- [ ] Verify `uv run pytest -q`, `uv run ruff check .`, `uv build`, and a clean wheel installation.
- [ ] Commit as `release: complete AI Sprite Studio v1`.

## Test and acceptance matrix

The release is complete only when all scenarios pass:

1. Text-to-character flow produces an approved base, four directions, six core animations, previews, and a valid export.
2. Uploaded artwork can be normalized locally without calling OpenAI.
3. An unsafe green character causes safe chroma selection and leaves no chroma residue.
4. Symmetric mode mirrors the right anchor and every right-side animation consistently.
5. Asymmetric mode generates and preserves separate left/right details.
6. Missing, extra, fused, and detached pose components can be recovered or selectively regenerated.
7. Editing an accepted anchor marks only dependent directions and animations stale.
8. Pixel edits survive refresh and re-composition without altering source frames.
9. AI whole-image, masked, state, and individual-frame edits create new candidate lineage.
10. Walk video interruption resumes without losing the downloaded clip or selected frames.
11. Higgsfield failure cleanly routes to manual upload or user-selected GPT fallback.
12. A restart restores queued/completed jobs and marks ambiguous billable calls for attention.
13. Export refuses structural failures but permits acknowledged identity/motion warnings.
14. Every manifest rectangle is inside its atlas and resolves to the same pixels as its standalone PNG.
15. Final pixel frames contain only binary alpha and uniform 2×2 logical pixel blocks.
16. A malicious filename, traversal path, SVG, decompression bomb, private-network result URL, or foreign Origin is rejected.
17. No provider key, OAuth token, absolute local path, or session token appears in project files or exports.
18. A clean installation opens the actual OS-assigned loopback URL and completes the provider-free demo.

## Explicit assumptions and exclusions

- The user will provide an OpenAI API key with `gpt-image-2` access and complete organization verification if required.
- Final live Higgsfield acceptance requires the user to complete its browser OAuth flow.
- Users are responsible for rights to uploaded or generated characters.
- “Full sprite character” means a top-down four-way character with core and custom states, not eight-way, isometric, skeletal, or 3D animation.
- Idle and walk are loops; attack, hurt, jump, and death are one-shots unless changed in a custom state.
- The app does not train or fine-tune models.
- No cloud deployment, multi-user authentication, collaborative editing, automatic publishing, engine-specific Unity/Godot exporters, or alternate pixel snap engine is included in v1.
- The manifest and generic assets are the stable integration boundary for future engine-specific exporters.
