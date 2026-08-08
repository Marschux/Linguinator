#!/usr/bin/env python3
"""Compare candidate translation models on quality, speed, and memory.

Standalone tool: does not import app.main, so it can run without the FastAPI app or its
model-loading lifecycle (idle-unload, job queue, ...). Meant to run on the target hardware
(the testbench), since CPU speed and available RAM are exactly what decides between the
candidates.

Each candidate is measured in its own subprocess, so peak memory is per-model instead of
accumulating across the whole run, and a crash in one candidate cannot take the others down.

Usage:
    python tools/compare_models.py                    # run every candidate
    python tools/compare_models.py --candidates m2m100,opus-mt-mul-mul
    python tools/compare_models.py --list              # show candidates and exit
    python tools/compare_models.py --output results.md
"""
import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTFILES_DIR = REPO_ROOT / "tests" / "testfiles"
RESULT_SENTINEL = "COMPARE_MODELS_RESULT_JSON: "

# Same alias table as app/main.py's LANGUAGE_CODE_ALIASES, kept as a small independent copy
# here on purpose: this tool must work standalone without importing the app.
ISO_639_1 = {
    "eng_Latn": "en", "deu_Latn": "de", "rus_Cyrl": "ru", "fra_Latn": "fr", "spa_Latn": "es",
}


@dataclass
class Candidate:
    name: str
    model_id: str
    family: str  # "forced_bos" | "prefix" | "plain" | "ctranslate2"
    license: str
    pair: Optional[Tuple[str, str]] = None  # for "plain": the one (source, target) it handles
    tokenizer_id: Optional[str] = None  # for "ctranslate2": HF id to load a tokenizer from
    notes: str = ""


CANDIDATES: List[Candidate] = [
    Candidate("nllb-600m", "facebook/nllb-200-distilled-600M", "forced_bos", "CC-BY-NC-4.0",
              notes="reference only, not commercially usable"),
    Candidate("m2m100-418m", "facebook/m2m100_418M", "forced_bos", "MIT"),
    Candidate("opus-mt-mul-mul", "Helsinki-NLP/opus-mt-tc-bible-big-mul-mul", "prefix", "Apache-2.0"),
    Candidate("opus-mt-en-de", "Helsinki-NLP/opus-mt-en-de", "plain", "CC-BY-4.0",
              pair=("eng_Latn", "deu_Latn")),
    Candidate("opus-mt-de-en", "Helsinki-NLP/opus-mt-de-en", "plain", "CC-BY-4.0",
              pair=("deu_Latn", "eng_Latn")),
    Candidate("madlad400-3b-ct2", "Nextcloud-AI/madlad400-3b-mt-ct2-int8", "ctranslate2", "Apache-2.0",
              tokenizer_id="google/madlad400-3b-mt",
              notes="needs ctranslate2 + huggingface_hub; skipped if unavailable"),
]


def load_excerpt(path: Path, fallback: str, max_chars: int = 500) -> str:
    try:
        from pypdf import PdfReader

        text = PdfReader(path).pages[0].extract_text() or ""
        text = " ".join(text.split())
        return text[:max_chars] if text else fallback
    except Exception:
        return fallback


def test_cases() -> List[Tuple[str, str, str, str]]:
    technical = load_excerpt(
        TESTFILES_DIR / "Stall Kamera System.pdf",
        "Ich wuerde die Kamera Anschaffung in mehrere Teile unterbrechen, damit man zum einen "
        "gucken kann, bei jeder Phase, ob es funktioniert oder man mehr benoetigt.",
    )
    marketing = load_excerpt(
        TESTFILES_DIR / "Get_Started_With_Smallpdf.pdf",
        "Welcome to Smallpdf. Ready to take document management to the next level? With the new "
        "Smallpdf experience, you can freely upload, organize, and share digital documents.",
    )
    return [
        ("short-en-de", "eng_Latn", "deu_Latn", "Please restart the camera after changing the SD card."),
        ("technical-de-en", "deu_Latn", "eng_Latn", technical),
        ("marketing-en-de", "eng_Latn", "deu_Latn", marketing),
        ("short-en-ru", "eng_Latn", "rus_Cyrl", "Please restart the camera after changing the SD card."),
    ]


