"""Model download, conversion to CTranslate2, the cache and idle unloading."""
import ctypes
import gc
import shutil
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict
from fastapi import HTTPException
from . import config, langs, jobqueue, heron


MODEL_LOCK = threading.RLock()
MODEL_ACTIVE_USERS = 0
MODEL_LAST_USED = 0.0


@lru_cache(maxsize=1)
def selected_device():
    """The GPU when there is one, otherwise the CPU. Nothing to configure: asking for cuda on a
    host without a GPU only ever meant a silent fall back to cpu anyway."""
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


@lru_cache(maxsize=config.MODEL_CACHE_SIZE)
def load_tokenizer(model_id: str):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers is required for translation") from exc
    return AutoTokenizer.from_pretrained(model_id)


# transformers fetches the first of these the repository has.
MODEL_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


def model_is_downloaded(model_id: str) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    return any(isinstance(try_to_load_from_cache(model_id, name), str) for name in MODEL_WEIGHT_FILES)


def model_download_size(model_id: str) -> int:
    """Bytes of the weights file a first load fetches, 0 when the hub does not say."""
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(model_id, files_metadata=True, timeout=10)
        sizes = {entry.rfilename: entry.size or 0 for entry in info.siblings}
    except Exception:
        return 0
    return next((sizes[name] for name in MODEL_WEIGHT_FILES if sizes.get(name)), 0)


def model_cache_dir(model_id: str) -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE
    return Path(HF_HUB_CACHE) / ("models--" + model_id.replace("/", "--"))


def model_cache_bytes(model_id: str) -> int:
    """What is on disk for a model so far, the unfinished download included."""
    total = 0
    for blob in (model_cache_dir(model_id) / "blobs").glob("*"):
        try:
            total += blob.stat().st_size
        except OSError:
            # Renamed from *.incomplete between the listing and the stat.
            continue
    return total


@contextmanager
def report_model_download(model_id: str):
    """Show a first-time model download in the running job's queue row. The hub reports no
    progress to its caller, so the growing cache directory is measured instead."""
    job_id = getattr(jobqueue.ACTIVE_JOB, "id", None)
    if not job_id or model_is_downloaded(model_id):
        yield
        return
    size = model_download_size(model_id)
    megabyte = 1024 * 1024
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            have = model_cache_bytes(model_id)
            if size:
                jobqueue.update_job(job_id, downloading=min(99.0, round(have / size * 100, 1)),
                           message=f"Downloading model {have // megabyte} / {size // megabyte} MB")
            else:
                jobqueue.update_job(job_id, message=f"Downloading model {have // megabyte} MB")

    jobqueue.update_job(job_id, downloading=0, message="Downloading model")
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        done.set()
        watcher.join()
        jobqueue.update_job(job_id, downloading=None, message="Loading model")


def downloaded_state() -> Dict[str, Any]:
    """Which models are on disk: the dedicated pairs among them, and the fallback every other
    pair uses."""
    pairs = []
    for key, entry in langs.OPUS_PAIRS.items():
        source, _, target = key.partition(">")
        if source in langs.CORE_LANGUAGES and target in langs.CORE_LANGUAGES and model_is_downloaded(entry["model_id"]):
            pairs.append([langs.CORE_LANGUAGES[source], langs.CORE_LANGUAGES[target]])
    return {"downloaded_pairs": pairs, "fallback_downloaded": model_is_downloaded(config.FALLBACK_MODEL_ID)}


def delete_downloaded_model(model_id: str) -> None:
    with jobqueue.JOBS_LOCK:
        if any(job.get("status") == "running" for job in jobqueue.JOBS.values()):
            raise HTTPException(status_code=409, detail="A translation is running. Try again when it has finished.")
    # The model in memory may be this one, and it would be reloaded from files that are gone.
    unload_model_cache()
    shutil.rmtree(model_cache_dir(model_id), ignore_errors=True)
    shutil.rmtree(converted_model_dir(model_id), ignore_errors=True)


