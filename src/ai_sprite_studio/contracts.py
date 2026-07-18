"""Validated public contracts for local sprite projects."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Direction = Literal["down", "right", "up", "left"]
SidePolicy = Literal["mirror", "independent"]
GeneratorKind = Literal["gpt_poseboard", "higgsfield_i2v", "upload"]
ArtifactVariant = Literal["raw", "plain", "pixel", "chroma", "preview", "video"]
ApprovalGate = Literal["base", "directions", "frames", "motion", "export"]
JobStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancel_requested",
    "canceled",
    "attention_required",
]


class JobCommand(StrEnum):
    NORMALIZE_INPUT = "normalize_input"
    GENERATE_BASE = "generate_base"
    EDIT_CANDIDATE = "edit_candidate"
    SNAP_CANDIDATE = "snap_candidate"
    GENERATE_DIRECTIONS = "generate_directions"
    GENERATE_POSEBOARDS = "generate_poseboards"
    GENERATE_WALK = "generate_walk"
    INGEST_WALK_VIDEO = "ingest_walk_video"
    RECOVER_FRAMES = "recover_frames"
    SAVE_CURATION = "save_curation"
    AI_EDIT_FRAME = "ai_edit_frame"
    RUN_QA = "run_qa"
    EXPORT_PACK = "export_pack"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


CORE_STATE_PRESETS: dict[str, tuple[int, int, bool, GeneratorKind]] = {
    "idle": (4, 6, True, "gpt_poseboard"),
    "walk": (8, 10, True, "higgsfield_i2v"),
    "attack": (4, 12, False, "gpt_poseboard"),
    "hurt": (4, 10, False, "gpt_poseboard"),
    "jump": (4, 10, False, "gpt_poseboard"),
    "death": (6, 8, False, "gpt_poseboard"),
}


class StateSpec(ContractModel):
    id: str
    label: str
    frames: int = Field(ge=1, le=128)
    fps: int = Field(ge=1, le=30)
    loop: bool
    generator: GeneratorKind
    motion: str

    @field_validator("id", "label", "motion")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _keep_core_state_presets_locked(self) -> StateSpec:
        preset = CORE_STATE_PRESETS.get(self.id)
        if preset and (self.frames, self.fps, self.loop, self.generator) != preset:
            raise ValueError(f"{self.id} uses its locked core-state preset")
        return self


class CellSpec(ContractModel):
    width: Literal[256] = 256
    height: Literal[256] = 256
    logical_size: Literal[64, 128, 256] = 128
    anchor_x: Literal[128] = 128
    anchor_y: Literal[255] = 255


def _core_states() -> list[StateSpec]:
    return [
        StateSpec(
            id="idle",
            label="Idle",
            frames=4,
            fps=6,
            loop=True,
            generator="gpt_poseboard",
            motion="breathing in place",
        ),
        StateSpec(
            id="walk",
            label="Walk",
            frames=8,
            fps=10,
            loop=True,
            generator="higgsfield_i2v",
            motion="walking in place",
        ),
        StateSpec(
            id="attack",
            label="Attack",
            frames=4,
            fps=12,
            loop=False,
            generator="gpt_poseboard",
            motion="a quick attack",
        ),
        StateSpec(
            id="hurt",
            label="Hurt",
            frames=4,
            fps=10,
            loop=False,
            generator="gpt_poseboard",
            motion="a brief recoil",
        ),
        StateSpec(
            id="jump",
            label="Jump",
            frames=4,
            fps=10,
            loop=False,
            generator="gpt_poseboard",
            motion="a short jump",
        ),
        StateSpec(
            id="death",
            label="Death",
            frames=6,
            fps=8,
            loop=False,
            generator="gpt_poseboard",
            motion="a fall to the ground",
        ),
    ]


def _valid_relative_path(value: str) -> str:
    if not value or value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("must be a non-empty safe relative path")
    windows_path = PureWindowsPath(value)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("must be a non-empty safe relative path")
    return value


def _valid_sha256(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


class ArtifactRef(ContractModel):
    id: UUID
    kind: str
    relative_path: str
    sha256: str
    media_type: str
    width: int | None
    height: int | None
    variant: ArtifactVariant
    source_job_id: UUID | None
    dependencies: list[UUID]
    stale: bool = False

    @field_validator("kind", "media_type")
    @classmethod
    def _require_nonempty_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        return _valid_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _valid_sha256(value)

    @field_validator("dependencies")
    @classmethod
    def _unique_dependencies(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("must not contain duplicate artifact IDs")
        return value


class Approval(ContractModel):
    gate: ApprovalGate
    artifact_ids: list[UUID] = Field(min_length=1)
    artifact_hashes: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    note: str = ""

    @field_validator("artifact_ids")
    @classmethod
    def _unique_artifact_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("must not contain duplicate artifact IDs")
        return value

    @field_validator("artifact_hashes")
    @classmethod
    def _validate_hashes(cls, value: list[str]) -> list[str]:
        return [_valid_sha256(item) for item in value]

    @model_validator(mode="after")
    def _match_artifacts_to_hashes(self) -> Approval:
        if len(self.artifact_ids) != len(self.artifact_hashes):
            raise ValueError("artifact_ids and artifact_hashes must have the same length")
        return self


class ApiError(ContractModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", "message")
    @classmethod
    def _require_error_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class JobRecord(ContractModel):
    id: UUID
    project_id: UUID
    command: JobCommand
    status: JobStatus
    payload: dict[str, Any]
    input_hash: str
    provider_request_ids: list[str]
    output_artifact_ids: list[UUID]
    progress: float = Field(ge=0, le=1)
    error: ApiError | None

    @field_validator("input_hash")
    @classmethod
    def _validate_input_hash(cls, value: str) -> str:
        return _valid_sha256(value)


class ProjectConfig(ContractModel):
    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=uuid4)
    name: str = "Untitled sprite"
    view: Literal["top_down_four_way"] = "top_down_four_way"
    directions: list[Direction] = Field(default_factory=lambda: ["down", "right", "up", "left"])
    side_policy: SidePolicy = "mirror"
    cell: CellSpec = Field(default_factory=CellSpec)
    palette_size: Literal[12] = 12
    binary_alpha: Literal[True] = True
    chroma: str = "#00FF00"
    candidates_per_request: Literal[1] = 1
    states: list[StateSpec] = Field(default_factory=_core_states)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("chroma")
    @classmethod
    def _validate_chroma(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            raise ValueError("must be a six-digit hex color")
        return value.upper()

    @field_validator("directions")
    @classmethod
    def _keep_four_way_order(cls, value: list[Direction]) -> list[Direction]:
        if value != ["down", "right", "up", "left"]:
            raise ValueError("must remain top-down four-way in down/right/up/left order")
        return value

    @model_validator(mode="after")
    def _keep_project_lists_consistent(self) -> ProjectConfig:
        state_ids = [state.id for state in self.states]
        artifact_ids = [artifact.id for artifact in self.artifacts]
        artifact_id_set = set(artifact_ids)
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("state IDs must be unique")
        if not set(CORE_STATE_PRESETS).issubset(state_ids):
            raise ValueError("projects must include all core states")
        if len(artifact_ids) != len(artifact_id_set):
            raise ValueError("artifact IDs must be unique")
        if any(
            dependency not in artifact_id_set
            for artifact in self.artifacts
            for dependency in artifact.dependencies
        ):
            raise ValueError("artifact dependencies contain an unknown artifact")
        return self
