import asyncio
from uuid import uuid4

import pytest

from ai_sprite_studio.contracts import JobCommand, JobRecord, ProjectConfig
from ai_sprite_studio.jobs import JobRunner
from ai_sprite_studio.project_store import ProjectStore


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
