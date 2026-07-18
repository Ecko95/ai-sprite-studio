import asyncio
from contextlib import suppress
import subprocess
import sys
from uuid import uuid4

import pytest

from ai_sprite_studio.contracts import JobCommand, JobRecord, ProjectConfig
from ai_sprite_studio.jobs import JobRunner
from ai_sprite_studio.project_store import ProjectStore, ProjectStoreError


@pytest.mark.asyncio
async def test_runner_persists_queued_event_before_executing_a_job(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Queued ranger"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(job, _cancel_requested):
        assert store.load_job(project.id, job.id).status == "running"
        entered.set()
        await release.wait()

    runner = JobRunner(store, handler)
    try:
        job = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "idle"})

        await asyncio.wait_for(entered.wait(), timeout=1)

        assert [event["data"]["status"] for event in store.job_events(project.id)] == [
            "queued",
            "running",
        ]

        release.set()
        await runner.wait_idle()
        assert store.load_job(project.id, job.id).status == "succeeded"
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_cancels_an_active_job_cooperatively(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Cancelable ranger"))
    entered = asyncio.Event()

    async def handler(_job, cancel_requested):
        entered.set()
        await cancel_requested.wait()

    runner = JobRunner(store, handler)
    try:
        job = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "walk"})
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert (await runner.cancel(job.id)).status == "cancel_requested"

        await runner.wait_idle()
        assert store.load_job(project.id, job.id).status == "canceled"
        assert [event["data"]["status"] for event in store.job_events(project.id)] == [
            "queued",
            "running",
            "cancel_requested",
            "canceled",
        ]
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_restart_recovers_queued_jobs_and_marks_interrupted_jobs_for_attention(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Recovered ranger"))
    queued = JobRecord(
        id=uuid4(),
        project_id=project.id,
        command=JobCommand.RUN_QA,
        status="queued",
        payload={"state": "idle"},
        input_hash="a" * 64,
        provider_request_ids=[],
        output_artifact_ids=[],
        progress=0,
        error=None,
    )
    interrupted = queued.model_copy(
        update={"id": uuid4(), "status": "running", "input_hash": "b" * 64}
    )
    store.save_job(queued)
    store.save_job(interrupted)

    async def handler(_job, _cancel_requested):
        return None

    runner = JobRunner(store, handler)
    try:
        await runner.start()
        await runner.wait_idle()

        assert store.load_job(project.id, queued.id).status == "succeeded"
        restored = store.load_job(project.id, interrupted.id)
        assert restored.status == "attention_required"
        assert restored.error and restored.error.code == "interrupted_job"
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_reuses_a_completed_job_for_the_same_canonical_input(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Idempotent ranger"))
    executions = []

    async def handler(job, _cancel_requested):
        executions.append(job.id)

    runner = JobRunner(store, handler)
    try:
        first = await runner.enqueue(
            project.id, JobCommand.RUN_QA, {"state": "idle", "options": {"a": 1, "b": 2}}
        )
        await runner.wait_idle()

        reused = await runner.enqueue(
            project.id, JobCommand.RUN_QA, {"options": {"b": 2, "a": 1}, "state": "idle"}
        )
        await runner.wait_idle()

        assert reused.id == first.id
        assert executions == [first.id]
        assert [job.id for job in store.list_jobs(project.id)] == [first.id]
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_executes_only_one_handler_at_a_time(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Serialized ranger"))
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0

    async def handler(_job, _cancel_requested):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        started.set()
        await release.wait()
        active -= 1

    runner = JobRunner(store, handler)
    try:
        first = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "idle"})
        second = await runner.enqueue(project.id, JobCommand.GENERATE_BASE, {"state": "idle"})
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)

        assert maximum_active == 1
        assert store.load_job(project.id, second.id).status == "queued"

        release.set()
        await runner.wait_idle()
        assert store.load_job(project.id, first.id).status == "succeeded"
        assert store.load_job(project.id, second.id).status == "succeeded"
        assert maximum_active == 1
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_default_runner_never_calls_a_provider(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Local-only ranger"))
    runner = JobRunner(store)
    try:
        job = await runner.enqueue(project.id, JobCommand.GENERATE_BASE, {"state": "idle"})
        await runner.wait_idle()

        saved = store.load_job(project.id, job.id)
        assert saved.status == "attention_required"
        assert saved.error and saved.error.code == "no_job_handler"
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_persists_handler_progress_and_outputs(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Progress ranger"))
    entered = asyncio.Event()
    release = asyncio.Event()
    output_id = uuid4()

    async def handler(_job, _cancel_requested):
        entered.set()
        await release.wait()

    runner = JobRunner(store, handler)
    try:
        job = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "idle"})
        await asyncio.wait_for(entered.wait(), timeout=1)

        updated = await runner.update(job.id, progress=0.5, output_artifact_ids=[output_id])
        assert updated.progress == 0.5
        assert store.load_job(project.id, job.id).output_artifact_ids == [output_id]

        release.set()
        await runner.wait_idle()
        saved = store.load_job(project.id, job.id)
        assert saved.progress == 1
        assert saved.output_artifact_ids == [output_id]
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_persists_a_safe_error_when_a_handler_fails(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Failure ranger"))

    async def handler(_job, _cancel_requested):
        raise RuntimeError("provider secret")

    runner = JobRunner(store, handler)
    try:
        job = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "idle"})
        await runner.wait_idle()

        saved = store.load_job(project.id, job.id)
        assert saved.status == "failed"
        assert saved.error and saved.error.code == "job_failed"
        assert "secret" not in saved.error.message
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_event_stream_replays_then_waits_for_new_persisted_events(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Streaming ranger"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_job, _cancel_requested):
        entered.set()
        await release.wait()

    runner = JobRunner(store, handler)
    try:
        job = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "idle"})
        stream = runner.event_stream(job.id, after_id=1)
        event = await asyncio.wait_for(anext(stream), timeout=1)

        assert event["id"] == 2
        assert event["data"]["status"] == "running"
        await asyncio.wait_for(entered.wait(), timeout=1)
        release.set()
        await stream.aclose()
        await runner.wait_idle()
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_cancels_a_queued_job_without_executing_it(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Queued cancellation ranger"))
    entered = asyncio.Event()
    release = asyncio.Event()
    executions = []

    async def handler(job, _cancel_requested):
        executions.append(job.id)
        entered.set()
        await release.wait()

    runner = JobRunner(store, handler)
    try:
        first = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "idle"})
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = await runner.enqueue(project.id, JobCommand.GENERATE_BASE, {"state": "idle"})

        assert (await runner.cancel(second.id)).status == "canceled"

        release.set()
        await runner.wait_idle()
        assert executions == [first.id]
        assert store.load_job(project.id, second.id).status == "canceled"
    finally:
        await runner.aclose()