def model_language_code(model_id: str, family: str, code: str) -> str:
    lowered = model_id.lower()
    if "nllb" in lowered:
        return code
    if "m2m100" in lowered:
        return ISO_639_1.get(code, code.split("_", 1)[0][:2])
    if family == "ctranslate2":
        return f"<2{ISO_639_1.get(code, code.split('_', 1)[0][:2])}>"
    if family == "prefix":
        return f">>{code.split('_', 1)[0]}<<"
    return code


def prepare_transformers_call(tokenizer, candidate: Candidate, text: str, source: str, target: str):
    if candidate.family == "forced_bos":
        tokenizer.src_lang = model_language_code(candidate.model_id, candidate.family, source)
        inputs = tokenizer(text, return_tensors="pt", truncation=True)
        target_code = model_language_code(candidate.model_id, candidate.family, target)
        get_lang_id = getattr(tokenizer, "get_lang_id", None)
        forced_bos_token_id = (
            get_lang_id(target_code) if get_lang_id else tokenizer.convert_tokens_to_ids(target_code)
        )
        return inputs, {"forced_bos_token_id": forced_bos_token_id}
    if candidate.family == "prefix":
        target_code = model_language_code(candidate.model_id, candidate.family, target)
        return tokenizer(f"{target_code} {text}", return_tensors="pt", truncation=True), {}
    return tokenizer(text, return_tensors="pt", truncation=True), {}


def run_transformers_candidate(candidate: Candidate, cases) -> Dict[str, Any]:
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

    start = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(candidate.model_id)
    model = AutoModelForSeq2SeqLM.from_pretrained(candidate.model_id)
    model.eval()
    load_seconds = time.monotonic() - start

    outputs = []
    for label, source, target, text in cases:
        if candidate.family == "plain" and candidate.pair != (source, target):
            outputs.append({"case": label, "skipped": f"fixed pair {candidate.pair}, does not match"})
            continue
        t0 = time.monotonic()
        inputs, generate_kwargs = prepare_transformers_call(tokenizer, candidate, text, source, target)
        with torch.inference_mode():
            generated = model.generate(**inputs, **generate_kwargs, max_new_tokens=512, num_beams=4)
        text_out = tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
        outputs.append({"case": label, "seconds": time.monotonic() - t0, "output": text_out})
    return {"load_seconds": load_seconds, "outputs": outputs}


def run_ctranslate2_candidate(candidate: Candidate, cases) -> Dict[str, Any]:
    try:
        import ctranslate2
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer
    except ImportError as exc:
        return {"error": f"ctranslate2 path unavailable: {exc}"}

    try:
        start = time.monotonic()
        local_dir = snapshot_download(candidate.model_id)
        tokenizer = AutoTokenizer.from_pretrained(candidate.tokenizer_id)
        translator = ctranslate2.Translator(local_dir, device="cpu")
        load_seconds = time.monotonic() - start
    except Exception as exc:
        return {"error": f"could not load {candidate.model_id}: {exc}"}

    outputs = []
    for label, source, target, text in cases:
        target_code = model_language_code(candidate.model_id, candidate.family, target)
        tokens = tokenizer.tokenize(f"{target_code} {text}")
        t0 = time.monotonic()
        result = translator.translate_batch([tokens], beam_size=4, max_decoding_length=512)
        output_tokens = result[0].hypotheses[0]
        text_out = tokenizer.decode(tokenizer.convert_tokens_to_ids(output_tokens), skip_special_tokens=True)
        outputs.append({"case": label, "seconds": time.monotonic() - t0, "output": text_out})
    return {"load_seconds": load_seconds, "outputs": outputs}


