"""Versioned, provider-free prompt assets for the sprite pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
from io import BytesIO
import json
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID

from PIL import Image

from .contracts import ArtifactRef, ProjectConfig, StateSpec
from .project_store import ProjectStore


_REPOSITORY_URL = "https://github.com/chongdashu/ai-pixel-snapped-game-sprites"
_REVISION = "5f4c8b99a1de38a84af7dcc36af80622e5057cd6"
_HEADER_PREFIX = "<!-- ai-sprite-studio-prompt-provenance:"
_HEADER_SUFFIX = " -->"
_TOKEN = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_RESERVED_RECORD_KEYS = frozenset({"text", "job_id"})
_PROVENANCE_KEYS = frozenset(
    {
        "stage_key",
        "source_repository",
        "locked_revision",
        "source_path",
        "source_sha256",
        "canonical_substitutions",
        "correction_ids",
        "guide",
        "artifact_inputs",
        "locked_context",
        "rendered_sha256",
    }
)
_DIRECTION_SENSITIVE_STAGES = frozenset(
    {"directional_anchors", "action_poseboards", "walk_i2v"}
)
_MIRRORED_GENERATION_STAGES = frozenset(
    {"directional_anchors", "action_poseboards", "walk_i2v"}
)
_ACTION_DIRECTION_DESCRIPTIONS = MappingProxyType(
    {
        "down": "front view, facing down",
        "right": "profile view, facing screen-right",
        "up": "back view, facing up",
        "left": "profile view, facing screen-left",
    }
)


class PromptError(ValueError):
    """A requested prompt stage or rendered prompt is invalid."""


@dataclass(frozen=True)
class PromptStage:
    """One pinned upstream prompt source."""

    key: str
    source_path: str
    source_sha256: str
    repository_url: str = _REPOSITORY_URL
    revision: str = _REVISION


@dataclass(frozen=True)
class RenderedPrompt:
    """A rendered prompt document and the provenance embedded above it."""

    text: str
    provenance: dict[str, Any]
    sha256: str

    def to_record(self) -> dict[str, Any]:
        """Return the JSON object persisted for a rendered prompt."""

        if not isinstance(self.provenance, dict):
            raise PromptError("rendered prompt provenance must be an object")
        if _RESERVED_RECORD_KEYS.intersection(self.provenance):
            raise PromptError("rendered prompt provenance contains a reserved record key")
        if parse_provenance(self.text) != self.provenance:
            raise PromptError("rendered prompt provenance does not match its header")
        _, _, body = self.text.partition("\n")
        if self.provenance.get("rendered_sha256") != self.sha256 or sha256(body.encode()).hexdigest() != self.sha256:
            raise PromptError("rendered prompt hash does not match its body")
        return {**self.provenance, "text": self.text}


@dataclass(frozen=True)
class GuideImage:
    """A deterministic RGB checkerboard used as an image-generation guide."""

    data: bytes
    width: int
    height: int
    sha256: str


STAGES: Mapping[str, PromptStage] = MappingProxyType(
    {
        "base_generation": PromptStage(
            "base_generation",
            "prompts/01-south-anchor.md",
            "f35ebfdbda144aafa6b6920fb7d80198060f73ec3379788fd3bd40109e82c7a2",
        ),
        "base_snap": PromptStage(
            "base_snap",
            "prompts/02-pixel-snap.md",
            "c6f0b4773b5689679695a803aed51709363d275a0de926f6bcf78040cb76306a",
        ),
        "directional_anchors": PromptStage(
            "directional_anchors",
            "prompts/03-directional-anchors.md",
            "650b03cc94bc1167b993cdc334a8f7f2119be559ad56343e1d49d6948e48616f",
        ),
        "action_poseboards": PromptStage(
            "action_poseboards",
            "prompts/04-action-spritesheet.md",
            "6a8586da63b82e2feb0e4ed3ef43d9b793f6745cf119eb06f424384b7e78614a",
        ),
        "frame_recovery": PromptStage(
            "frame_recovery",
            "prompts/05-frame-recovery.md",
            "56e61866d0f6cc87209924d9404a86b3d4872b22cd994a190827c2ab30aba654",
        ),
        "walk_i2v": PromptStage(
            "walk_i2v",
            "prompts/06-walk-cycle-i2v.md",
            "4ae4cb3afe62280fcb2fd47e4a3fbb203901b1d060ee17dcf4492737a0288f32",
        ),
        "per_frame_snap": PromptStage(
            "per_frame_snap",
            "prompts/07-per-frame-snap.md",
            "1bd5c0eecffaec5f5a1d0fe0de8ce740df6beda245e5002fa70412157b4c5611",
        ),
        "runtime_normalize": PromptStage(
            "runtime_normalize",
            "prompts/08-runtime-normalize-and-align.md",
            "33024f62f658dd1090f87cd45b12a278ec6ab7ef5c869731a3b32f1aa98a3e68",
        ),
    }
)


def list_stages() -> tuple[PromptStage, ...]:
    """Return the canonical prompt stages in pipeline order."""

    return tuple(STAGES.values())


def load_source(stage_key: str) -> str:
    """Load one verified prompt source from the installed package assets."""

    try:
        stage = STAGES[stage_key]
    except (KeyError, TypeError) as exc:
        raise PromptError("unknown prompt stage") from exc
    source = (
        files("ai_sprite_studio")
        .joinpath("assets", "upstream", "ai-pixel-snapped-game-sprites", *stage.source_path.split("/"))
        .read_text(encoding="utf-8")
    )
    if sha256(source.encode()).hexdigest() != stage.source_sha256:
        raise PromptError("prompt source does not match its pinned hash")
    return source


def render_prompt(
    stage_key: str,
    project: ProjectConfig,
    substitutions: Mapping[str, str | int],
    *,
    direction: str | None = None,
    state: StateSpec | str | None = None,
    artifact_inputs: Iterable[ArtifactRef | Mapping[str, object]] = (),
) -> RenderedPrompt:
    """Render one locked source with exactly its declared substitutions."""

    if not isinstance(project, ProjectConfig):
        raise PromptError("project must be a ProjectConfig")
    try:
        stage = STAGES[stage_key]
    except (KeyError, TypeError) as exc:
        raise PromptError("unknown prompt stage") from exc
    source, corrections = _apply_corrections(stage.key, load_source(stage.key))
    canonical = _canonical_substitutions(source, substitutions)
    _validate_direction_substitution(stage.key, project, canonical, direction)
    rendered_source = _TOKEN.sub(lambda match: canonical[match.group(1)], source)
    if _TOKEN.search(rendered_source):
        raise PromptError("rendered prompt has an unresolved template token")

    context = _locked_context(project, direction=direction, state=state)
    inputs = _artifact_inputs(project, artifact_inputs)
    guide = guide_for_stage(stage.key)
    body = "## Locked project context\n\n```json\n" + _json(context) + "\n```\n\n" + rendered_source
    digest = sha256(body.encode()).hexdigest()
    provenance = {
        "stage_key": stage.key,
        "source_repository": stage.repository_url,
        "locked_revision": stage.revision,
        "source_path": stage.source_path,
        "source_sha256": stage.source_sha256,
        "canonical_substitutions": canonical,
        "correction_ids": list(corrections),
        "guide": (
            None
            if guide is None
            else {"width": guide.width, "height": guide.height, "sha256": guide.sha256}
        ),
        "artifact_inputs": inputs,
        "locked_context": context,
        "rendered_sha256": digest,
    }
    text = _HEADER_PREFIX + _json(provenance) + _HEADER_SUFFIX + "\n" + body
    return RenderedPrompt(text=text, provenance=provenance, sha256=digest)


def parse_provenance(rendered_text: str) -> dict[str, Any]:
    """Parse the canonical provenance header from a rendered prompt."""

    if not isinstance(rendered_text, str):
        raise PromptError("rendered prompt must be text")
    header, separator, _ = rendered_text.partition("\n")
    if not separator or not header.startswith(_HEADER_PREFIX) or not header.endswith(_HEADER_SUFFIX):
        raise PromptError("rendered prompt has no provenance header")
    try:
        provenance = json.loads(header[len(_HEADER_PREFIX) : -len(_HEADER_SUFFIX)])
    except json.JSONDecodeError as exc:
        raise PromptError("rendered prompt has malformed provenance") from exc
    if not isinstance(provenance, dict):
        raise PromptError("rendered prompt provenance must be an object")
    return provenance


def guide_for_stage(stage_key: str) -> GuideImage | None:
    """Return the deterministic guide required by a stage, if it has one."""

    if stage_key not in STAGES:
        raise PromptError("unknown prompt stage")
    size = {
        "directional_anchors": (1024, 1024),
        "action_poseboards": (2048, 1536),
    }.get(stage_key)
    return None if size is None else _checkerboard_guide(*size)


def persist_prompt(
    store: ProjectStore,
    project_id: UUID | str,
    job_id: UUID | str,
    rendered: RenderedPrompt,
) -> dict[str, Any]:
    """Persist one rendered prompt as the immutable record for a job."""

    if not isinstance(rendered, RenderedPrompt):
        raise PromptError("rendered prompt is invalid")
    try:
        job_id = UUID(str(job_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PromptError("invalid job ID") from exc
    canonical = _canonical_persisted_prompt(store, project_id, rendered)
    return store.save_prompt(
        project_id,
        job_id,
        {**canonical.to_record(), "job_id": str(job_id)},
    )


@lru_cache(maxsize=2)
def _checkerboard_guide(width: int, height: int) -> GuideImage:
    black = b"\0\0\0"
    white = b"\xff\xff\xff"
    even_row = (black + white) * (width // 2) + (black if width % 2 else b"")
    odd_row = (white + black) * (width // 2) + (white if width % 2 else b"")
    raw = (even_row + odd_row) * (height // 2) + (even_row if height % 2 else b"")
    image = Image.frombytes("RGB", (width, height), raw)
    buffer = BytesIO()
    try:
        image.save(buffer, format="PNG", compress_level=9)
    finally:
        image.close()
    data = buffer.getvalue()
    return GuideImage(data=data, width=width, height=height, sha256=sha256(data).hexdigest())


def _canonical_substitutions(
    source: str, substitutions: Mapping[str, str | int]
) -> dict[str, str]:
    if not isinstance(substitutions, Mapping):
        raise PromptError("prompt substitutions must be a mapping")
    if any(not isinstance(key, str) for key in substitutions):
        raise PromptError("unknown prompt substitutions")
    expected = set(_TOKEN.findall(source))
    supplied = set(substitutions)
    if missing := expected - supplied:
        raise PromptError(f"missing prompt substitutions: {', '.join(sorted(missing))}")
    if unknown := supplied - expected:
        raise PromptError(f"unknown prompt substitutions: {', '.join(sorted(unknown))}")

    canonical: dict[str, str] = {}
    for key in sorted(expected):
        value = substitutions[key]
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise PromptError(f"invalid prompt substitution: {key}")
        value = str(value)
        if not value.strip() or _TOKEN.search(value):
            raise PromptError(f"invalid prompt substitution: {key}")
        canonical[key] = value
    return canonical


def _validate_direction_substitution(
    stage_key: str,
    project: ProjectConfig,
    substitutions: Mapping[str, str],
    direction: str | None,
) -> None:
    if stage_key not in _DIRECTION_SENSITIVE_STAGES:
        return
    if direction is None:
        raise PromptError("direction-sensitive prompts require an explicit direction")
    if (
        stage_key in _MIRRORED_GENERATION_STAGES
        and project.side_policy == "mirror"
        and direction == "left"
    ):
        raise PromptError("mirrored projects derive left instead of generating it")
    if "DIRECTION" in substitutions and substitutions["DIRECTION"] != direction:
        raise PromptError("DIRECTION substitution must match the supplied direction")
    if stage_key == "action_poseboards":
        try:
            expected_description = _ACTION_DIRECTION_DESCRIPTIONS[direction]
        except KeyError as exc:
            raise PromptError("direction is not configured for this project") from exc
        if substitutions.get("DIRECTION_DESCRIPTION") != expected_description:
            raise PromptError("action poseboard direction description must match its direction")


def _canonical_persisted_prompt(
    store: ProjectStore, project_id: UUID | str, rendered: RenderedPrompt
) -> RenderedPrompt:
    rendered.to_record()
    provenance = rendered.provenance
    if set(provenance) != _PROVENANCE_KEYS:
        raise PromptError("rendered prompt provenance has invalid keys")
    stage_key = provenance["stage_key"]
    if not isinstance(stage_key, str) or stage_key not in STAGES:
        raise PromptError("rendered prompt has an invalid stage")
    substitutions = provenance["canonical_substitutions"]
    if not isinstance(substitutions, Mapping):
        raise PromptError("rendered prompt substitutions must be a mapping")
    artifact_inputs = provenance["artifact_inputs"]
    if not isinstance(artifact_inputs, list):
        raise PromptError("rendered prompt artifact inputs must be a list")
    context = provenance["locked_context"]
    if not isinstance(context, dict):
        raise PromptError("rendered prompt context must be an object")
    direction = context.get("direction")
    if direction is not None and not isinstance(direction, str):
        raise PromptError("rendered prompt context has an invalid direction")
    state = context.get("state")
    if state is None:
        state_id = None
    elif isinstance(state, dict) and isinstance(state.get("id"), str):
        state_id = state["id"]
    else:
        raise PromptError("rendered prompt context has an invalid state")

    canonical = render_prompt(
        stage_key,
        store.load(project_id),
        substitutions,
        direction=direction,
        state=state_id,
        artifact_inputs=artifact_inputs,
    )
    if (
        rendered.text != canonical.text
        or rendered.provenance != canonical.provenance
        or rendered.sha256 != canonical.sha256
    ):
        raise PromptError("rendered prompt does not match the canonical project render")
    return canonical


def _apply_corrections(stage_key: str, source: str) -> tuple[str, tuple[str, ...]]:
    if stage_key == "base_snap":
        source = source.replace(
            "If you snap the south candidate at 96×96 and then snap the *west* anchor "
            "(a fresh generation) at 102×101, both are correct. Each anchor (N/S/E/W) "
            "is an independent generation.",
            "If you snap the down-facing candidate at 96×96 and then snap the *right* anchor "
            "(a fresh generation) at 102×101, both are correct. Down, right, and up are "
            "independent generations. For mirrored projects, left is derived by mirroring right. "
            "For independent-side projects, generate left separately.",
        )
        return source, ("direction-policy-right-up-left",)
    if stage_key == "directional_anchors":
        source = source.replace(
            "# 03 — Directional Anchors (NSEW from the snapped south)",
            "# 03 — Directional Anchors (down/right/up with policy-specific left)",
        ).replace(
            "Generate west and north anchors from the snapped south. East is a horizontal flip of west "
            "— don't generate it separately.",
            "Generate right and up anchors from the snapped south. For mirrored projects, left is a "
            "horizontal flip of right — don't generate it separately. For independent-side projects, "
            "generate left separately.",
        ).replace(
            "per direction (W, N)",
            "per generated direction (right, up; add left for independent-side projects)",
        ).replace(
            "`west` / `north`", "`right` / `up`; `left` for independent-side projects"
        ).replace(
            "The W canonical reference used by every action in the west direction:",
            "The right canonical reference used by every action in the right direction:",
        ).replace(
            "![W canonical reference]",
            "![Right canonical reference]",
        ).replace(
            "![Directional anchors NSEW]",
            "![Directional anchors (down/right/up with policy-specific left)]",
        ).replace(
            "facing screen-left",
            "facing screen-right",
        )
        return source, ("direction-policy-right-up-left",)
    if stage_key == "frame_recovery":
        source = source.replace(
            "Feed these to file 06 (per-frame chroma-layout snap).",
            "Feed these to file 07 (per-frame chroma-layout snap).",
        )
        return source, ("frame-recovery-next-stage",)
    return source, ()


def _locked_context(
    project: ProjectConfig, *, direction: str | None, state: StateSpec | str | None
) -> dict[str, Any]:
    if direction is not None and direction not in project.directions:
        raise PromptError("direction is not configured for this project")
    state_spec = _project_state(project, state)
    return {
        "chroma": project.chroma,
        "palette_size": project.palette_size,
        "cell": {
            "width": project.cell.width,
            "height": project.cell.height,
            "logical_size": project.cell.logical_size,
            "anchor": {"x": project.cell.anchor_x, "y": project.cell.anchor_y},
        },
        "direction": direction,
        "state": (
            None
            if state_spec is None
            else {
                "id": state_spec.id,
                "label": state_spec.label,
                "frames": state_spec.frames,
                "fps": state_spec.fps,
                "loop": state_spec.loop,
                "generator": state_spec.generator,
                "motion": state_spec.motion,
            }
        ),
    }


def _project_state(project: ProjectConfig, state: StateSpec | str | None) -> StateSpec | None:
    if state is None:
        return None
    state_id = state.id if isinstance(state, StateSpec) else state
    if not isinstance(state_id, str):
        raise PromptError("state must be a configured state ID")
    for candidate in project.states:
        if candidate.id == state_id:
            return candidate
    raise PromptError("state is not configured for this project")


def _artifact_inputs(
    project: ProjectConfig, artifact_inputs: Iterable[ArtifactRef | Mapping[str, object]]
) -> list[dict[str, str]]:
    known = {artifact.id: artifact for artifact in project.artifacts}
    pairs: list[dict[str, str]] = []
    seen: set[UUID] = set()
    try:
        inputs = tuple(artifact_inputs)
    except TypeError as exc:
        raise PromptError("artifact inputs must be iterable") from exc
    for item in inputs:
        if isinstance(item, ArtifactRef):
            artifact_id, artifact_hash = item.id, item.sha256
        elif isinstance(item, Mapping):
            if set(item) != {"id", "sha256"}:
                raise PromptError("artifact input must contain only id and sha256")
            try:
                artifact_id = UUID(str(item["id"]))
            except (TypeError, ValueError, AttributeError) as exc:
                raise PromptError("invalid artifact input ID") from exc
            artifact_hash = item["sha256"]
        else:
            raise PromptError("invalid artifact input")
        if not isinstance(artifact_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
            raise PromptError("invalid artifact input hash")
        artifact = known.get(artifact_id)
        if artifact is None:
            raise PromptError("unknown artifact input")
        if artifact.stale:
            raise PromptError("stale artifact input")
        if artifact.sha256 != artifact_hash:
            raise PromptError("artifact input hash does not match project")
        if artifact_id in seen:
            raise PromptError("duplicate artifact input")
        seen.add(artifact_id)
        pairs.append({"id": str(artifact_id), "sha256": artifact_hash})
    return sorted(pairs, key=lambda pair: pair["id"])


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