@pytest.mark.asyncio
async def test_event_stream_replays_only_its_job_and_skips_unrelated_wakes(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Isolated events ranger"))
    target = JobRecord(
        id=uuid4(),
        project_id=project.id,
        command=JobCommand.RUN_QA,
        status="running",
        payload={},
        input_hash="a" * 64,
        provider_request_ids=[],
        output_artifact_ids=[],
        progress=0,
        error=None,
    )
    other = target.model_copy(update={"id": uuid4(), "input_hash": "b" * 64})
    store.save_job(target)
    store.save_job(other)
    store.append_job_event(project.id, "job", target.model_dump(mode="json"))
    store.append_job_event(project.id, "job", other.model_dump(mode="json"))

    runner = JobRunner(store)
    stream = runner.event_stream(target.id)
    pending = None
    try:
        first = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)

        assert first["data"]["id"] == str(target.id)
        assert not pending.done()

        await runner.update(target.id, progress=0.5)
        resumed = await asyncio.wait_for(pending, timeout=1)
        assert resumed["data"]["id"] == str(target.id)
        assert resumed["id"] == 3
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        await stream.aclose()
        await runner.aclose()


@pytest.mark.asyncio
async def test_event_stream_rescans_after_a_replayed_event_before_terminal_close(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Terminal replay ranger"))
    running = JobRecord(
        id=uuid4(),
        project_id=project.id,
        command=JobCommand.RUN_QA,
        status="running",
        payload={},
        input_hash="a" * 64,
        provider_request_ids=[],
        output_artifact_ids=[],
        progress=0,
        error=None,
    )
    store.save_job(running)
    store.append_job_event(project.id, "job", running.model_dump(mode="json"))

    runner = JobRunner(store)
    stream = runner.event_stream(running.id)
    try:
        replayed = await anext(stream)

        completed = running.model_copy(update={"status": "succeeded", "progress": 1})
        store.save_job(completed)
        await runner._emit(completed)

        terminal = await anext(stream)

        assert replayed["data"]["status"] == "running"
        assert terminal["id"] == 2
        assert terminal["data"]["status"] == "succeeded"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()
        await runner.aclose()


@pytest.mark.asyncio
async def test_event_stream_repairs_a_transient_nonterminal_event_write_while_waiting(
    tmp_path, monkeypatch
):
    class TrackingCondition(asyncio.Condition):
        def __init__(self):
            super().__init__()
            self.waiting = asyncio.Event()
            self.woke = asyncio.Event()

        async def wait_for(self, predicate):
            self.waiting.set()
            while not predicate():
                await self.wait()
                self.woke.set()
            return True

    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Live repair ranger"))
    running = JobRecord(
        id=uuid4(),
        project_id=project.id,
        command=JobCommand.RUN_QA,
        status="running",
        payload={},
        input_hash="a" * 64,
        provider_request_ids=[],
        output_artifact_ids=[],
        progress=0,
        error=None,
    )
    store.save_job(running)
    store.append_job_event(project.id, "job", running.model_dump(mode="json"))

    runner = JobRunner(store)
    condition = TrackingCondition()
    runner._events_changed = condition
    stream = runner.event_stream(running.id)
    pending = None
    try:
        assert (await anext(stream))["id"] == 1
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(condition.waiting.wait(), timeout=1)

        original_append = store.append_job_event
        failed = False

        def fail_once(project_id, event, data):
            nonlocal failed
            if not failed:
                failed = True
                raise ProjectStoreError("transient event write failure")
            return original_append(project_id, event, data)

        monkeypatch.setattr(store, "append_job_event", fail_once)
        await runner.update(running.id, progress=0.5)
        await asyncio.wait_for(condition.woke.wait(), timeout=1)

        repaired = await asyncio.wait_for(pending, timeout=1)
        assert repaired["id"] == 2
        assert repaired["data"]["progress"] == 0.5
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        await stream.aclose()
        await runner.aclose()


@pytest.mark.asyncio
async def test_runner_exclusively_locks_its_workspace_for_its_lifetime(tmp_path):
    first = JobRunner(ProjectStore(tmp_path))
    second = JobRunner(ProjectStore(tmp_path))
    try:
        await first.start()

        with pytest.raises(ProjectStoreError, match="already in use"):
            await second.start()
    finally:
        await second.aclose()
        await first.aclose()


@pytest.mark.asyncio
async def test_runner_lock_rejects_a_second_process_in_the_same_workspace(tmp_path):
    first = JobRunner(ProjectStore(tmp_path))
    script = """
import asyncio
import sys

from ai_sprite_studio.jobs import JobRunner
from ai_sprite_studio.project_store import ProjectStore, ProjectStoreError


async def main():
    runner = JobRunner(ProjectStore(sys.argv[1]))
    try:
        await runner.start()
    except ProjectStoreError:
        return 0
    await runner.aclose()
    return 1


raise SystemExit(asyncio.run(main()))
"""
    try:
        await first.start()
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=5,
        )

        assert result.returncode == 0, result.stderr
    finally:
        await first.aclose()