def run_worker(candidate_name: str) -> None:
    """Runs exactly one candidate in this process and prints its result as one JSON line.

    A dedicated subprocess per candidate keeps peak-RSS measurement and any load failure
    isolated to that one model.
    """
    candidate = next((c for c in CANDIDATES if c.name == candidate_name), None)
    if candidate is None:
        print(RESULT_SENTINEL + json.dumps({"name": candidate_name, "error": "unknown candidate"}))
        return

    cases = test_cases()
    try:
        if candidate.family == "ctranslate2":
            result = run_ctranslate2_candidate(candidate, cases)
        else:
            result = run_transformers_candidate(candidate, cases)
    except Exception as exc:
        result = {"error": str(exc)}

    result["name"] = candidate.name
    result["peak_rss_mb"] = peak_rss_mb()
    print(RESULT_SENTINEL + json.dumps(result))


def peak_rss_mb() -> float:
    """Peak resident memory of this process, in MB. `resource` (ru_maxrss, KB on Linux)
    covers the testbench this tool is meant to run on; Windows has no such stdlib call, so
    dev runs elsewhere just report 0 instead of failing."""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        return 0.0


def run_orchestrator(selected: List[Candidate], output_path: Optional[Path]) -> None:
    results = []
    for candidate in selected:
        print(f"--- {candidate.name} ({candidate.model_id}, {candidate.license}) ---", file=sys.stderr)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", candidate.name],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        line = next((l for l in proc.stdout.splitlines() if l.startswith(RESULT_SENTINEL)), None)
        if line is None:
            results.append({
                "name": candidate.name,
                "error": f"worker produced no result (exit {proc.returncode}): {proc.stderr[-800:]}",
            })
            continue
        results.append(json.loads(line[len(RESULT_SENTINEL):]))

    report = render_report(selected, results)
    print(report)
    if output_path:
        output_path.write_text(report, encoding="utf-8")
        print(f"\nWritten to {output_path}", file=sys.stderr)


def render_report(candidates: List[Candidate], results: List[Dict[str, Any]]) -> str:
    lines = ["# Model comparison", ""]
    lines.append("| Candidate | License | Load (s) | Peak RSS (MB) | Notes |")
    lines.append("| --- | --- | --- | --- | --- |")
    by_name = {c.name: c for c in candidates}
    for result in results:
        candidate = by_name[result["name"]]
        if "error" in result:
            lines.append(f"| {candidate.name} | {candidate.license} | - | - | ERROR: {result['error']} |")
            continue
        lines.append(
            f"| {candidate.name} | {candidate.license} | {result.get('load_seconds', 0):.1f} | "
            f"{result.get('peak_rss_mb', 0):.0f} | {candidate.notes} |"
        )
    lines.append("")

    for result in results:
        if "error" in result:
            continue
        candidate = by_name[result["name"]]
        lines.append(f"## {candidate.name}")
        for entry in result.get("outputs", []):
            lines.append(f"**{entry['case']}**")
            if "skipped" in entry:
                lines.append(f"- skipped: {entry['skipped']}")
            else:
                lines.append(f"- {entry['seconds']:.2f}s")
                lines.append(f"- {entry['output']}")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", help="comma-separated candidate names, default: all")
    parser.add_argument("--list", action="store_true", help="list candidates and exit")
    parser.add_argument("--output", type=Path, help="also write the report to this file")
    parser.add_argument("--worker", metavar="NAME", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.worker:
        run_worker(args.worker)
        return

    if args.list:
        for candidate in CANDIDATES:
            print(f"{candidate.name}: {candidate.model_id} ({candidate.license}) {candidate.notes}")
        return

    selected = CANDIDATES
    if args.candidates:
        wanted = {name.strip() for name in args.candidates.split(",")}
        selected = [c for c in CANDIDATES if c.name in wanted]
        missing = wanted - {c.name for c in selected}
        if missing:
            parser.error(f"unknown candidate(s): {', '.join(sorted(missing))}")

    run_orchestrator(selected, args.output)


if __name__ == "__main__":
    main()