def converted_model_dir(model_id: str) -> Path:
    """Where a model's CTranslate2 copy lives, next to the hub cache so it shares its volume."""
    from huggingface_hub.constants import HF_HUB_CACHE
    return Path(HF_HUB_CACHE).parent / "ctranslate2" / model_id.replace("/", "--")


def convert_model(model_id: str) -> Path:
    """The model in CTranslate2's format, converted from the hub weights on first use.

    Translation runs on CTranslate2 rather than transformers: measured 21.10.2026 on
    opus-mt-en-de, 175 sentences on 4 cores, 2.6x faster one sentence at a time and 4.1x in
    batches of 8, where transformers gained nothing from batching. Kept at float32: int8 was
    faster still (12.9x) but visibly worse ("Unsere Magd, Moira" for "Our Handmaid's friend,
    Moira"), float32 only differed in variants like "Gospel-Sängerin"/"Gospelsängerin".
    """
    target = converted_model_dir(model_id)
    if (target / "model.bin").exists():
        return target
    import ctranslate2
    # Converted beside the target and renamed at the end, so an interrupted conversion is never
    # mistaken for a finished one.
    partial = target.with_name(target.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    ctranslate2.converters.TransformersConverter(model_id).convert(str(partial), force=True)
    shutil.rmtree(target, ignore_errors=True)
    partial.rename(target)
    # The converter held the whole torch model; give that back before the translator loads.
    gc.collect()
    return target


@lru_cache(maxsize=config.MODEL_CACHE_SIZE)
def load_model(model_id: str):
    try:
        import ctranslate2
    except ImportError as exc:
        raise RuntimeError("ctranslate2 is required for translation") from exc
    with report_model_download(model_id):
        tokenizer = load_tokenizer(model_id)
        path = convert_model(model_id)
    translator = ctranslate2.Translator(str(path), device=selected_device(), intra_threads=max(config.CPU_THREADS, 0))
    return tokenizer, translator


def model_cache_loaded() -> bool:
    return load_model.cache_info().currsize > 0 or load_tokenizer.cache_info().currsize > 0


def begin_model_use():
    global MODEL_ACTIVE_USERS
    with MODEL_LOCK:
        MODEL_ACTIVE_USERS += 1


def end_model_use():
    global MODEL_ACTIVE_USERS, MODEL_LAST_USED
    with MODEL_LOCK:
        MODEL_ACTIVE_USERS = max(0, MODEL_ACTIVE_USERS - 1)
        MODEL_LAST_USED = time.time()


def unload_model_cache() -> bool:
    with MODEL_LOCK:
        if MODEL_ACTIVE_USERS > 0:
            return False
        if not model_cache_loaded() and not heron.load_layout_model.cache_info().currsize:
            return False
        load_model.cache_clear()
        load_tokenizer.cache_clear()
        heron.load_layout_model.cache_clear()
    gc.collect()
    try:
        # glibc keeps freed heap arenas rather than returning them to the OS, so RSS stays
        # high after unload without this - no-op on non-glibc platforms (Windows/macOS dev).
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass
    return True


def unload_model_if_idle(now: float | None = None) -> bool:
    if config.MODEL_IDLE_SECONDS <= 0:
        return False
    current_time = time.time() if now is None else now
    with MODEL_LOCK:
        if MODEL_ACTIVE_USERS > 0 or MODEL_LAST_USED <= 0:
            return False
        if current_time - MODEL_LAST_USED < config.MODEL_IDLE_SECONDS:
            return False
    return unload_model_cache()


def model_idle_unloader():
    while True:
        sleep_seconds = min(60, max(1, config.MODEL_IDLE_SECONDS // 4 or 1))
        time.sleep(sleep_seconds)
        unload_model_if_idle()


if config.MODEL_IDLE_SECONDS > 0:
    threading.Thread(target=model_idle_unloader, daemon=True).start()
