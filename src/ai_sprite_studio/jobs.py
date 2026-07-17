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
        self._runner_lock_fd: int | None = None
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
        await self._persist(job)
        self._projects[job.id] = project.id
        self._cancellations[job.id] = asyncio.Event()
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())
        await self._queue.put(job.id)
        return job

    async def start(self) -> None:
        if self._started:
            return
        self._runner_lock_fd = self.store.acquire_runner_lock()
        try:
            for project in self.store.list_projects():
                for job in self.store.list_jobs(project.id):
                    self._projects[job.id] = project.id
                    self._cancellations[job.id] = asyncio.Event()
                    if job.status in {"running", "cancel_requested"}:
                        attention = job.model_copy(
                            update={
                                "status": "attention_required",
                                "error": ApiError(
                                    code="interrupted_job",
                                    message="Job needs attention after an interrupted run",
                                ),
                            }
                        )
                        await self._persist(attention)
                    else:
                        await self._reconcile_event(job)
                    if job.status == "queued":
                        await self._queue.put(job.id)
            self._worker_task = asyncio.create_task(self._worker())
            self._started = True
        except BaseException:
            self.store.release_runner_lock(self._runner_lock_fd)
            self._runner_lock_fd = None
            raise

    async def wait_idle(self) -> None:
        await self._queue.join()

    async def cancel(self, job_id: UUID | str) -> JobRecord:
        job = await self.get(job_id)
        if job.status == "queued":
            self._cancellations[job.id].set()
            canceled = job.model_copy(update={"status": "canceled"})
            return await self._persist(canceled)
        if job.status != "running":
            return job
        self._cancellations[job.id].set()
        requested = job.model_copy(update={"status": "cancel_requested"})
        return await self._persist(requested)

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
            current = self.store.load_job(job.project_id, job.id)
            await self._reconcile_event(current)
            events = self.store.job_events(job.project_id, after_id=last_id)
            for event in events:
                last_id = event["id"]
                if self._is_job_event(event, job.id):
                    yield event
            current = self.store.load_job(job.project_id, job.id)
            if current.status in {"succeeded", "failed", "canceled", "attention_required"}:
                return
            async with self._events_changed:
                await self._events_changed.wait_for(
                    lambda: any(
                        self._is_job_event(event, job.id)
                        for event in self.store.job_events(job.project_id, after_id=last_id)
                    )
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
        return await self._persist(updated)

    async def aclose(self) -> None:
        try:
            if self._worker_task is not None:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
                self._worker_task = None
        finally:
            if self._runner_lock_fd is not None:
                self.store.release_runner_lock(self._runner_lock_fd)
                self._runner_lock_fd = None
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
                await self._persist(running)
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
                    await self._persist(attention)
                    continue
                await self.handler(running, self._cancellations[job_id])
                current = self.store.load_job(project_id, job_id)
                if current.status == "cancel_requested":
                    finished = current.model_copy(update={"status": "canceled"})
                else:
                    finished = current.model_copy(update={"status": "succeeded", "progress": 1})
                await self._persist(finished)
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
                await self._persist(failed)
            finally:
                self._queue.task_done()

    async def _persist(self, job: JobRecord) -> JobRecord:
        saved = self.store.save_job(job)
        try:
            await self._emit(saved)
        except ProjectStoreError:
            # State is already durable.  A later start or stream will repair
            # the missing event rather than changing this job's outcome.
            await self._notify_events_changed()
        return saved

    async def _reconcile_event(self, job: JobRecord) -> None:
        expected = job.model_dump(mode="json")
        latest = next(
            (
                event
                for event in reversed(self.store.job_events(job.project_id))
                if self._is_job_event(event, job.id)
            ),
            None,
        )
        if latest is not None and latest["data"] == expected:
            return
        try:
            await self._emit(job)
        except ProjectStoreError:
            await self._notify_events_changed()

    async def _emit(self, job: JobRecord) -> None:
        self.store.append_job_event(job.project_id, "job", job.model_dump(mode="json"))
        await self._notify_events_changed()

    async def _notify_events_changed(self) -> None:
        async with self._events_changed:
            self._events_changed.notify_all()

    @staticmethod
    def _is_job_event(event: dict[str, Any], job_id: UUID) -> bool:
        data = event.get("data")
        return event.get("event") == "job" and isinstance(data, dict) and data.get("id") == str(job_id)

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
