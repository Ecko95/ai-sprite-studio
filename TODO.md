# TODO

## Current status (2026-07-18)
Higgsfield-guided workflow + Curator Studio UI rework implemented, uncommitted on `main` working tree. 237 tests passed, 1 skipped (playwright browser not installed), ruff clean.

## Completed
- Backend: `POST /curator/video-upload` (file / url / saved `video_id`, `save_only`, chroma-keyed frame sampling into the engine), video library (`GET /curator/videos`, `GET|DELETE /curator/video/{id}`), `POST /curator/reference` (OpenAI gpt-image-1, degrades honestly without `OPENAI_API_KEY`).
- Frame ceiling raised 12 → 128 end to end (contracts, engine, endpoint) + 90-frame round-trip test.
- Landing redesign: Higgsfield-guided wizard (reference prompt → CDance mini 2.0 animation prompt → video fetch/extract) + restyled manual mode + video library.
- Curator Studio at `/curator/studio`: frame timeline (per-frame hide/delete/reorder), onion skin with per-layer visibility/opacity, real-export-speed GIF playback, nudge/auto-normalize, atlas render panel, exports, autosaving curation, progress UX.
- Dark/light theme (shared tokens, `data-theme` + `prefers-color-scheme`, localStorage toggle).

## Pending
- Commit/branch + PR (user decides branch name).
- `uv run playwright install` to un-skip the browser smoke test.
- README.md: document the guided workflow, `/curator/studio`, and `OPENAI_API_KEY`.

## Notes for future sessions
- Higgsfield MCP runs Claude-side; the web app hands off the animation prompt and takes the video back (URL/file). Classic vendored curator still at `/curator`.
- All curator ingest paths lock the chroma key to the corner pixel — don't rely on the vendored auto key picker.
