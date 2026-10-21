"""The persisted job queue, its workers, and pause/resume/stop."""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from fastapi import HTTPException
from . import config, history, pipeline


JOBS: Dict[str, Dict[str, Any]] = {}
JOB_RUNNERS: Dict[str, Tuple[Callable[..., None], Tuple[Any, ...]]] = {}
JOBS_LOCK = threading.RLock()
JOBS_CONDITION = threading.Condition(JOBS_LOCK)
QUEUE_WORKERS_STARTED = False
# Per-chunk progress ticks otherwise wrote the whole job out to disk once per sentence - a
# document with thousands of sentences did thousands of small synchronous writes purely for
# crash-resume bookkeeping. update_job skips a write within this many seconds of the last one for
# the same job, unless the job just reached a terminal status.
JOB_PERSIST_MIN_INTERVAL_SECONDS = 2.0
JOB_LAST_PERSISTED_AT: Dict[str, float] = {}
# The job the current worker thread is running, so a model download deep inside a translation
# call can report into that job's queue row.
ACTIVE_JOB = threading.local()


def job_json_path(job_id: str) -> Path:
    return config.JOBS_DIR / f"{job_id}.json"


def job_payload_path(job_id: str) -> Path:
    return config.JOBS_DIR / f"{job_id}.payload"


def persisted_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in job.items() if key != "result"}


def public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    visible = dict(job)
    visible.pop("text", None)
    visible.pop("payload_path", None)
    visible.pop("source_payload_path", None)
    return visible


def job_with_recovered_result(job: Dict[str, Any]) -> Dict[str, Any]:
    visible = public_job(job)
    if visible.get("status") == "complete" and not visible.get("result") and visible.get("history_id"):
        history_path, _ = history.history_paths(visible["history_id"])
        if history_path.exists():
            visible["result"] = history_path.read_text(encoding="utf-8")
    return visible


def persist_job(job: Dict[str, Any]):
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_json_path(job["id"]).write_text(json.dumps(persisted_job(job)), encoding="utf-8")


def load_persisted_jobs():
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        for meta_path in config.JOBS_DIR.glob("*.json"):
            try:
                job = json.loads(meta_path.read_text(encoding="utf-8"))
                if job.get("status") == "running":
                    job["status"] = "queued"
                    job["message"] = "Requeued after restart"
                    job["downloading"] = None
                    job["started_at"] = None
                job.setdefault("result", None)
                job.setdefault("error", None)
                JOBS[job["id"]] = job
            except Exception:
                continue


def job_sort_key(job: Dict[str, Any]):
    return job.get("queued_at") or job.get("started_at") or job.get("finished_at") or 0


def queue_positions_by_job_id() -> Dict[str, int]:
    queued_position = 0
    positions: Dict[str, int] = {}
    for job in sorted(JOBS.values(), key=job_sort_key):
        if job.get("status") == "queued":
            queued_position += 1
            positions[job["id"]] = queued_position
    return positions


# How long a finished job stays in the queue before it is dropped. Its translation is in the
# history by then; what the job itself still answers for is the tab that was watching it, and an
# hour is far longer than anyone waits for that.
FINISHED_JOB_RETENTION_SECONDS = 3600


def cleanup_finished_jobs():
    """Drop jobs that ended long ago, from memory and from disk.

    Nothing ever removed them: 137 of them had piled up on the test machine, all complete, held in
    memory, written out as one file each, reloaded on every restart and sent over the wire with
    every poll. Only the ones still queued or running have anything left to do.
    """
    cutoff = time.time() - FINISHED_JOB_RETENTION_SECONDS
    with JOBS_LOCK:
        stale = [job_id for job_id, job in JOBS.items()
                 if job.get("status") in ("complete", "failed", "cancelled")
                 # Falls back to when it was queued: a record without a finish time is not a
                 # reason to keep it for ever, but it should not vanish the moment it appears.
                 and (job.get("finished_at") or job.get("queued_at") or 0) < cutoff]
        for job_id in stale:
            JOBS.pop(job_id, None)
            JOB_RUNNERS.pop(job_id, None)
            JOB_LAST_PERSISTED_AT.pop(job_id, None)
            job_json_path(job_id).unlink(missing_ok=True)


