"""Local adapter around the pinned ``sprite-gen`` component-row engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import BytesIO, StringIO
import json
from pathlib import Path, PurePath
import re
import tempfile
from typing import Any
from uuid import UUID
import warnings

from PIL import Image, UnidentifiedImageError
from sprite_gen import compose_atlas, extract as sprite_extract, inspect as sprite_inspect
from sprite_gen import prepare as sprite_prepare
from sprite_gen import preview as sprite_preview
from sprite_gen.curation import frame_variant, load_curation, run_revision, stamp_curation, state_plan
from sprite_gen.layout import raw_rel
from sprite_gen.runio import atomic_write_text, read_guard

from .contracts import ArtifactRef
from .project_store import ProjectStore, ProjectStoreError


MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_INPUT_PIXELS = 16_000_000
_ENGINE_STATE = "sprite-engine.json"
_UPLOAD_STATE = "upload"
_ARTIFACT_STAGE = "sprite_upload"
_MEDIA_FORMATS = {
    "PNG": ("image/png", {".png"}),
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
    "WEBP": ("image/webp", {".webp"}),
}
_ABSOLUTE_PATH = re.compile(r"(?<![\w.-])/(?:[^\s\"']+)")


class SpriteEngineError(ProjectStoreError):
    """A redacted, app-specific local sprite engine failure."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


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

    def prepare(
        self,
        project_id: UUID | str,
        input_artifact_id: UUID | str,
        *,
        frames: int = 1,
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
                chroma_key="auto",
            )
        self._require_success("prepare", result)

        prepared_request = self._request(run_dir)
        raw_path = self._run_file(run_dir, raw_rel(prepared_request, _UPLOAD_STATE), required=False)
        try:
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            with raw_path.open("xb") as raw_file:
                raw_file.write(source_data)
        except FileExistsError as exc:
            raise SpriteEngineError("sprite raw source already exists") from exc
        except OSError as exc:
            raise SpriteEngineError("sprite raw source could not be written") from exc

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

    def extract(
        self, project_id: UUID | str, *, segmentation: str = "components"
    ) -> ExtractionResult:
        """Recover one upload row with components or explicitly requested projection."""

        if segmentation not in {"components", "projection"}:
            raise SpriteEngineError("unsupported sprite segmentation")
        project = self.store.load(project_id)
        run_dir, state = self._prepared_state(project.id)
        result = self._invoke(
            "extract",
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

        source_id = self._state_uuid(state, "input_artifact_id")
        plain_ids: list[UUID] = []
        pixel_ids: list[UUID] = []
        for plain_file, pixel_file in zip(plain_files, files, strict=True):
            plain_path = self._run_file(run_dir, plain_file)
            pixel_path = self._run_file(run_dir, pixel_file)
            self._validate_plain_twin(plain_path)
            self._validate_canonical_frame(pixel_path)
            plain_ids.append(
                self._put_artifact(
                    project.id,
                    plain_path.read_bytes(),
                    kind="sprite_frame",
                    media_type="image/png",
                    variant="plain",
                    dependencies=(source_id,),
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
                    dependencies=(source_id,),
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
                **state,
                "plain_artifact_ids": [str(item) for item in plain_ids],
                "pixel_artifact_ids": [str(item) for item in pixel_ids],
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

    def load_curation(self, project_id: UUID | str) -> CurationSnapshot:
        """Load the current upstream curation sidecar and its generation revision."""

        project = self.store.load(project_id)
        run_dir, state = self._extracted_state(project.id)
        with read_guard(run_dir):
            payload = load_curation(run_dir)
            revision = run_revision(run_dir)
            return self._curation_snapshot(run_dir, state, payload, revision)

    def stamp_curation(
        self, project_id: UUID | str, payload: Mapping[str, Any]
    ) -> CurationSnapshot:
        """Atomically save a caller revision-checked upstream curation sidecar."""

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
        with read_guard(run_dir):
            current = run_revision(run_dir)
            if expected != current:
                raise SpriteEngineError("stale curation revision")
            stamped = stamp_curation(run_dir, submitted)
            atomic_write_text(
                run_dir / "curation.json",
                json.dumps(stamped, ensure_ascii=False, indent=2) + "\n",
            )

        pixel_ids = self._state_uuids(state, "pixel_artifact_ids")
        artifact = self._put_artifact(
            project.id,
            (run_dir / "curation.json").read_bytes(),
            kind="sprite_curation",
            media_type="application/json",
            variant="raw",
            dependencies=pixel_ids,
        )
        self._write_state(run_dir, {**state, "curation_artifact_id": str(artifact.id)})
        return self._curation_snapshot(run_dir, state, stamped, current, artifact.id)

    def compose(self, project_id: UUID | str) -> ComposeResult:
        """Bake the selected upstream frames into a local atlas and manifest."""

        project = self.store.load(project_id)
        run_dir, state = self._extracted_state(project.id)
        result = self._invoke("compose", compose_atlas.run, run_dir=run_dir)
        self._require_success("compose", result)

        pixel_ids = self._state_uuids(state, "pixel_artifact_ids")
        atlas_path = self._run_file(run_dir, "sprite-sheet-alpha.png")
        manifest_path = self._run_file(run_dir, "manifest.json")
        report_path = self._run_file(run_dir, "sprite-sheet-alpha.report.json")
        atlas = self._put_artifact(
            project.id,
            atlas_path.read_bytes(),
            kind="sprite_atlas",
            media_type="image/png",
            variant="pixel",
            dependencies=pixel_ids,
        )
        manifest = self._put_artifact(
            project.id,
            manifest_path.read_bytes(),
            kind="sprite_manifest",
            media_type="application/json",
            variant="raw",
            dependencies=(*pixel_ids, atlas.id),
        )
        report = self._put_artifact(
            project.id,
            report_path.read_bytes(),
            kind="sprite_report",
            media_type="application/json",
            variant="raw",
            dependencies=(*pixel_ids, atlas.id),
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

    def inspect(self, project_id: UUID | str) -> InspectionResult:
        """Return a redacted upstream inspection report and preserve it immutably."""

        project = self.store.load(project_id)
        run_dir, state = self._extracted_state(project.id)
        report = self._invoke("inspect", sprite_inspect.inspect_run, run_dir, states=_UPLOAD_STATE)
        if not isinstance(report, dict):
            raise SpriteEngineError("sprite inspection failed")
        summary = self._redact(report, run_dir)
        report_path = run_dir / "sprite-inspect.report.json"
        atomic_write_text(report_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        dependencies = self._state_uuids(state, "pixel_artifact_ids")
        if "atlas_artifact_id" in state:
            dependencies = (*dependencies, self._state_uuid(state, "atlas_artifact_id"))
        artifact = self._put_artifact(
            project.id,
            report_path.read_bytes(),
            kind="sprite_report",
            media_type="application/json",
            variant="raw",
            dependencies=dependencies,
        )
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
        }

    @staticmethod
    def _source_suffix(media_type: str) -> str:
        for expected_media_type, suffixes in _MEDIA_FORMATS.values():
            if media_type == expected_media_type:
                return sorted(suffixes)[0]
        raise SpriteEngineError("invalid upload artifact")

    def _input_artifact(self, project_id: UUID, artifact_id: UUID | str) -> ArtifactRef:
        try:
            artifact_uuid = artifact_id if isinstance(artifact_id, UUID) else UUID(str(artifact_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise SpriteEngineError("invalid upload artifact") from exc
        project = self.store.load(project_id)
        artifact = next((item for item in project.artifacts if item.id == artifact_uuid), None)
        if artifact is None or artifact.stale or artifact.kind != "input" or artifact.variant != "raw":
            raise SpriteEngineError("invalid upload artifact")
        return artifact

    def _prepared_state(self, project_id: UUID) -> tuple[Path, dict[str, Any]]:
        run_dir = self.store.run_dir(project_id)
        state = self._state(run_dir)
        self._state_uuid(state, "input_artifact_id")
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
    def _validate_canonical_frame(path: Path) -> None:
        try:
            with Image.open(path) as opened:
                if opened.mode != "RGBA" or opened.size != (256, 256):
                    raise SpriteEngineError("canonical sprite frame has invalid geometry")
                frame = opened.copy()
        except (OSError, UnidentifiedImageError) as exc:
            raise SpriteEngineError("canonical sprite frame is invalid") from exc
        alpha = frame.getchannel("A")
        if set(alpha.get_flattened_data()) - {0, 255}:
            raise SpriteEngineError("canonical sprite frame has non-binary alpha")
        if alpha.getbbox() is None or alpha.getbbox()[3] != 256:
            raise SpriteEngineError("canonical sprite frame does not use the foot anchor")
        opaque = {pixel[:3] for pixel in frame.get_flattened_data() if pixel[3] == 255}
        if not opaque or len(opaque) > 12:
            raise SpriteEngineError("canonical sprite frame exceeds the shared palette")
        pixels = frame.load()
        for y in range(0, 256, 2):
            for x in range(0, 256, 2):
                block = [pixels[x + dx, y + dy] for dy in range(2) for dx in range(2)]
                if any(pixel[3] for pixel in block) and len(set(block)) != 1:
                    raise SpriteEngineError("canonical sprite frame is not on the logical grid")

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
        except SystemExit as exc:
            raise SpriteEngineError(f"sprite {operation} failed") from exc
        except (OSError, ValueError, RuntimeError, KeyError, json.JSONDecodeError) as exc:
            raise SpriteEngineError(f"sprite {operation} failed") from exc

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
                str(key): SpriteEngine._redact(item, run_dir)
                for key, item in value.items()
                if key != "run_dir"
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
