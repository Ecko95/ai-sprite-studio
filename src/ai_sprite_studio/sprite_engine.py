"""Local adapter around the pinned ``sprite-gen`` component-row engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from functools import wraps
from io import BytesIO, StringIO
import json
from math import isfinite
import os
from pathlib import Path, PurePath
import re
import tempfile
from threading import RLock
from typing import Any
from uuid import UUID
import warnings

from PIL import Image, UnidentifiedImageError
from sprite_gen import compose_atlas, extract as sprite_extract, inspect as sprite_inspect
from sprite_gen import prepare as sprite_prepare
from sprite_gen import preview as sprite_preview
from sprite_gen.curation import frame_variant, load_curation, run_revision, stamp_curation, state_plan
from sprite_gen.layout import raw_rel
from sprite_gen.runio import atomic_write_text, read_guard, release_run_dir_lock

from .contracts import ArtifactRef
from .project_store import ProjectStore, ProjectStoreError

try:
    import fcntl
except ImportError:  # pragma: no cover - descriptor-backed storage requires Unix
    fcntl = None


MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_INPUT_PIXELS = 16_000_000
# Connected components retain per-region bookkeeping; projection remains explicitly available above this cap.
MAX_COMPONENT_INPUT_PIXELS = 262_144
_ENGINE_STATE = "sprite-engine.json"
_UPLOAD_STATE = "upload"
_ARTIFACT_STAGE = "sprite_upload"
_MEDIA_FORMATS = {
    "PNG": ("image/png", {".png"}),
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
    "WEBP": ("image/webp", {".webp"}),
}
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w.-])(?:/(?:[^\s\"']+)|[A-Za-z]:[\\/](?:[^\s\"']+)|"
    r"\\\\[^\s\\/\"']+[\\/][^\s\"']+)"
)
_PIXEL_EDIT_COORDINATE = re.compile(r"(0|[1-9]\d*),(0|[1-9]\d*)\Z")
_CURATION_TRANSFORM_FIELDS = {"rotate", "scale", "dx", "dy", "shx", "shy", "flipX"}
_CHROMA_DEFAULTS = {"mode": "rgb", "unmix_reach": 4, "spill_max_fraction": 0.005}
_ENGINE_MUTATION_LOCK = RLock()


@contextmanager
def _engine_mutation_guard(run_dir: Path):
    """Serialize this engine's run-state changes across app processes."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if fcntl is None or no_follow is None:
        raise RuntimeError("sprite engine mutation lock is unavailable")
    path = run_dir.parent / f".{run_dir.name}.sprite-engine.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | no_follow, 0o600)
    except OSError as exc:
        raise RuntimeError("sprite engine mutation lock is unavailable") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _serialized_engine_mutation(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def locked(self: SpriteEngine, project_id: UUID | str, *args: Any, **kwargs: Any) -> Any:
        try:
            with _ENGINE_MUTATION_LOCK:
                run_dir = self.store.run_dir(project_id)
                with _engine_mutation_guard(run_dir):
                    return method(self, project_id, *args, **kwargs)
        except SpriteEngineError:
            raise
        except (OSError, RuntimeError, SystemExit) as exc:
            raise SpriteEngineError("sprite engine operation failed") from exc

    return locked


class SpriteEngineError(ProjectStoreError):
    """A redacted, app-specific local sprite engine failure."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@contextmanager
def _curation_boundary(operation: str):
    try:
        yield
    except SpriteEngineError:
        raise
    except (
        SystemExit,
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OverflowError,
    ) as exc:
        raise SpriteEngineError(f"sprite {operation} failed") from exc


@dataclass(frozen=True)
class PreparedSpriteRun:
    input_artifact_id: UUID
    request_artifact_id: UUID
    selected_chroma: str
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionResult:
    raw_artifact_id: UUID
    plain_artifact_ids: tuple[UUID, ...]
    pixel_artifact_ids: tuple[UUID, ...]
    preview_artifact_ids: tuple[UUID, ...]
    report_artifact_id: UUID
    selected_chroma: str
    outputs: tuple[str, ...]
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class CurationSnapshot:
    run_revision: str
    payload: dict[str, Any] | None
    selected: dict[str, tuple[int, ...]]
    variants: dict[str, str]
    artifact_id: UUID | None = None


@dataclass(frozen=True)
class SpriteRunView:
    """A read-only snapshot of an extracted run for the local curator UI."""

    run_revision: str
    cell: dict[str, int]
    state: str
    request_frames: int
    fps: int
    loop: bool
    pixel_artifact_ids: tuple[UUID, ...]
    plain_artifact_ids: tuple[UUID, ...]
    curation: dict[str, Any] | None
    has_atlas: bool
    atlas_artifact_id: UUID | None
    manifest_artifact_id: UUID | None
    character_id: str


@dataclass(frozen=True)
class ComposeResult:
    atlas_artifact_id: UUID
    manifest_artifact_id: UUID
    report_artifact_id: UUID
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class InspectionResult:
    report_artifact_id: UUID
    outputs: tuple[str, ...]
    summary: dict[str, Any]


class SpriteEngine:
    """The narrow local upload, curation, and atlas boundary for one project."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def ingest_upload(
        self,
        project_id: UUID | str,
        data: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> ArtifactRef:
        """Verify and persist one immutable local PNG, JPEG, or WebP source."""

        width, height, normalized_media_type = self._validated_upload(
            data, media_type=media_type, filename=filename
        )
        try:
            return self.store.put_artifact(
                project_id,
                data,
                kind="input",
                media_type=normalized_media_type,
                variant="raw",
                width=width,
                height=height,
            )
        except ProjectStoreError as exc:
            raise SpriteEngineError("upload could not be stored") from exc

    @_serialized_engine_mutation
    def prepare(
        self,
        project_id: UUID | str,
        input_artifact_id: UUID | str,
        *,
        frames: int = 1,
        chroma_key: str = "auto",
    ) -> PreparedSpriteRun:
        """Create a numeric, upload-only component-row run without overwriting it."""

        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 12:
            raise SpriteEngineError("upload frame count must be between 1 and 12")
        project = self.store.load(project_id)
        source = self._input_artifact(project.id, input_artifact_id)
        run_dir = self.store.run_dir(project.id)
        if any(run_dir.iterdir()):
            raise SpriteEngineError("sprite run is already prepared")

        source_data = self.store.read_artifact_bytes(project.id, source.id)
        suffix = self._source_suffix(source.media_type)
        self._validated_upload(
            source_data,
            media_type=source.media_type,
            filename=f"source{suffix}",
        )
        request = self._upload_request(frames)
        with tempfile.TemporaryDirectory(prefix="sprite-engine-", dir=run_dir.parent) as staging:
            source_path = Path(staging) / f"source{suffix}"
            source_path.write_bytes(source_data)
            result = self._invoke(
                "prepare",
                sprite_prepare.run,
                out_dir=run_dir,
                character_id=f"upload-{project.id.hex}",
                base_image=source_path,
                request_json=json.dumps(request),
                chroma_key=chroma_key,
            )
        self._require_success("prepare", result)

        prepared_request = self._request(run_dir)
        prepared_request["chroma"] = dict(_CHROMA_DEFAULTS)
        atomic_write_text(
            run_dir / "sprite-request.json",
            json.dumps(prepared_request, ensure_ascii=False, indent=2) + "\n",
        )
        self._bind_raw_source(run_dir, prepared_request, source_data, replace=False)

        request_artifact = self._put_artifact(
            project.id,
            (run_dir / "sprite-request.json").read_bytes(),
            kind="sprite_request",
            media_type="application/json",
            variant="raw",
            dependencies=(source.id,),
        )
        self._write_state(
            run_dir,
            {"input_artifact_id": str(source.id), "request_artifact_id": str(request_artifact.id)},
        )
        chroma = str(prepared_request["chroma_key"]["hex"])
        return PreparedSpriteRun(
            input_artifact_id=source.id,
            request_artifact_id=request_artifact.id,
            selected_chroma=chroma,
            outputs=(raw_rel(prepared_request, _UPLOAD_STATE), "sprite-request.json"),
        )

    @_serialized_engine_mutation
    def extract(
        self, project_id: UUID | str, *, segmentation: str = "components"
    ) -> ExtractionResult:
        """Recover one upload row with components or explicitly requested projection."""

        if segmentation not in {"components", "projection"}:
            raise SpriteEngineError("unsupported sprite segmentation")
        project = self.store.load(project_id)
        run_dir, state = self._prepared_state(project.id)
        source_id = self._state_uuid(state, "input_artifact_id")
        request_id = self._state_uuid(state, "request_artifact_id")
        source = self._input_artifact(project.id, source_id)
        try:
            source_data = self.store.read_artifact_bytes(project.id, source.id)
        except ProjectStoreError as exc:
            raise SpriteEngineError("invalid upload artifact") from exc
        width, height, _ = self._validated_upload(
            source_data,
            media_type=source.media_type,
            filename=f"source{self._source_suffix(source.media_type)}",
        )
        if segmentation == "components" and width * height > MAX_COMPONENT_INPUT_PIXELS:
            raise SpriteEngineError(
                "sprite component input is too complex; choose projection explicitly"
            )
        self._write_state(
            run_dir,
            {"input_artifact_id": str(source_id), "request_artifact_id": str(request_id)},
        )
        try:
            (run_dir / "curation.json").unlink(missing_ok=True)
        except OSError as exc:
            raise SpriteEngineError("sprite curation state could not be reset") from exc
        # Re-bind the verified immutable source so extraction never trusts a
        # raw file that was mutated on disk after prepare wrote it.
        self._bind_raw_source(run_dir, self._request(run_dir), source_data, replace=True)
        result = self._invoke_writer(
            "extract",
            run_dir,
            sprite_extract.run,
            run_dir=run_dir,
            states=_UPLOAD_STATE,
            segmentation=segmentation,
            allow_slot_fallback=False,
        )
        if result != 0:
            raise SpriteEngineError(
                "sprite extraction failed",
                diagnostics=self._failure_diagnostics(run_dir),
            )

        self._current_request_dependency(project.id, run_dir, state)
        request = self._request(run_dir)
        manifest_path = self._run_file(run_dir, "frames/frames-manifest.json")
        manifest = self._json_file(manifest_path, "sprite extraction report")
        row = next((item for item in manifest.get("rows", []) if item.get("state") == _UPLOAD_STATE), None)
        if not isinstance(row, dict):
            raise SpriteEngineError("sprite extraction did not produce the upload row")
        files = self._relative_files(row.get("files"), "pixel frames")
        plain_files = self._relative_files(row.get("plain_files"), "plain frames")
        if not files or len(files) != len(plain_files):
            raise SpriteEngineError("sprite extraction did not preserve frame twins")

        validated_frames: list[tuple[Path, Path]] = []
        shared_palette: set[tuple[int, int, int]] = set()
        for plain_file, pixel_file in zip(plain_files, files, strict=True):
            plain_path = self._run_file(run_dir, plain_file)
            pixel_path = self._run_file(run_dir, pixel_file)
            self._validate_plain_twin(plain_path)
            shared_palette.update(self._validate_canonical_frame(pixel_path))
            validated_frames.append((plain_path, pixel_path))
        if len(shared_palette) > 12:
            raise SpriteEngineError("canonical sprite frames exceed the shared palette")

        plain_ids: list[UUID] = []
        pixel_ids: list[UUID] = []
        for plain_path, pixel_path in validated_frames:
            plain_ids.append(
                self._put_artifact(
                    project.id,
                    plain_path.read_bytes(),
                    kind="sprite_frame",
                    media_type="image/png",
                    variant="plain",
                    dependencies=(source_id, request_id),
                    width=256,
                    height=256,
                ).id
            )
            pixel_ids.append(
                self._put_artifact(
                    project.id,
                    pixel_path.read_bytes(),
                    kind="sprite_frame",
                    media_type="image/png",
                    variant="pixel",
                    dependencies=(source_id, request_id),
                    width=256,
                    height=256,
                ).id
            )

        preview_result = self._invoke("preview", sprite_preview.run, run_dir=run_dir)
        self._require_success("preview", preview_result)
        preview_ids: list[UUID] = []
        preview_files = ("qa/upload-contact.png", "qa/upload.gif", "qa/all-contact.png")
        for relative_path in preview_files:
            preview_path = self._run_file(run_dir, relative_path)
            preview_ids.append(
                self._put_artifact(
                    project.id,
                    preview_path.read_bytes(),
                    kind="sprite_preview",
                    media_type="image/gif" if preview_path.suffix == ".gif" else "image/png",
                    variant="preview",
                    dependencies=tuple(pixel_ids),
                ).id
            )

        diagnostics = self._redact({"segmentation": segmentation, **manifest}, run_dir)
        report_artifact = self._put_artifact(
            project.id,
            json.dumps(diagnostics, sort_keys=True, separators=(",", ":")).encode(),
            kind="sprite_report",
            media_type="application/json",
            variant="raw",
            dependencies=(*plain_ids, *pixel_ids),
        )
        self._write_state(
            run_dir,
            {
                "input_artifact_id": str(source_id),
                "request_artifact_id": str(request_id),
                "plain_artifact_ids": [str(item) for item in plain_ids],
                "pixel_artifact_ids": [str(item) for item in pixel_ids],
                "pixel_files": list(files),
                "preview_artifact_ids": [str(item) for item in preview_ids],
                "extract_report_artifact_id": str(report_artifact.id),
            },
        )
        return ExtractionResult(
            raw_artifact_id=source_id,
            plain_artifact_ids=tuple(plain_ids),
            pixel_artifact_ids=tuple(pixel_ids),
            preview_artifact_ids=tuple(preview_ids),
            report_artifact_id=report_artifact.id,
            selected_chroma=str(request["chroma_key"]["hex"]),
            outputs=("frames/frames-manifest.json", *files, *plain_files, *preview_files),
            diagnostics=diagnostics,
        )

    @_serialized_engine_mutation
    def normalize(
        self, project_id: UUID | str, *, nudge: tuple[int, int] | None = None
    ) -> ExtractionResult:
        """Auto scale + recenter every extracted frame, or nudge one frame sideways.

        Rewrites the canonical pixel frames (and their plain display twins) in
        place: shared nearest-neighbour integer scale, horizontal centre, foot
        baseline locked to the cell bottom. Resets curation and any composed
        atlas, since the frames they were stamped against changed.
        """
        from .imaging import normalize_frames, nudge_frame

        project = self.store.load(project_id)
        run_dir, state = self._extracted_state(project.id)
        source_id = self._state_uuid(state, "input_artifact_id")
        request_id = self._state_uuid(state, "request_artifact_id")
        self._current_pixel_dependencies(project.id, run_dir, state)
        files = self._relative_files(state.get("pixel_files"), "pixel frames")
        manifest_path = self._run_file(run_dir, "frames/frames-manifest.json")
        manifest = self._json_file(manifest_path, "sprite extraction report")
        row = next((item for item in manifest.get("rows", []) if item.get("state") == _UPLOAD_STATE), None)
        if not isinstance(row, dict):
            raise SpriteEngineError("sprite run has not been extracted")
        plain_files = self._relative_files(row.get("plain_files"), "plain frames")
        if len(plain_files) != len(files):
            raise SpriteEngineError("sprite extraction did not preserve frame twins")

        pixel_paths = [self._run_file(run_dir, item) for item in files]
        plain_paths = [self._run_file(run_dir, item) for item in plain_files]
        pixels = [path.read_bytes() for path in pixel_paths]
        plains = [path.read_bytes() for path in plain_paths]
        if nudge is None:
            pixels, plains = normalize_frames(pixels, plains)
        else:
            index, dx = nudge
            if not 0 <= index < len(pixels):
                raise SpriteEngineError("invalid frame index")
            moved_pixel, moved_plain = nudge_frame(pixels[index], plains[index], dx)
            pixels[index] = moved_pixel
            if moved_plain is not None:
                plains[index] = moved_plain

        shared_palette: set[tuple[int, int, int]] = set()
        for data in pixels:
            with Image.open(BytesIO(data)) as opened:
                shared_palette.update(self._validate_canonical_image(opened.convert("RGBA"), "normalized sprite frame"))
        if len(shared_palette) > 12:
            raise SpriteEngineError("canonical sprite frames exceed the shared palette")

        for path, data in zip((*pixel_paths, *plain_paths), (*pixels, *plains), strict=True):
            path.write_bytes(data)
        try:
            (run_dir / "curation.json").unlink(missing_ok=True)
        except OSError as exc:
            raise SpriteEngineError("sprite curation state could not be reset") from exc

        plain_ids: list[UUID] = []
        pixel_ids: list[UUID] = []
        for plain_data, pixel_data in zip(plains, pixels, strict=True):
            plain_ids.append(
                self._put_artifact(
                    project.id,
                    plain_data,
                    kind="sprite_frame",
                    media_type="image/png",
                    variant="plain",
                    dependencies=(source_id, request_id),
                    width=256,
                    height=256,
                ).id
            )
            pixel_ids.append(
                self._put_artifact(
                    project.id,
                    pixel_data,
                    kind="sprite_frame",
                    media_type="image/png",
                    variant="pixel",
                    dependencies=(source_id, request_id),
                    width=256,
                    height=256,
                ).id
            )

        preview_result = self._invoke("preview", sprite_preview.run, run_dir=run_dir)
        self._require_success("preview", preview_result)
        preview_ids: list[UUID] = []
        preview_files = ("qa/upload-contact.png", "qa/upload.gif", "qa/all-contact.png")
        for relative_path in preview_files:
            preview_path = self._run_file(run_dir, relative_path)
            preview_ids.append(
                self._put_artifact(
                    project.id,
                    preview_path.read_bytes(),
                    kind="sprite_preview",
                    media_type="image/gif" if preview_path.suffix == ".gif" else "image/png",
                    variant="preview",
                    dependencies=tuple(pixel_ids),
                ).id
            )

        self._write_state(
            run_dir,
            {
                "input_artifact_id": str(source_id),
                "request_artifact_id": str(request_id),
                "plain_artifact_ids": [str(item) for item in plain_ids],
                "pixel_artifact_ids": [str(item) for item in pixel_ids],
                "pixel_files": list(files),
                "preview_artifact_ids": [str(item) for item in preview_ids],
                "extract_report_artifact_id": str(self._state_uuid(state, "extract_report_artifact_id")),
            },
        )
        return ExtractionResult(
            raw_artifact_id=source_id,
            plain_artifact_ids=tuple(plain_ids),
            pixel_artifact_ids=tuple(pixel_ids),
            preview_artifact_ids=tuple(preview_ids),
            report_artifact_id=self._state_uuid(state, "extract_report_artifact_id"),
            selected_chroma=str(self._request(run_dir)["chroma_key"]["hex"]),
            outputs=tuple(files),
            diagnostics={},
        )

    @_serialized_engine_mutation
    def load_curation(self, project_id: UUID | str) -> CurationSnapshot:
        """Load the current upstream curation sidecar and its generation revision."""

        with _curation_boundary("curation load"):
            project = self.store.load(project_id)
            run_dir, state = self._extracted_state(project.id)
            with read_guard(run_dir):
                self._current_pixel_dependencies(project.id, run_dir, state)
                curation_ids = self._current_curation_dependencies(project.id, run_dir, state)
                self._validate_stored_curation(
                    project.id,
                    curation_ids,
                    int(self._request(run_dir)["states"][_UPLOAD_STATE]["frames"]),
                )
                payload = self._load_curation(run_dir)
                revision = run_revision(run_dir)
                if self._current_curation_dependencies(project.id, run_dir, state) != curation_ids:
                    raise SpriteEngineError("sprite curation provenance is unavailable")
                return self._curation_snapshot(
                    run_dir, state, payload, revision, curation_ids[0] if curation_ids else None
                )

    @_serialized_engine_mutation
    def run_snapshot(self, project_id: UUID | str) -> SpriteRunView:
        """Assemble the extracted-run view the local curator UI renders."""

        with _curation_boundary("run snapshot"):
            project = self.store.load(project_id)
            run_dir, state = self._extracted_state(project.id)
            request = self._request(run_dir)
            entry = request["states"][_UPLOAD_STATE]
            with read_guard(run_dir):
                pixel_ids = self._current_pixel_dependencies(project.id, run_dir, state)
                plain_ids = self._state_uuids(state, "plain_artifact_ids")
                curation_ids = self._current_curation_dependencies(project.id, run_dir, state)
                payload = self._load_curation(run_dir)
                revision = run_revision(run_dir)
                if self._current_curation_dependencies(project.id, run_dir, state) != curation_ids:
                    raise SpriteEngineError("sprite curation provenance is unavailable")
            cell = request.get("cell", {})
            atlas_id = state.get("atlas_artifact_id")
            manifest_id = state.get("manifest_artifact_id")
            return SpriteRunView(
                run_revision=revision,
                cell={
                    "width": int(cell.get("width", 256)),
                    "height": int(cell.get("height", 256)),
                },
                state=_UPLOAD_STATE,
                request_frames=int(entry["frames"]),
                fps=int(entry.get("fps", 1)),
                loop=bool(entry.get("loop", False)),
                pixel_artifact_ids=pixel_ids,
                plain_artifact_ids=plain_ids,
                curation=payload,
                has_atlas="atlas_artifact_id" in state,
                atlas_artifact_id=UUID(atlas_id) if isinstance(atlas_id, str) else None,
                manifest_artifact_id=UUID(manifest_id) if isinstance(manifest_id, str) else None,
                character_id=f"upload-{project.id.hex}",
            )

    @_serialized_engine_mutation
    def stamp_curation(
        self, project_id: UUID | str, payload: Mapping[str, Any]
    ) -> CurationSnapshot:
        """Atomically save a caller revision-checked upstream curation sidecar."""

        with _curation_boundary("curation update"):
            try:
                submitted = json.loads(json.dumps(payload))
            except (TypeError, ValueError) as exc:
                raise SpriteEngineError("invalid curation payload") from exc
            if not isinstance(submitted, dict):
                raise SpriteEngineError("invalid curation payload")
            if submitted.get("kind") != "sprite-gen-curation" or submitted.get("version") != 1:
                raise SpriteEngineError("invalid curation payload")
            if not isinstance(submitted.get("states"), dict) or set(submitted["states"]) - {_UPLOAD_STATE}:
                raise SpriteEngineError("invalid curation payload")
            expected = submitted.get("runRevision")
            if not isinstance(expected, str) or not expected:
                raise SpriteEngineError("curation runRevision is required")

            project = self.store.load(project_id)
            run_dir, state = self._extracted_state(project.id)
            request = self._request(run_dir)
            self._validate_curation_payload(
                submitted, int(request["states"][_UPLOAD_STATE]["frames"])
            )
            with read_guard(run_dir):
                pixel_ids = self._current_pixel_dependencies(project.id, run_dir, state)
                current = run_revision(run_dir)
                if expected != current:
                    raise SpriteEngineError("stale curation revision")
                stamped = stamp_curation(run_dir, submitted)
                sidecar_text = json.dumps(stamped, ensure_ascii=False, indent=2) + "\n"
                sidecar_bytes = sidecar_text.encode()
                atomic_write_text(run_dir / "curation.json", sidecar_text)
                artifact = self._put_artifact(
                    project.id,
                    sidecar_bytes,
                    kind="sprite_curation",
                    media_type="application/json",
                    variant="raw",
                    dependencies=pixel_ids,
                )
                next_state = {
                    key: value
                    for key, value in state.items()
                    if key
                    not in {
                        "atlas_artifact_id",
                        "manifest_artifact_id",
                        "compose_report_artifact_id",
                        "inspect_report_artifact_id",
                    }
                }
                next_state["curation_artifact_id"] = str(artifact.id)
                self._write_state(run_dir, next_state)
            return self._curation_snapshot(run_dir, state, stamped, current, artifact.id)

    @_serialized_engine_mutation
    def compose(self, project_id: UUID | str) -> ComposeResult:
        """Bake the selected upstream frames into a local atlas and manifest."""

        project = self.store.load(project_id)
        run_dir, state = self._extracted_state(project.id)
        pixel_ids = self._current_pixel_dependencies(project.id, run_dir, state)
        curation_ids = self._current_curation_dependencies(project.id, run_dir, state)
        with _curation_boundary("compose"):
            self._validate_stored_curation(
                project.id,
                curation_ids,
                int(self._request(run_dir)["states"][_UPLOAD_STATE]["frames"]),
            )
            self._load_curation(run_dir)
            if self._current_curation_dependencies(project.id, run_dir, state) != curation_ids:
                raise SpriteEngineError("sprite curation provenance is unavailable")
        try:
            result = self._invoke_writer("compose", run_dir, compose_atlas.run, run_dir=run_dir)
            self._require_success("compose", result)
        except SpriteEngineError:
            self._check_failed_compose(project.id, run_dir, state)
            raise
        except (
            SystemExit,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OverflowError,
        ) as exc:
            self._check_failed_compose(project.id, run_dir, state)
            raise SpriteEngineError("sprite compose failed") from exc

        self._current_pixel_dependencies(project.id, run_dir, state)
        if self._current_curation_dependencies(project.id, run_dir, state) != curation_ids:
            raise SpriteEngineError("sprite curation provenance changed during compose")
        atlas_path = self._run_file(run_dir, "sprite-sheet-alpha.png")
        manifest_path = self._run_file(run_dir, "manifest.json")
        report_path = self._run_file(run_dir, "sprite-sheet-alpha.report.json")
        self._validate_canonical_atlas(atlas_path, rows=len(self._request(run_dir)["states"]))
        atlas = self._put_artifact(
            project.id,
            atlas_path.read_bytes(),
            kind="sprite_atlas",
            media_type="image/png",
            variant="pixel",
            dependencies=(*pixel_ids, *curation_ids),
        )
        manifest = self._put_artifact(
            project.id,
            manifest_path.read_bytes(),
            kind="sprite_manifest",
            media_type="application/json",
            variant="raw",
            dependencies=(*pixel_ids, *curation_ids, atlas.id),
        )
        report = self._put_artifact(
            project.id,
            report_path.read_bytes(),
            kind="sprite_report",
            media_type="application/json",
            variant="raw",
            dependencies=(*pixel_ids, *curation_ids, atlas.id),
        )
        self._write_state(
            run_dir,
            {
                **state,
                "atlas_artifact_id": str(atlas.id),
                "manifest_artifact_id": str(manifest.id),
                "compose_report_artifact_id": str(report.id),
            },
        )
        return ComposeResult(
            atlas_artifact_id=atlas.id,
            manifest_artifact_id=manifest.id,
            report_artifact_id=report.id,
            outputs=("sprite-sheet-alpha.png", "manifest.json", "sprite-sheet-alpha.report.json"),
        )

    @_serialized_engine_mutation
    def inspect(self, project_id: UUID | str) -> InspectionResult:
        """Return a redacted upstream inspection report and preserve it immutably."""

        project = self.store.load(project_id)
        run_dir, state = self._extracted_state(project.id)
        dependencies = self._current_pixel_dependencies(project.id, run_dir, state)
        try:
            report = self._invoke("inspect", sprite_inspect.inspect_run, run_dir, states=_UPLOAD_STATE)
            if not isinstance(report, dict):
                raise SpriteEngineError("sprite inspection failed")
        except SpriteEngineError:
            try:
                self._current_pixel_dependencies(project.id, run_dir, state)
            except SpriteEngineError:
                pass
            raise
        self._current_pixel_dependencies(project.id, run_dir, state)
        summary = self._redact(report, run_dir)
        report_path = run_dir / "sprite-inspect.report.json"
        atomic_write_text(report_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        artifact = self._put_artifact(
            project.id,
            report_path.read_bytes(),
            kind="sprite_report",
            media_type="application/json",
            variant="raw",
            dependencies=dependencies,
        )
        self._write_state(run_dir, {**state, "inspect_report_artifact_id": str(artifact.id)})
        return InspectionResult(
            report_artifact_id=artifact.id,
            outputs=("sprite-inspect.report.json",),
            summary=summary,
        )

    def _validated_upload(self, data: bytes, *, media_type: str, filename: str) -> tuple[int, int, str]:
        if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
            raise SpriteEngineError("invalid upload")
        if not isinstance(media_type, str):
            raise SpriteEngineError("invalid upload media type")
        declared = media_type.strip().lower()
        if declared not in {entry[0] for entry in _MEDIA_FORMATS.values()}:
            raise SpriteEngineError("invalid upload media type")
        if not isinstance(filename, str) or not filename or filename != filename.strip():
            raise SpriteEngineError("invalid upload filename")
        if "\x00" in filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise SpriteEngineError("invalid upload filename")

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as opened:
                    opened.verify()
                with Image.open(BytesIO(data)) as opened:
                    actual_format = opened.format
                    frames = getattr(opened, "n_frames", 1)
                    animated = bool(getattr(opened, "is_animated", False))
                    width, height = opened.size
                    if width <= 0 or height <= 0 or width * height > MAX_INPUT_PIXELS:
                        raise SpriteEngineError("invalid upload")
                    opened.load()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise SpriteEngineError("invalid upload") from exc
        if actual_format not in _MEDIA_FORMATS:
            raise SpriteEngineError("invalid upload media type")
        expected_media_type, suffixes = _MEDIA_FORMATS[actual_format]
        if declared != expected_media_type or Path(filename).suffix.lower() not in suffixes:
            raise SpriteEngineError("upload declaration does not match image data")
        if frames != 1 or animated:
            raise SpriteEngineError("invalid upload")
        return width, height, expected_media_type

    @staticmethod
    def _upload_request(frames: int) -> dict[str, Any]:
        return {
            "cell": {"width": 256, "height": 256, "safe_margin_x": 0, "safe_margin_y": 0},
            "states": {
                _UPLOAD_STATE: {
                    "frames": frames,
                    "fps": 1,
                    "loop": False,
                    "action": "uploaded source",
                }
            },
            "fit": {
                "resample": "kcentroid",
                "align_x": "alpha-centroid",
                "align_y": "bottom",
                "pixel_perfect": True,
                "logical_height": 128,
                "palette_size": 12,
                "outline": False,
                "conform": False,
            },
            "chroma": dict(_CHROMA_DEFAULTS),
        }

    @staticmethod
    def _source_suffix(media_type: str) -> str:
        for expected_media_type, suffixes in _MEDIA_FORMATS.values():
            if media_type == expected_media_type:
                return sorted(suffixes)[0]
        raise SpriteEngineError("invalid upload artifact")

    def _bind_raw_source(
        self, run_dir: Path, request: dict[str, Any], source_data: bytes, *, replace: bool
    ) -> Path:
        """Write the verified immutable source into its engine-owned raw slot."""

        raw_path = self._run_file(run_dir, raw_rel(request, _UPLOAD_STATE), required=False)
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            if replace:
                # Drop any existing regular file or symlink first, then create
                # exclusively so a raced replacement is rejected, not followed.
                raw_path.unlink(missing_ok=True)
            with raw_path.open("xb") as raw_file:
                raw_file.write(source_data)
        except FileExistsError as exc:
            raise SpriteEngineError("sprite raw source already exists") from exc
        except OSError as exc:
            raise SpriteEngineError("sprite raw source could not be written") from exc
        return raw_path

    def _artifact_index(self, project_id: UUID) -> dict[UUID, ArtifactRef]:
        return {artifact.id: artifact for artifact in self.store.load(project_id).artifacts}

    @staticmethod
    def _require_artifact_role(
        artifacts: dict[UUID, ArtifactRef],
        artifact_id: UUID,
        *,
        kind: str,
        variant: str,
        dependencies: tuple[UUID, ...],
        failure: str,
    ) -> None:
        artifact = artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.stale
            or artifact.kind != kind
            or artifact.variant != variant
            or tuple(artifact.dependencies) != dependencies
        ):
            raise SpriteEngineError(failure)

    def _input_artifact(self, project_id: UUID, artifact_id: UUID | str) -> ArtifactRef:
        try:
            artifact_uuid = artifact_id if isinstance(artifact_id, UUID) else UUID(str(artifact_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise SpriteEngineError("invalid upload artifact") from exc
        project = self.store.load(project_id)
        artifact = next((item for item in project.artifacts if item.id == artifact_uuid), None)
        if (
            artifact is None
            or artifact.stale
            or artifact.kind != "input"
            or artifact.variant != "raw"
            or artifact.dependencies
        ):
            raise SpriteEngineError("invalid upload artifact")
        return artifact

    def _current_curation_dependencies(
        self, project_id: UUID, run_dir: Path, state: dict[str, Any]
    ) -> tuple[UUID, ...]:
        path = run_dir / "curation.json"
        if not path.is_file():
            if "curation_artifact_id" in state:
                raise SpriteEngineError("sprite curation provenance is unavailable")
            return ()
        if "curation_artifact_id" not in state:
            raise SpriteEngineError("sprite curation provenance is unavailable")
        artifact_id = self._state_uuid(state, "curation_artifact_id")
        try:
            self._require_artifact_role(
                self._artifact_index(project_id),
                artifact_id,
                kind="sprite_curation",
                variant="raw",
                dependencies=self._state_uuids(state, "pixel_artifact_ids"),
                failure="sprite curation provenance is unavailable",
            )
            if path.read_bytes() != self.store.read_artifact_bytes(project_id, artifact_id):
                raise SpriteEngineError("sprite curation provenance is unavailable")
        except SpriteEngineError:
            raise
        except (OSError, ProjectStoreError) as exc:
            raise SpriteEngineError("sprite curation provenance is unavailable") from exc
        return (artifact_id,)

    def _validate_stored_curation(
        self, project_id: UUID, artifact_ids: tuple[UUID, ...], frames: int
    ) -> None:
        if not artifact_ids:
            return
        try:
            payload = json.loads(self.store.read_artifact_bytes(project_id, artifact_ids[0]))
            if (
                not isinstance(payload, dict)
                or payload.get("kind") != "sprite-gen-curation"
                or type(payload.get("version")) is not int
                or payload["version"] != 1
                or not isinstance(payload.get("run_revision"), str)
                or not payload["run_revision"]
                or not isinstance(payload.get("states"), dict)
                or set(payload["states"]) - {_UPLOAD_STATE}
            ):
                raise SpriteEngineError("invalid sprite curation")
            self._validate_curation_payload(payload, frames)
        except SpriteEngineError as exc:
            raise SpriteEngineError("invalid sprite curation") from exc
        except (
            OSError,
            ProjectStoreError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as exc:
            raise SpriteEngineError("invalid sprite curation") from exc

    @staticmethod
    def _load_curation(run_dir: Path) -> dict[str, Any] | None:
        try:
            payload = load_curation(run_dir)
        except AttributeError as exc:
            raise SpriteEngineError("invalid sprite curation") from exc
        if payload is not None and (
            not isinstance(payload, dict)
            or ("states" in payload and not isinstance(payload["states"], dict))
        ):
            raise SpriteEngineError("invalid sprite curation")
        return payload

    def _reset_derived_state(self, run_dir: Path, state: dict[str, Any]) -> None:
        self._write_state(
            run_dir,
            {
                "input_artifact_id": str(self._state_uuid(state, "input_artifact_id")),
                "request_artifact_id": str(self._state_uuid(state, "request_artifact_id")),
            },
        )

    def _current_request_dependency(
        self, project_id: UUID, run_dir: Path, state: dict[str, Any]
    ) -> UUID:
        try:
            artifact_id = self._state_uuid(state, "request_artifact_id")
            self._require_artifact_role(
                self._artifact_index(project_id),
                artifact_id,
                kind="sprite_request",
                variant="raw",
                dependencies=(self._state_uuid(state, "input_artifact_id"),),
                failure="sprite request provenance is unavailable",
            )
            if (run_dir / "sprite-request.json").read_bytes() != self.store.read_artifact_bytes(
                project_id, artifact_id
            ):
                raise SpriteEngineError("sprite request provenance is unavailable")
        except SpriteEngineError:
            self._reset_derived_state(run_dir, state)
            raise
        except (OSError, ProjectStoreError) as exc:
            self._reset_derived_state(run_dir, state)
            raise SpriteEngineError("sprite request provenance is unavailable") from exc
        return artifact_id

    def _current_pixel_dependencies(
        self, project_id: UUID, run_dir: Path, state: dict[str, Any]
    ) -> tuple[UUID, ...]:
        self._current_request_dependency(project_id, run_dir, state)
        try:
            pixel_ids = self._state_uuids(state, "pixel_artifact_ids")
            lineage = (
                self._state_uuid(state, "input_artifact_id"),
                self._state_uuid(state, "request_artifact_id"),
            )
            artifacts = self._artifact_index(project_id)
            for artifact_id in pixel_ids:
                self._require_artifact_role(
                    artifacts,
                    artifact_id,
                    kind="sprite_frame",
                    variant="pixel",
                    dependencies=lineage,
                    failure="sprite frame provenance is unavailable",
                )
            expected_files = self._relative_files(state.get("pixel_files"), "sprite frame provenance")
            manifest = self._json_file(
                self._run_file(run_dir, "frames/frames-manifest.json"), "sprite extraction report"
            )
            rows = [
                row
                for row in manifest.get("rows", [])
                if isinstance(row, dict) and row.get("state") == _UPLOAD_STATE
            ]
            if len(rows) != 1:
                raise SpriteEngineError("sprite frame provenance is unavailable")
            files = self._relative_files(rows[0].get("files"), "sprite frame provenance")
            if (
                not pixel_ids
                or files != expected_files
                or len(files) != len(pixel_ids)
                or len(files) != len(set(files))
            ):
                raise SpriteEngineError("sprite frame provenance is unavailable")
            for relative_path, artifact_id in zip(files, pixel_ids, strict=True):
                if self._run_file(run_dir, relative_path).read_bytes() != self.store.read_artifact_bytes(
                    project_id, artifact_id
                ):
                    raise SpriteEngineError("sprite frame provenance is unavailable")
        except SpriteEngineError:
            self._reset_derived_state(run_dir, state)
            raise
        except (OSError, ProjectStoreError) as exc:
            self._reset_derived_state(run_dir, state)
            raise SpriteEngineError("sprite frame provenance is unavailable") from exc
        return pixel_ids

    @staticmethod
    def _curation_index(value: object, frames: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < frames:
            raise SpriteEngineError("invalid curation payload")
        return value

    @staticmethod
    def _finite_curation_number(value: object) -> bool:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)

    @classmethod
    def _curation_index_key(cls, value: object, frames: int) -> int:
        if not isinstance(value, str) or not value.isdecimal():
            raise SpriteEngineError("invalid curation payload")
        index = cls._curation_index(int(value), frames)
        if value != str(index):
            raise SpriteEngineError("invalid curation payload")
        return index

    @classmethod
    def _validate_curation_indices(cls, entry: dict[str, Any], key: str, frames: int) -> set[int]:
        if key not in entry:
            return set()
        values = entry[key]
        if not isinstance(values, list) or len(values) > frames:
            raise SpriteEngineError("invalid curation payload")
        indices = [cls._curation_index(value, frames) for value in values]
        if len(indices) != len(set(indices)):
            raise SpriteEngineError("invalid curation payload")
        return set(indices)

    @classmethod
    def _validate_curation_payload(cls, payload: dict[str, Any], frames: int) -> None:
        if frames < 1 or payload.get("pixel_perfect") is False:
            raise SpriteEngineError("invalid curation payload")
        if "pixel_perfect" in payload and not isinstance(payload["pixel_perfect"], bool):
            raise SpriteEngineError("invalid curation payload")
        entry = payload["states"].get(_UPLOAD_STATE)
        if entry is None:
            return
        if not isinstance(entry, dict) or entry.get("pixel_perfect") is False:
            raise SpriteEngineError("invalid curation payload")
        if "pixel_perfect" in entry and not isinstance(entry["pixel_perfect"], bool):
            raise SpriteEngineError("invalid curation payload")
        clones = entry.get("clones")
        if clones is not None and (not isinstance(clones, dict) or clones):
            raise SpriteEngineError("invalid curation payload")

        selected = cls._validate_curation_indices(entry, "selected", frames)
        deleted = cls._validate_curation_indices(entry, "deleted", frames)
        cls._validate_curation_indices(entry, "order", frames)
        if selected & deleted:
            raise SpriteEngineError("invalid curation payload")

        transforms = entry.get("transforms")
        if transforms is not None:
            if not isinstance(transforms, dict) or len(transforms) > frames:
                raise SpriteEngineError("invalid curation payload")
            for key, transform in transforms.items():
                cls._curation_index_key(key, frames)
                if not isinstance(transform, dict) or set(transform) - _CURATION_TRANSFORM_FIELDS:
                    raise SpriteEngineError("invalid curation payload")
                for name, value in transform.items():
                    if name in {"dx", "dy"}:
                        if (
                            isinstance(value, bool)
                            or not isinstance(value, int)
                            or abs(value) > 256
                            or value % 2
                        ):
                            raise SpriteEngineError("invalid curation payload")
                    elif name == "rotate":
                        if (
                            isinstance(value, bool)
                            or not isinstance(value, int)
                            or abs(value) > 360
                            or value % 90
                        ):
                            raise SpriteEngineError("invalid curation payload")
                    elif name == "scale" and (
                        not cls._finite_curation_number(value) or value != 1
                    ):
                        raise SpriteEngineError("invalid curation payload")
                    elif name in {"shx", "shy"} and (
                        not cls._finite_curation_number(value) or value != 0
                    ):
                        raise SpriteEngineError("invalid curation payload")
                    elif name == "flipX" and not (
                        isinstance(value, bool) or (type(value) is int and value in (0, 1))
                    ):
                        raise SpriteEngineError("invalid curation payload")

        pixels = entry.get("pixels")
        if pixels is not None:
            if not isinstance(pixels, dict) or len(pixels) > frames:
                raise SpriteEngineError("invalid curation payload")
            for frame_key, edits in pixels.items():
                cls._curation_index_key(frame_key, frames)
                if not isinstance(edits, dict) or len(edits) > 256 * 256:
                    raise SpriteEngineError("invalid curation payload")
                blocks: dict[tuple[int, int], dict[tuple[int, int], object]] = {}
                for coordinate, color in edits.items():
                    if not isinstance(coordinate, str):
                        raise SpriteEngineError("invalid curation payload")
                    match = _PIXEL_EDIT_COORDINATE.fullmatch(coordinate)
                    if match is None:
                        raise SpriteEngineError("invalid curation payload")
                    x, y = (int(value) for value in match.groups())
                    if x >= 256 or y >= 256 or (
                        color is not None
                        and (not isinstance(color, str) or re.fullmatch(r"#[0-9A-Fa-f]{6}", color) is None)
                    ):
                        raise SpriteEngineError("invalid curation payload")
                    blocks.setdefault((x // 2, y // 2), {})[(x, y)] = color
                for (block_x, block_y), block in blocks.items():
                    if set(block) != {
                        (block_x * 2, block_y * 2),
                        (block_x * 2 + 1, block_y * 2),
                        (block_x * 2, block_y * 2 + 1),
                        (block_x * 2 + 1, block_y * 2 + 1),
                    } or len(set(block.values())) != 1:
                        raise SpriteEngineError("invalid curation payload")

        selected_plan, _transforms = state_plan(payload, _UPLOAD_STATE, frames)
        if not selected_plan:
            raise SpriteEngineError("invalid curation payload")

    def _check_failed_compose(self, project_id: UUID, run_dir: Path, state: dict[str, Any]) -> None:
        try:
            self._current_pixel_dependencies(project_id, run_dir, state)
        except (
            SpriteEngineError,
            SystemExit,
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OverflowError,
        ):
            pass

    def _prepared_state(self, project_id: UUID) -> tuple[Path, dict[str, Any]]:
        run_dir = self.store.run_dir(project_id)
        state = self._state(run_dir)
        self._state_uuid(state, "input_artifact_id")
        self._current_request_dependency(project_id, run_dir, state)
        self._request(run_dir)
        return run_dir, state

    def _extracted_state(self, project_id: UUID) -> tuple[Path, dict[str, Any]]:
        run_dir, state = self._prepared_state(project_id)
        if not self._state_uuids(state, "pixel_artifact_ids"):
            raise SpriteEngineError("sprite run has not been extracted")
        return run_dir, state

    @staticmethod
    def _state(run_dir: Path) -> dict[str, Any]:
        return SpriteEngine._json_file(run_dir / _ENGINE_STATE, "sprite engine state")

    @staticmethod
    def _write_state(run_dir: Path, state: dict[str, Any]) -> None:
        atomic_write_text(run_dir / _ENGINE_STATE, json.dumps(state, sort_keys=True) + "\n")

    @staticmethod
    def _state_uuid(state: dict[str, Any], key: str) -> UUID:
        try:
            return UUID(str(state[key]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SpriteEngineError("invalid sprite engine state") from exc

    @classmethod
    def _state_uuids(cls, state: dict[str, Any], key: str) -> tuple[UUID, ...]:
        values = state.get(key, [])
        if not isinstance(values, list):
            raise SpriteEngineError("invalid sprite engine state")
        try:
            result = tuple(UUID(str(value)) for value in values)
        except (TypeError, ValueError) as exc:
            raise SpriteEngineError("invalid sprite engine state") from exc
        if len(result) != len(set(result)):
            raise SpriteEngineError("invalid sprite engine state")
        return result

    @staticmethod
    def _json_file(path: Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpriteEngineError(f"invalid {label}") from exc
        if not isinstance(value, dict):
            raise SpriteEngineError(f"invalid {label}")
        return value

    @classmethod
    def _request(cls, run_dir: Path) -> dict[str, Any]:
        request = cls._json_file(run_dir / "sprite-request.json", "sprite request")
        if _UPLOAD_STATE not in request.get("states", {}):
            raise SpriteEngineError("invalid sprite request")
        return request

    @staticmethod
    def _relative_files(value: object, label: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SpriteEngineError(f"invalid {label}")
        return tuple(value)

    @staticmethod
    def _run_file(run_dir: Path, relative_path: str, *, required: bool = True) -> Path:
        path = PurePath(relative_path)
        if not relative_path or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise SpriteEngineError("invalid sprite output path")
        result = run_dir.joinpath(*path.parts)
        if required and not result.is_file():
            raise SpriteEngineError("expected sprite output is missing")
        return result

    @staticmethod
    def _validate_plain_twin(path: Path) -> None:
        try:
            with Image.open(path) as opened:
                if opened.size != (256, 256):
                    raise SpriteEngineError("plain sprite twin has invalid dimensions")
        except (OSError, UnidentifiedImageError) as exc:
            raise SpriteEngineError("plain sprite twin is invalid") from exc

    @staticmethod
    def _validate_canonical_image(
        frame: Image.Image, label: str
    ) -> set[tuple[int, int, int]]:
        if frame.mode != "RGBA" or frame.size != (256, 256):
            raise SpriteEngineError(f"{label} has invalid geometry")
        alpha = frame.getchannel("A")
        if set(alpha.get_flattened_data()) - {0, 255}:
            raise SpriteEngineError(f"{label} has non-binary alpha")
        if alpha.getbbox() is None or alpha.getbbox()[3] != 256:
            raise SpriteEngineError(f"{label} does not use the foot anchor")
        opaque = {pixel[:3] for pixel in frame.get_flattened_data() if pixel[3] == 255}
        if not opaque or len(opaque) > 12:
            raise SpriteEngineError(f"{label} exceeds the shared palette")
        pixels = frame.load()
        for y in range(0, 256, 2):
            for x in range(0, 256, 2):
                block = [pixels[x + dx, y + dy] for dy in range(2) for dx in range(2)]
                if any(pixel[3] for pixel in block) and len(set(block)) != 1:
                    raise SpriteEngineError(f"{label} is not on the logical grid")
        return opaque

    @classmethod
    def _validate_canonical_frame(cls, path: Path) -> set[tuple[int, int, int]]:
        try:
            with Image.open(path) as opened:
                frame = opened.copy()
        except (OSError, UnidentifiedImageError) as exc:
            raise SpriteEngineError("canonical sprite frame is invalid") from exc
        return cls._validate_canonical_image(frame, "canonical sprite frame")

    @classmethod
    def _validate_canonical_atlas(cls, path: Path, *, rows: int) -> None:
        try:
            with Image.open(path) as opened:
                atlas = opened.copy()
        except (OSError, UnidentifiedImageError) as exc:
            raise SpriteEngineError("canonical sprite atlas is invalid") from exc
        if (
            rows < 1
            or atlas.mode != "RGBA"
            or atlas.width < 256
            or atlas.width % 256
            or atlas.height != rows * 256
        ):
            raise SpriteEngineError("canonical sprite atlas has invalid geometry")
        palette: set[tuple[int, int, int]] = set()
        for top in range(0, atlas.height, 256):
            for left in range(0, atlas.width, 256):
                palette.update(
                    cls._validate_canonical_image(
                        atlas.crop((left, top, left + 256, top + 256)), "canonical sprite atlas"
                    )
                )
        if len(palette) > 12:
            raise SpriteEngineError("canonical sprite atlas exceeds the shared palette")

    def _put_artifact(
        self,
        project_id: UUID,
        data: bytes,
        *,
        kind: str,
        media_type: str,
        variant: str,
        dependencies: tuple[UUID, ...] | tuple[UUID] | list[UUID],
        width: int | None = None,
        height: int | None = None,
    ) -> ArtifactRef:
        try:
            return self.store.put_artifact(
                project_id,
                data,
                kind=kind,
                media_type=media_type,
                variant=variant,  # type: ignore[arg-type]
                stage=_ARTIFACT_STAGE,
                width=width,
                height=height,
                dependencies=dependencies,
            )
        except ProjectStoreError as exc:
            raise SpriteEngineError("sprite artifact could not be stored") from exc

    @staticmethod
    def _invoke(operation: str, runner: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return runner(*args, **kwargs)
        except SpriteEngineError:
            raise
        except SystemExit as exc:
            raise SpriteEngineError(f"sprite {operation} failed") from exc
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
            AttributeError,
            OverflowError,
        ) as exc:
            raise SpriteEngineError(f"sprite {operation} failed") from exc

    @staticmethod
    def _invoke_writer(
        operation: str, run_dir: Path, runner: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            return SpriteEngine._invoke(operation, runner, *args, **kwargs)
        finally:
            release_run_dir_lock(run_dir)

    @staticmethod
    def _require_success(operation: str, result: object) -> None:
        if result != 0:
            raise SpriteEngineError(f"sprite {operation} failed")

    def _failure_diagnostics(self, run_dir: Path) -> dict[str, Any]:
        path = run_dir / "extract-failure.json"
        if not path.is_file():
            return {}
        try:
            return self._redact(self._json_file(path, "sprite extraction diagnostics"), run_dir)
        except SpriteEngineError:
            return {}

    @staticmethod
    def _redact(value: Any, run_dir: Path) -> Any:
        if isinstance(value, dict):
            return {
                str(SpriteEngine._redact(str(key), run_dir)): SpriteEngine._redact(item, run_dir)
                for key, item in value.items()
                if str(key) != "run_dir"
            }
        if isinstance(value, list):
            return [SpriteEngine._redact(item, run_dir) for item in value]
        if isinstance(value, Path):
            return "<path>"
        if isinstance(value, str):
            cleaned = value.replace(str(run_dir), "<run>")
            return _ABSOLUTE_PATH.sub("<path>", cleaned)
        return value

    def _curation_snapshot(
        self,
        run_dir: Path,
        state: dict[str, Any],
        payload: dict[str, Any] | None,
        revision: str,
        artifact_id: UUID | None = None,
    ) -> CurationSnapshot:
        request = self._request(run_dir)
        frames = int(request["states"][_UPLOAD_STATE]["frames"])
        selected, _transforms = state_plan(payload, _UPLOAD_STATE, frames)
        return CurationSnapshot(
            run_revision=revision,
            payload=payload,
            selected={_UPLOAD_STATE: tuple(selected)},
            variants={_UPLOAD_STATE: frame_variant(payload, _UPLOAD_STATE)},
            artifact_id=artifact_id,
        )