def list_jobs():
    """The queue as the UI polls it, every few seconds, without the translated text.

    A finished job keeps its whole result in memory for /jobs/{id} to hand back, and carrying that
    through the listing made the answer 136 KB where the list shows none of it - three seconds
    later, and again three seconds after that, for every open tab.
    """
    cleanup_finished_jobs()
    with JOBS_LOCK:
        positions = queue_positions_by_job_id()
        jobs = [public_job(job) for job in sorted(JOBS.values(), key=job_sort_key)]
        for job in jobs:
            job["position"] = positions.get(job["id"])
            job.pop("result", None)
        return jobs


def create_job(kind: str, source: str = "", target: str = "", label: str = "") -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "message": "Queued",
            "source": source,
            "target": target,
            "label": label,
            "queued_at": time.time(),
            "current": 0,
            "total": 0,
            "percent": 0,
            "eta_seconds": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "pause_requested": False,
            "cancel_requested": False,
        }
        persist_job(JOBS[job_id])
    return job_id


def update_job(job_id: str, **values):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(values)
        current = job.get("current") or 0
        total = job.get("total") or 0
        started_at = job.get("started_at")
        if total:
            percent = (current / total) * 100
            if job.get("status") == "running" and current >= total:
                # Every chunk translated, but a PDF job still has to render the result -
                # redaction, reflow, font subsetting - which can take real time on a big
                # document. Without the cap the bar and the number both read as finished long
                # before the file is actually there to download.
                percent = min(percent, 99.0)
            job["percent"] = round(percent, 1)
        if started_at and current and total and current < total:
            elapsed = time.time() - started_at
            job["eta_seconds"] = round((elapsed / current) * (total - current))
        elif current and total and current >= total:
            job["eta_seconds"] = 0
        now = time.time()
        terminal = job.get("status") in ("complete", "failed", "cancelled")
        control_flag_changed = "cancel_requested" in values or "pause_requested" in values
        if terminal or control_flag_changed or now - JOB_LAST_PERSISTED_AT.get(job_id, 0) >= JOB_PERSIST_MIN_INTERVAL_SECONDS:
            persist_job(job)
            JOB_LAST_PERSISTED_AT[job_id] = now
        JOBS_CONDITION.notify_all()


def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        visible = job_with_recovered_result(job)
        visible["position"] = queue_positions_by_job_id().get(job_id)
        return visible


def control_job(job_id: str, action: str):
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(status_code=400, detail="Unknown job action")
    current = get_job(job_id)
    if action == "pause":
        update_job(job_id, pause_requested=True, message="Pause requested")
    elif action == "resume":
        update_job(job_id, pause_requested=False, message="Resuming")
    elif action == "cancel":
        if current.get("status") == "queued":
            update_job(
                job_id,
                status="cancelled",
                cancel_requested=True,
                pause_requested=False,
                message="Cancelled",
                finished_at=time.time(),
            )
            cleanup_job_payload(job_id)
        else:
            update_job(job_id, cancel_requested=True, pause_requested=False, message="Stop requested")
    return get_job(job_id)


def register_job_runner(job_id: str, runner: Callable[..., None], args: Tuple[Any, ...]):
    with JOBS_LOCK:
        JOB_RUNNERS[job_id] = (runner, args)
        persist_job(JOBS[job_id])
        JOBS_CONDITION.notify_all()


def queued_job_without_active_worker(active_jobs: set) -> Optional[Dict[str, Any]]:
    for job in sorted(JOBS.values(), key=job_sort_key):
        if job.get("status") == "queued" and not job.get("cancel_requested") and job["id"] not in active_jobs:
            return job
    return None


