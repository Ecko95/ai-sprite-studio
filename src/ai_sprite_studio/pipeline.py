"""Provider-free artifact dependencies, approval gates, and request preflight."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from .contracts import Approval, ArtifactRef, ArtifactVariant, ProjectConfig, StateSpec
from .prompts import STAGES
from .project_store import ProjectStore, ProjectStoreError


class PipelineError(ProjectStoreError):
    """A pipeline dependency or approval policy is invalid."""


@dataclass(frozen=True)
class RequestLine:
    """One transparent preflight request count."""

    stage: str
    request_type: str
    count: int
    directions: tuple[str, ...] = ()
    state_ids: tuple[str, ...] = ()
    available: bool = True
    unavailable: int = 0


@dataclass(frozen=True)
class RequestPreflight:
    """Pure request-count preflight with no job or provider side effects."""

    lines: tuple[RequestLine, ...]

    @property
    def total(self) -> int:
        return sum(line.count for line in self.lines)


_STAGE_INPUTS: dict[str, tuple[frozenset[str], ...]] = {
    "base_generation": (frozenset({"input"}),),
    "base_snap": (frozenset({"base_generation"}),),
    "directional_anchors": (frozenset({"base_snap"}),),
    "action_poseboards": (frozenset({"directional_anchors"}),),
    "frame_recovery": (frozenset({"action_poseboards"}),),
    "walk_i2v": (frozenset({"directional_anchors"}),),
    "per_frame_snap": (frozenset({"frame_recovery"}), frozenset({"walk_i2v"})),
    "runtime_normalize": (frozenset({"per_frame_snap"}),),
}
_GATE_KINDS: dict[str, frozenset[str]] = {
    "base": frozenset({"base_snap"}),
    "directions": frozenset({"directional_anchors"}),
    "frames": frozenset({"frame_recovery"}),
    "motion": frozenset({"curation"}),
    "export": frozenset({"qa", "atlas"}),
}
_GATE_PREDECESSORS = {
    "base": (),
    "directions": ("base",),
    "frames": ("directions",),
    "motion": ("frames",),
    "export": ("motion",),
}


def put_stage_artifact(
    store: ProjectStore,
    project_id: UUID | str,
    stage_key: str,
    data: bytes,
    *,
    media_type: str,
    dependencies: Iterable[UUID | str],
    variant: ArtifactVariant = "raw",
    width: int | None = None,
    height: int | None = None,
    source_job_id: UUID | str | None = None,
) -> ArtifactRef:
    """Persist one stage output after validating its live direct inputs."""

    project = store.load(project_id)
    dependency_ids = _input_ids(dependencies)
    validate_stage_inputs(project, stage_key, dependency_ids)
    return store.put_artifact(
        project.id,
        data,
        kind=stage_key,
        media_type=media_type,
        variant=variant,
        stage=stage_key,
        width=width,
        height=height,
        source_job_id=source_job_id,
        dependencies=dependency_ids,
    )


def validate_stage_inputs(
    project: ProjectConfig, stage_key: str, dependencies: Iterable[UUID | str]
) -> tuple[ArtifactRef, ...]:
    """Validate the direct, live artifact inputs for a canonical stage."""

    if not isinstance(project, ProjectConfig):
        raise PipelineError("project must be a ProjectConfig")
    if stage_key not in STAGES:
        raise PipelineError("unknown pipeline stage")
    dependency_ids = _input_ids(dependencies)
    artifacts = {artifact.id: artifact for artifact in project.artifacts}
    inputs: list[ArtifactRef] = []
    for artifact_id in dependency_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise PipelineError("unknown input artifact")
        if artifact.stale:
            raise PipelineError("stale input artifact")
        inputs.append(artifact)

    input_kinds = frozenset(artifact.kind for artifact in inputs)
    if input_kinds not in _STAGE_INPUTS[stage_key]:
        raise PipelineError("stage has wrong or missing direct input dependencies")
    return tuple(inputs)


def approve_gate(
    store: ProjectStore,
    project_id: UUID | str,
    gate: str,
    artifact_ids: Iterable[UUID | str],
    *,
    note: str = "",
) -> Approval:
    """Approve live stage outputs only after their predecessor gate is live."""

    project = store.load(project_id)
    if gate not in _GATE_KINDS:
        raise PipelineError("unknown approval gate")
    if not isinstance(note, str):
        raise PipelineError("approval note must be text")
    selected_ids = _input_ids(artifact_ids)
    if not selected_ids:
        raise PipelineError("approval requires at least one artifact")
    artifacts = {artifact.id: artifact for artifact in project.artifacts}
    for artifact_id in selected_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise PipelineError("unknown approval artifact")
        if artifact.stale:
            raise PipelineError("cannot approve a stale artifact")
        if artifact.kind not in _GATE_KINDS[gate]:
            raise PipelineError("approval artifact has the wrong stage")
    for predecessor in _GATE_PREDECESSORS[gate]:
        approval = _stored_approval(project, predecessor)
        if approval is None:
            raise PipelineError(f"{predecessor} approval is required")
        _validate_stored_approval(project, approval)
    previous = _stored_approval(project, gate)
    if previous is not None:
        _validate_approval_hashes(project, previous, require_live=False)
        if previous.artifact_ids != selected_ids:
            stale_ids = _descendant_ids(project, previous.artifact_ids)
            if stale_ids.intersection(selected_ids):
                raise PipelineError("replacement approval depends on its prior selection")
            store.invalidate_dependants(project.id, previous.artifact_ids)
    return store.approve(project.id, gate, selected_ids, note=note)


def preflight_requests(
    project: ProjectConfig,
    *,
    state_ids: Iterable[str] | None = None,
    directions: Iterable[str] | None = None,
    include_base: bool = True,
    motion_available: bool = True,
) -> RequestPreflight:
    """Count selected image and motion requests without enqueuing work."""

    if not isinstance(project, ProjectConfig):
        raise PipelineError("project must be a ProjectConfig")
    if not isinstance(include_base, bool) or not isinstance(motion_available, bool):
        raise PipelineError("preflight flags must be booleans")
    selected_states = _selected_states(project, state_ids)
    selected_directions = _selected_directions(project, directions)
    generated_directions = _generated_directions(project, selected_directions)
    anchor_directions = tuple(direction for direction in generated_directions if direction != "down")
    poseboard_states = tuple(
        state.id for state in selected_states if state.generator == "gpt_poseboard"
    )
    motion_states = tuple(
        state.id for state in selected_states if state.generator == "higgsfield_i2v"
    )
    upload_states = tuple(state.id for state in selected_states if state.generator == "upload")
    poseboard_count = len(poseboard_states) * len(generated_directions)
    motion_count = len(motion_states) * len(generated_directions)
    return RequestPreflight(
        lines=(
            RequestLine("base_generation", "image", int(include_base)),
            RequestLine(
                "directional_anchors",
                "image",
                len(anchor_directions),
                directions=anchor_directions,
            ),
            RequestLine(
                "action_poseboards",
                "image",
                poseboard_count,
                directions=generated_directions,
                state_ids=poseboard_states,
            ),
            RequestLine(
                "walk_i2v",
                "motion",
                motion_count if motion_available else 0,
                directions=generated_directions,
                state_ids=motion_states,
                available=motion_available,
                unavailable=motion_count if not motion_available else 0,
            ),
            RequestLine("upload", "local", 0, state_ids=upload_states),
            RequestLine("base_snap", "local", 0),
            RequestLine("frame_recovery", "local", 0),
            RequestLine("per_frame_snap", "local", 0),
            RequestLine("runtime_normalize", "local", 0),
        )
    )


def _stored_approval(project: ProjectConfig, gate: str) -> Approval | None:
    return next((approval for approval in project.approvals if approval.gate == gate), None)


def _validate_stored_approval(project: ProjectConfig, approval: Approval) -> None:
    _validate_approval_hashes(project, approval, require_live=True)


def _validate_approval_hashes(
    project: ProjectConfig, approval: Approval, *, require_live: bool
) -> None:
    artifacts = {artifact.id: artifact for artifact in project.artifacts}
    current_hashes: list[str] = []
    for artifact_id in approval.artifact_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None:
            raise PipelineError(f"{approval.gate} approval has an unknown artifact")
        if require_live and artifact.stale:
            raise PipelineError(f"{approval.gate} approval is stale")
        current_hashes.append(artifact.sha256)
    if approval.artifact_hashes != current_hashes:
        raise PipelineError(f"{approval.gate} approval hash does not match its artifact")


def _descendant_ids(project: ProjectConfig, roots: Iterable[UUID]) -> set[UUID]:
    changed = set(roots)
    invalidated: set[UUID] = set()
    frontier = set(changed)
    while frontier:
        descendants = {
            artifact.id
            for artifact in project.artifacts
            if artifact.id not in changed | invalidated
            and any(dependency in frontier for dependency in artifact.dependencies)
        }
        invalidated.update(descendants)
        frontier = descendants
    return invalidated


def _selected_states(
    project: ProjectConfig, state_ids: Iterable[str] | None
) -> tuple[StateSpec, ...]:
    selected_ids = _selection(state_ids, (state.id for state in project.states), "state")
    states = {state.id: state for state in project.states}
    return tuple(states[state_id] for state_id in selected_ids)


def _selected_directions(
    project: ProjectConfig, directions: Iterable[str] | None
) -> tuple[str, ...]:
    return _selection(directions, project.directions, "direction")


def _selection(
    values: Iterable[str] | None, allowed: Iterable[str], label: str
) -> tuple[str, ...]:
    allowed_values = tuple(allowed)
    if values is None:
        return allowed_values
    try:
        selected = tuple(values)
    except TypeError as exc:
        raise PipelineError(f"{label} selection must be iterable") from exc
    if any(not isinstance(value, str) for value in selected):
        raise PipelineError(f"invalid {label} selection")
    if len(selected) != len(set(selected)):
        raise PipelineError(f"duplicate {label} selection")
    if any(value not in allowed_values for value in selected):
        raise PipelineError(f"unknown {label} selection")
    return selected


def _generated_directions(project: ProjectConfig, selected: tuple[str, ...]) -> tuple[str, ...]:
    if project.side_policy == "independent":
        return selected
    generated: list[str] = []
    if "down" in selected:
        generated.append("down")
    if "right" in selected or "left" in selected:
        generated.append("right")
    if "up" in selected:
        generated.append("up")
    return tuple(generated)


def _input_ids(dependencies: Iterable[UUID | str]) -> list[UUID]:
    try:
        values = list(dependencies)
    except TypeError as exc:
        raise PipelineError("dependencies must be iterable") from exc
    try:
        ids = [item if isinstance(item, UUID) else UUID(str(item)) for item in values]
    except (TypeError, ValueError, AttributeError) as exc:
        raise PipelineError("invalid input artifact ID") from exc
    if len(ids) != len(set(ids)):
        raise PipelineError("duplicate input artifact ID")
    return ids
