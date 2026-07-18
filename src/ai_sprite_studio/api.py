"""Typed local API routes."""

from pydantic import BaseModel, ConfigDict, ValidationError
import json
from starlette.responses import JSONResponse, StreamingResponse
from typing import Any
from uuid import UUID

from .contracts import JobCommand, ProjectConfig
from .project_store import ProjectStoreError


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Untitled sprite"


class PatchProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: JobCommand
    payload: dict[str, Any]


def api_error(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"code": code, "message": message, "retryable": False, "details": {}},
        status_code=status_code,
    )


def _job_id(request) -> UUID | None:
    try:
        return UUID(request.path_params["job_id"])
    except (KeyError, TypeError, ValueError):
        return None


async def list_projects(request):
    projects = request.app.state.store.list_projects()
    return JSONResponse({"projects": [project.model_dump(mode="json") for project in projects]})


async def create_project(request):
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError
        payload = CreateProjectRequest.model_validate(raw)
        project = request.app.state.store.create(ProjectConfig(name=payload.name))
    except (ValueError, ValidationError, ProjectStoreError):
        return api_error("invalid_request", "The request is invalid", status_code=422)
    return JSONResponse(project.model_dump(mode="json"), status_code=201)


async def projects(request):
    if request.method == "GET":
        return await list_projects(request)
    return await create_project(request)


async def project_detail(request):
    try:
        project_id = UUID(request.path_params["project_id"])
    except (KeyError, TypeError, ValueError):
        return api_error("invalid_project_id", "The project ID is invalid", status_code=422)
    try:
        project = request.app.state.store.load(project_id)
    except ProjectStoreError:
        return api_error("unknown_project", "The project was not found", status_code=404)
    if request.method == "GET":
        return JSONResponse(project.model_dump(mode="json"))
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError
        payload = PatchProjectRequest.model_validate(raw)
        if payload.name is None:
            raise ValueError
        project = request.app.state.store.save(project.model_copy(update={"name": payload.name}))
    except (ValueError, ValidationError, ProjectStoreError):
        return api_error("invalid_request", "The request is invalid", status_code=422)
    return JSONResponse(project.model_dump(mode="json"))


async def create_job(request):
    try:
        project_id = UUID(request.path_params["project_id"])
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError
        payload = CreateJobRequest.model_validate(raw)
        job = await request.app.state.runner.enqueue(project_id, payload.command, payload.payload)
    except (KeyError, TypeError, ValueError, ValidationError):
        return api_error("invalid_request", "The request is invalid", status_code=422)
    except ProjectStoreError:
        return api_error("unknown_project", "The project was not found", status_code=404)
    return JSONResponse(job.model_dump(mode="json"), status_code=201)


async def get_job(request):
    job_id = _job_id(request)
    if job_id is None:
        return api_error("invalid_job_id", "The job ID is invalid", status_code=422)
    try:
        job = await request.app.state.runner.get(job_id)
    except ProjectStoreError:
        return api_error("unknown_job", "The job was not found", status_code=404)
    return JSONResponse(job.model_dump(mode="json"))


async def job_events(request):
    try:
        after_id = int(request.headers.get("last-event-id", "0"))
        if after_id < 0:
            raise ValueError
    except ValueError:
        return api_error("invalid_event_id", "The event ID is invalid", status_code=422)
    job_id = _job_id(request)
    if job_id is None:
        return api_error("invalid_job_id", "The job ID is invalid", status_code=422)
    try:
        job = await request.app.state.runner.get(job_id)
    except ProjectStoreError:
        return api_error("unknown_job", "The job was not found", status_code=404)

    async def stream():
        async for event in request.app.state.runner.event_stream(job.id, after_id=after_id):
            yield (
                f"id: {event['id']}\nevent: {event['event']}\ndata: "
                f"{json.dumps(event['data'], ensure_ascii=False, separators=(',', ':'))}\n\n"
            )

    return StreamingResponse(stream(), media_type="text/event-stream")


async def cancel_job(request):
    job_id = _job_id(request)
    if job_id is None:
        return api_error("invalid_job_id", "The job ID is invalid", status_code=422)
    try:
        job = await request.app.state.runner.cancel(job_id)
    except ProjectStoreError:
        return api_error("unknown_job", "The job was not found", status_code=404)
    return JSONResponse(job.model_dump(mode="json"))
