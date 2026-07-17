"""Serialized, durable local job execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from hashlib import sha256
import json
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from .contracts import ApiError, JobCommand, JobRecord
from .project_store import ProjectStore, ProjectStoreError


type JobHandler = Callable[[JobRecord, asyncio.Event], Awaitable[None]]


class JobRunner:
    def __init__(self, store: ProjectStore, handler: JobHandler | None = None):
        self.store = store
        self.handler = handler
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._projects: dict[UUID, UUID] = {}
        self._cancellations: dict[UUID, asyncio.Event] = {}
        self._events_changed = asyncio.Condition()
        self._worker_task: asyncio.Task[None] | None = None
        self._started = False

    async def enqueue(
        self, project_id: UUID | str, command: JobCommand | str, payload: dict[str, Any]
    ) -> JobRecord:
        if not isinstance(payload, dict):
            raise ValueError("job payload must be an object")
        await self.start()
        project = self.store.load(project_id)
        command = JobCommand(command)
        input_hash = self._input_hash(command, payload)
        for existing in self.store.list_jobs(project.id):
            if (
                existing.command == command
                and existing.input_hash == input_hash
                and existing.status == "succeeded"
            ):
                return existing
        job = JobRecord(
            id=uuid4(),
            project_id=project.id,
            command=command,
            status="queued",
            payload=payload,
            input_hash=input_hash,
            provider_request_ids=[],
            output_artifact_ids=[],
            progress=0,
            error=None,
        )
        self.store.save_job(job)
        await self._emit(job)
        self._projects[job.id] = project.id
        self._cancellations[job.id] = asyncio.Event()
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())
        await self._queue.put(job.id)
        return job

    async def start(self) -> None:
        if self._started:
            return
        for project in self.store.list_projects():
            for job in self.store.list_jobs(project.id):
                self._projects[job.id] = project.id
                self._cancellations[job.id] = asyncio.Event()
                if job.status == "queued":
                    await self._queue.put(job.id)
                elif job.status in {"running", "cancel_requested"}:
                    attention = job.model_copy(
                        update={
                            "status": "attention_required",
                            "error": ApiError(
                                code="interrupted_job",
                                message="Job needs attention after an interrupted run",
                            ),
                        }
                    )
                    self.store.save_job(attention)
                    await self._emit(attention)
        self._worker_task = asyncio.create_task(self._worker())
        self._started = True

    async def wait_idle(self) -> None:
        await self._queue.join()

    async def cancel(self, job_id: UUID | str) -> JobRecord:
        job = await self.get(job_id)
        if job.status == "queued":
            self._cancellations[job.id].set()
            canceled = job.model_copy(update={"status": "canceled"})
            self.store.save_job(canceled)
            await self._emit(canceled)
            return canceled
        if job.status != "running":
            return job
        self._cancellations[job.id].set()
        requested = job.model_copy(update={"status": "cancel_requested"})
        self.store.save_job(requested)
        await self._emit(requested)
        return requested

    async def get(self, job_id: UUID | str) -> JobRecord:
        try:
            job_id = UUID(str(job_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProjectStoreError("invalid job ID") from exc
        project_id = self._projects.get(job_id)
        if project_id is not None:
            return self.store.load_job(project_id, job_id)
        for project in self.store.list_projects():
            for job in self.store.list_jobs(project.id):
                if job.id == job_id:
                    self._projects[job.id] = project.id
                    self._cancellations.setdefault(job.id, asyncio.Event())
                    return job
        raise ProjectStoreError("unknown job")

    async def event_stream(
        self, job_id: UUID | str, *, after_id: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        job = await self.get(job_id)
        last_id = after_id
        while True:
            events = self.store.job_events(job.project_id, after_id=last_id)
            for event in events:
                yield event
                last_id = event["id"]
            current = self.store.load_job(job.project_id, job.id)
            if current.status in {"succeeded", "failed", "canceled", "attention_required"}:
                return
            async with self._events_changed:
                await self._events_changed.wait_for(
                    lambda: bool(self.store.job_events(job.project_id, after_id=last_id))
                    or self.store.load_job(job.project_id, job.id).status
                    in {"succeeded", "failed", "canceled", "attention_required"}
                )

    async def update(
        self,
        job_id: UUID | str,
        *,
        progress: float | None = None,
        output_artifact_ids: list[UUID] | None = None,
    ) -> JobRecord:
        job_id = UUID(str(job_id))
        project_id = self._projects[job_id]
        current = self.store.load_job(project_id, job_id)
        raw = current.model_dump(mode="python")
        if progress is not None:
            raw["progress"] = progress
        if output_artifact_ids is not None:
            raw["output_artifact_ids"] = output_artifact_ids
        updated = JobRecord.model_validate(raw)
        self.store.save_job(updated)
        await self._emit(updated)
        return updated

    async def aclose(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None
        self._started = False

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                project_id = self._projects[job_id]
                job = self.store.load_job(project_id, job_id)
                if job.status != "queued":
                    continue
                running = job.model_copy(update={"status": "running"})
                self.store.save_job(running)
                await self._emit(running)
                if self.handler is None:
                    attention = running.model_copy(
                        update={
                            "status": "attention_required",
                            "error": ApiError(
                                code="no_job_handler",
                                message="No local handler is configured for this job",
                            ),
                        }
                    )
                    self.store.save_job(attention)
                    await self._emit(attention)
                    continue
                await self.handler(running, self._cancellations[job_id])
                current = self.store.load_job(project_id, job_id)
                if current.status == "cancel_requested":
                    finished = current.model_copy(update={"status": "canceled"})
                else:
                    finished = current.model_copy(update={"status": "succeeded", "progress": 1})
                self.store.save_job(finished)
                await self._emit(finished)
            except asyncio.CancelledError:
                raise
            except Exception:
                current = self.store.load_job(project_id, job_id)
                if current.status == "cancel_requested":
                    failed = current.model_copy(update={"status": "canceled"})
                else:
                    failed = current.model_copy(
                        update={
                            "status": "failed",
                            "error": ApiError(
                                code="job_failed", message="Job execution failed"
                            ),
                        }
                )
                self.store.save_job(failed)
                await self._emit(failed)
            finally:
                self._queue.task_done()

    async def _emit(self, job: JobRecord) -> None:
        self.store.append_job_event(job.project_id, "job", job.model_dump(mode="json"))
        async with self._events_changed:
            self._events_changed.notify_all()

    @staticmethod
    def _input_hash(command: JobCommand, payload: dict[str, Any]) -> str:
        data = json.dumps(
            {"command": command.value, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(data).hexdigest()