def rebuild_runner_for_job(job: Dict[str, Any]) -> Optional[Tuple[Callable[..., None], Tuple[Any, ...]]]:
    job_id = job["id"]
    if job.get("kind") == "translate":
        source_payload_path = job.get("source_payload_path")
        source_content = b""
        if source_payload_path and Path(source_payload_path).exists():
            source_content = Path(source_payload_path).read_bytes()
        return pipeline.run_text_job, (
            job_id,
            job.get("text", ""),
            job.get("source", config.DEFAULT_SOURCE),
            job.get("target", config.DEFAULT_TARGET),
            job.get("original_name", "text"),
            source_content,
            job.get("source_extension", ""),
            job.get("source_meta", {}),
        )
    if job.get("kind") == "translate-pdf":
        payload_path = job.get("payload_path")
        if not payload_path or not Path(payload_path).exists():
            update_job(job_id, status="failed", message="Failed", error="Queued PDF payload is missing", finished_at=time.time())
            return None
        return pipeline.run_pdf_translate_job, (
            job_id,
            Path(payload_path).read_bytes(),
            job.get("content_type", "application/pdf"),
            job.get("source", config.DEFAULT_SOURCE),
            job.get("target", config.DEFAULT_TARGET),
            job.get("filename", "pdf"),
            job.get("page_range", ""),
        )
    if job.get("kind") == "translate-pdf-layout":
        payload_path = job.get("payload_path")
        if not payload_path or not Path(payload_path).exists():
            update_job(job_id, status="failed", message="Failed", error="Queued PDF payload is missing", finished_at=time.time())
            return None
        return pipeline.run_pdf_layout_translate_job, (
            job_id,
            Path(payload_path).read_bytes(),
            job.get("source", config.DEFAULT_SOURCE),
            job.get("target", config.DEFAULT_TARGET),
            job.get("filename", "pdf"),
            job.get("page_range", ""),
        )
    # An unrecognised kind (a hand-edited or corrupted job file, or one from a kind since
    # removed) must not come back as None with the job left "queued" - job_worker_loop would
    # just pick the same job again with nothing to make it wait, spinning one worker at 100% CPU
    # forever instead of blocking on JOBS_CONDITION.wait() like every other empty queue does.
    update_job(job_id, status="failed", message="Failed", error=f"Unknown job kind: {job.get('kind')}", finished_at=time.time())
    return None


def job_worker_loop():
    active_jobs = set()
    while True:
        with JOBS_CONDITION:
            while True:
                active_jobs = {
                    job_id for job_id, job in JOBS.items()
                    if job.get("status") == "running" and job_id in JOB_RUNNERS
                }
                if len(active_jobs) < config.JOB_WORKERS:
                    job = queued_job_without_active_worker(active_jobs)
                    if job:
                        break
                JOBS_CONDITION.wait()
            job_id = job["id"]
            runner_data = JOB_RUNNERS.get(job_id) or rebuild_runner_for_job(job)
            if not runner_data:
                continue
            JOB_RUNNERS[job_id] = runner_data
            job["status"] = "running"
            job["message"] = "Starting"
            if not job.get("started_at"):
                job["started_at"] = time.time()
            persist_job(job)
        runner, args = runner_data
        ACTIVE_JOB.id = job_id
        try:
            runner(*args)
        finally:
            ACTIVE_JOB.id = None
        with JOBS_CONDITION:
            JOB_RUNNERS.pop(job["id"], None)
            JOBS_CONDITION.notify_all()


def ensure_queue_workers():
    global QUEUE_WORKERS_STARTED
    with JOBS_LOCK:
        if QUEUE_WORKERS_STARTED:
            return
        load_persisted_jobs()
        QUEUE_WORKERS_STARTED = True
        for _ in range(config.JOB_WORKERS):
            threading.Thread(target=job_worker_loop, daemon=True).start()
        JOBS_CONDITION.notify_all()


def wait_if_paused_or_cancelled(job_id: str):
    while True:
        job = get_job(job_id)
        if job.get("cancel_requested"):
            raise RuntimeError("Job stopped by user")
        if not job.get("pause_requested"):
            return
        update_job(job_id, status="paused", message="Paused")
        time.sleep(1)


def cleanup_job_payload(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        payload_path = job.get("payload_path")
        if payload_path:
            Path(payload_path).unlink(missing_ok=True)
        source_payload_path = job.get("source_payload_path")
        if source_payload_path:
            Path(source_payload_path).unlink(missing_ok=True)
        if "text" in job:
            job["text"] = ""
        persist_job(job)


def exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)
