"""Quality benchmark: translate every file in tests/testfiles the way the UI does and measure it.

Run inside the app's container (it needs the models), e.g. on the testbench:

    docker run --rm -v "$PWD:/w" -w /w -v ~/hf:/root/.cache/huggingface <image> \
        python tools/benchmark.py --out /w/bench-out [--baseline /w/bench-old/results.json] [--quick]

--quick runs every file but the long PDFs on a few pages only (a few minutes instead of ten); the
full run is for a release.

Per file it records the time, every model call's source and output (via translate_batch), how often
guard_hallucination threw a translation away, and for PDFs the lines that overlap in the result,
plus page images of original and translation side by side. With --baseline it prints the change
against an earlier run, so a change to the pipeline is measured instead of judged on single pages.
Files whose text is German are translated to English, everything else to German.
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

TESTFILES = Path(__file__).resolve().parent.parent / "tests" / "testfiles"
SKIP_DIRS = {"Ergebnisse"}
TEXT_SUFFIXES = {".txt", ".md"}
# A translation this much shorter than its source (in characters, sources of 30 and more) has most
# likely dropped part of it. Characters, not words: German compounds made "Table 1. Operational
# specifications ..." look lost against its complete "Tabelle 1. Betriebsspezifikationen ...".
LOST_CHAR_RATIO = 0.55
LOST_MIN_CHARS = 30
# Names, IDs and terms rightly come back unchanged ("MP-001", "Gateway", "Maximilian Rade"); only a
# sentence that does is counted as untranslated.
UNTRANSLATED_MIN_WORDS = 4
# --quick: every file still runs, so every format is covered, but the long PDFs only on pages that
# carry their kind of problem. Most of a full run (about 460 of 590 s) is these five files.
QUICK_PAGES = {
    "Powerupall Pdf.pdf": "1-12",                              # front matter, author names, footnotes
    "Ruders - The Handmaids Tale - KA_1.pdf": "1,3,6-9,24,30",  # title, contents, cast list, synopsis EN/DA, scores
    "MatterhornProtokoll.pdf": "1-5",                          # tables, curly quotes, the Symbol bullet
    "Two_Column_Paper.pdf": "1-2",
}
# Two lines of the result count as overlapping when they share this much of the smaller one's area.
OVERLAP_SHARE = 0.25

calls = []
guard_hits = []


def instrument():
    # The functions are looked up in the module that defines them, so that is where they are wrapped.
    home = getattr(main, "translation", main)
    batch, guard = home.translate_batch, home.guard_hallucination

    def recording_batch(texts, source, target):
        out = batch(texts, source, target)
        calls.extend({"source": s, "output": o, "language": source} for s, o in zip(texts, out))
        return out

    def counting_guard(source, translated, target=""):
        kept = guard(source, translated, target)
        if kept.strip() == source.strip() and translated.strip() != source.strip():
            guard_hits.append({"source": source[:160], "rejected": translated[:160]})
        return kept

    home.translate_batch, home.guard_hallucination = recording_batch, counting_guard


def words(text):
    return len(re.findall(r"[^\W\d_]{2,}", text))


def overlaps(pdf):
    """Pairs of text lines in the result that sit on top of each other, per document."""
    count = 0
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        for page in document:
            boxes = [pymupdf.Rect(line["bbox"]) for block in page.get_text("dict")["blocks"]
                     for line in block.get("lines", []) if any(s["text"].strip() for s in line["spans"])]
            for i, a in enumerate(boxes):
                for b in boxes[i + 1:]:
                    shared = (a & b)
                    smaller = min(a.get_area(), b.get_area())
                    if smaller > 0 and not shared.is_empty and shared.get_area() >= OVERLAP_SHARE * smaller:
                        count += 1
    return count


def side_by_side(original, translated, out_dir, stem, pages=2):
    """Page images, original left and translation right, for the first pages."""
    with pymupdf.open(stream=original, filetype="pdf") as left, pymupdf.open(stream=translated, filetype="pdf") as right:
        for number in range(min(pages, left.page_count, right.page_count)):
            a, b = left[number].get_pixmap(dpi=60), right[number].get_pixmap(dpi=60)
            canvas = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, a.width + b.width + 10, max(a.height, b.height)), False)
            canvas.clear_with(255)
            canvas.copy(a, a.irect)
            b.set_origin(a.width + 10, 0)
            canvas.copy(b, b.irect)
            canvas.save(str(out_dir / f"{stem}-p{number + 1}.png"))


def run_job(client, path, target, page_range=""):
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        response = client.post("/jobs/translate-pdf-layout", files={"file": (path.name, data, "application/pdf")},
                               data={"source": main.AUTO_SOURCE, "target": target, "page_range": page_range})
    else:
        if suffix in TEXT_SUFFIXES:
            text = data.decode("utf-8", "replace")
        else:
            route = {".csv": "extract-csv", ".xlsx": "extract-xlsx", ".odt": "extract-odt",
                     ".pptx": "extract-pptx"}.get(suffix, "extract-docx")
            text = client.post(f"/{route}", files={"file": (path.name, data)}).text
        response = client.post("/jobs/translate-file", files={"file": (path.name, data)},
                               data={"text": text, "source": main.AUTO_SOURCE, "target": target})
    job_id = response.json()["job_id"]
    while True:
        job = main.get_job(job_id)
        if job["status"] in ("complete", "failed", "cancelled"):
            return job, data
        time.sleep(0.5)


def measure(client, path, out_dir, page_range=""):
    calls.clear()
    guard_hits.clear()
    started = time.time()
    job, data = run_job(client, path, "deu_Latn", page_range)
    if job.get("source") == "deu_Latn":
        calls.clear()
        guard_hits.clear()
        started = time.time()
        job, data = run_job(client, path, "eng_Latn", page_range)
    seconds = time.time() - started
    translatable = [c for c in calls if main.has_translatable_text(c["source"])]
    untranslated = [c for c in translatable if c["output"].strip() == c["source"].strip()
                    and words(c["source"]) >= UNTRANSLATED_MIN_WORDS]
    lost = [c for c in translatable if c["output"].strip() != c["source"].strip()
            and len(c["source"]) >= LOST_MIN_CHARS and len(c["output"]) < LOST_CHAR_RATIO * len(c["source"])]
    row = {
        "file": str(path.relative_to(TESTFILES)) + (f" [{page_range}]" if page_range else ""),
        "status": job["status"], "error": job.get("error"),
        "seconds": round(seconds, 1), "source": job.get("source"), "target": job.get("target"),
        "chunks": len(translatable), "untranslated": len(untranslated), "lost": len(lost),
        "guard_hits": len(guard_hits), "overlaps": None,
        # Two runs with the same result_sha translated the file word for word the same.
        "result_sha": hashlib.sha256((job.get("result") or "").encode("utf-8")).hexdigest()[:16],
        "samples": {"untranslated": [c["source"][:120] for c in untranslated[:5]],
                    "lost": [{"source": c["source"][:160], "output": c["output"][:160]} for c in lost[:5]],
                    "guard": guard_hits[:8]},
    }
    if path.suffix.lower() == ".pdf" and job["status"] == "complete":
        item = main.history_item(job["history_id"])
        exported = main.history_original_export(job["history_id"], "pdf", data, job["result"], item["source_meta"])
        row["overlaps"] = overlaps(exported)
        side_by_side(data, exported, out_dir, re.sub(r"\W+", "_", path.stem))
    return row


def report(rows, baseline):
    old = {row["file"]: row for row in baseline} if baseline else {}
    keys = ("seconds", "untranslated", "lost", "guard_hits", "overlaps")
    lines = ["| file | status | " + " | ".join(keys) + " |", "|---" * (len(keys) + 2) + "|"]
    for row in rows:
        cells = []
        for key in keys:
            value, before = row[key], old.get(row["file"], {}).get(key)
            cells.append(f"{value}" if before is None or value is None or before == value else f"{value} ({value - before:+g})")
        lines.append(f"| {row['file']} | {row['status']} | " + " | ".join(cells) + " |")
    totals = {key: sum(row[key] or 0 for row in rows) for key in keys}
    lines.append("| **total** | | " + " | ".join(f"{totals[k]:g}" for k in keys) + " |")
    return "\n".join(lines)


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--only", help="substring of the file paths to run")
    parser.add_argument("--quick", action="store_true", help="the long PDFs on a few pages only (QUICK_PAGES)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    main.HISTORY_DIR, main.JOBS_DIR = args.out / "history", args.out / "jobs"
    instrument()
    files = sorted(p for p in TESTFILES.rglob("*") if p.is_file() and p.parent.name not in SKIP_DIRS
                   and (not args.only or args.only in str(p)))
    rows = []
    with TestClient(main.app) as client:
        for path in files:
            print(f"... {path.relative_to(TESTFILES)}", flush=True)
            rows.append(measure(client, path, args.out, QUICK_PAGES.get(path.name, "") if args.quick else ""))
    (args.out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    summary = report(rows, baseline)
    (args.out / "summary.md").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main_cli()