@pytest.mark.asyncio
async def test_runner_skips_only_pre_manifest_project_crash_residue(tmp_path):
    store = ProjectStore(tmp_path)
    (tmp_path / "projects" / str(uuid4())).mkdir(parents=True)
    runner = JobRunner(store)
    try:
        await runner.start()
    finally:
        await runner.aclose()

    project = store.create(ProjectConfig(name="Malformed ranger"))
    manifest = tmp_path / "projects" / str(project.id) / "project.json"
    manifest.write_text("{not JSON")
    broken = JobRunner(ProjectStore(tmp_path))
    try:
        with pytest.raises(ProjectStoreError, match="malformed project JSON"):
            await broken.start()
    finally:
        await broken.aclose()


def test_job_events_replace_the_complete_snapshot_atomically(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Atomic events ranger"))
    writes = []
    original = ProjectStore._atomic_write_at

    def record_write(cls, parent_fd, target_name, data, *, overwrite):
        writes.append((target_name, data, overwrite))
        return original(parent_fd, target_name, data, overwrite=overwrite)

    monkeypatch.setattr(ProjectStore, "_atomic_write_at", classmethod(record_write))

    store.append_job_event(project.id, "job", {"id": "first"})
    store.append_job_event(project.id, "job", {"id": "second"})

    assert [name for name, _data, _overwrite in writes] == ["events.ndjson", "events.ndjson"]
    assert all(overwrite for _name, _data, overwrite in writes)
    assert writes[-1][1].count(b"\n") == 2
    assert b'"id":1' in writes[-1][1]
    assert b'"id":2' in writes[-1][1]


@pytest.mark.asyncio
async def test_runner_repairs_a_missing_terminal_event_on_start_and_before_streaming(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Reconciled events ranger"))
    job = JobRecord(
        id=uuid4(),
        project_id=project.id,
        command=JobCommand.RUN_QA,
        status="succeeded",
        payload={},
        input_hash="a" * 64,
        provider_request_ids=[],
        output_artifact_ids=[],
        progress=1,
        error=None,
    )
    store.save_job(job)
    runner = JobRunner(store)
    stream = None
    try:
        await runner.start()

        assert [event["data"]["status"] for event in store.job_events(project.id)] == ["succeeded"]

        failed = job.model_copy(update={"status": "failed"})
        store.save_job(failed)
        stream = runner.event_stream(job.id, after_id=1)
        event = await anext(stream)

        assert event["id"] == 2
        assert event["data"]["status"] == "failed"
    finally:
        if stream is not None:
            await stream.aclose()
        await runner.aclose()


@pytest.mark.asyncio
async def test_event_write_failure_keeps_saved_terminal_job_and_worker_alive(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    project = store.create(ProjectConfig(name="Event failure ranger"))
    executed = []

    async def handler(job, _cancel_requested):
        executed.append(job.id)

    original_append = store.append_job_event
    terminal_write_failed = False

    def fail_first_terminal_event(project_id, event, data):
        nonlocal terminal_write_failed
        if data["status"] == "succeeded" and not terminal_write_failed:
            terminal_write_failed = True
            raise ProjectStoreError("event write failed")
        return original_append(project_id, event, data)

    monkeypatch.setattr(store, "append_job_event", fail_first_terminal_event)
    runner = JobRunner(store, handler)
    stream = None
    try:
        first = await runner.enqueue(project.id, JobCommand.RUN_QA, {"state": "first"})
        await runner.wait_idle()
        second = await runner.enqueue(project.id, JobCommand.GENERATE_BASE, {"state": "second"})
        await runner.wait_idle()

        assert store.load_job(project.id, first.id).status == "succeeded"
        assert store.load_job(project.id, second.id).status == "succeeded"
        assert executed == [first.id, second.id]

        stream = runner.event_stream(first.id)
        replayed = [event async for event in stream]
        assert replayed[-1]["data"]["status"] == "succeeded"
    finally:
        if stream is not None:
            await stream.aclose()
        await runner.aclose()
