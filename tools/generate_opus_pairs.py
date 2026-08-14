#!/usr/bin/env python3
"""Generate app/opus_pairs.json: a source>target -> model-id lookup for OPUS-MT bilingual models.

Run manually and commit the result; the app never queries the Hugging Face API at runtime.
OPUS-MT model ids are not systematic (opus-mt-en-de exists, opus-mt-tc-big-en-de does not; for
Turkish it is the other way round), so this table has to be built from what the API actually
lists rather than guessed from a naming pattern.

Usage:
    python tools/generate_opus_pairs.py [--languages en,de,fr,...]
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

API_URL = "https://huggingface.co/api/models?author=Helsinki-NLP&limit=1000"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "app" / "opus_pairs.json"

# Same short list as app/main.py's CORE_LANGUAGES; kept as a plain default here since this
# script must run standalone, without importing the app.
DEFAULT_LANGUAGES = [
    "en", "de", "fr", "es", "it", "nl", "pt", "pl", "ru", "uk", "sv", "da", "fi", "el",
    "hu", "bg", "zh", "ja", "tr",
]

# opus-mt-tc-big-* models are newer Tatoeba-Challenge releases and generally translate better
# than the older opus-mt-* ones; prefer them whenever both exist for the same pair.
PAIR_RE = re.compile(r"^opus-mt(?:-tc-big)?-([a-z]{2,3})-([a-z]{2,3})$")


def fetch_all_models() -> List[dict]:
    models = []
    url = API_URL
    while url:
        request = urllib.request.Request(url, headers={"User-Agent": "linguinator-tools"})
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.URLError as exc:
            print(f"error fetching {url}: {exc}", file=sys.stderr)
            break
        models.extend(json.load(response))
        link = response.headers.get("Link", "")
        match = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
        url = match.group(1) if match else None
        time.sleep(0.2)
    return models


def license_of(model: dict) -> str:
    for tag in model.get("tags", []):
        if tag.startswith("license:"):
            return tag[len("license:"):]
    return "unknown"


def build_pairs(models: List[dict], languages: set) -> Dict[str, dict]:
    # Rank a model id so the preferred variant (tc-big) sorts after the plain one; the loop
    # below always keeps the last (i.e. best) match it sees for a given pair.
    def rank(model_id: str) -> int:
        return 1 if "-tc-big-" in model_id else 0

    best: Dict[str, dict] = {}
    for model in models:
        model_id = model["id"]
        name = model_id.split("/", 1)[-1]
        match = PAIR_RE.match(name)
        if not match:
            continue
        source, target = match.group(1), match.group(2)
        if source not in languages or target not in languages or source == target:
            continue
        key = f"{source}>{target}"
        existing = best.get(key)
        if existing and rank(existing["model_id"]) >= rank(model_id):
            continue
        best[key] = {"model_id": model_id, "license": license_of(model)}
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--languages", help="comma-separated ISO 639-1 codes, default: the built-in core list")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()

    languages = set(args.languages.split(",")) if args.languages else set(DEFAULT_LANGUAGES)

    print(f"Fetching Helsinki-NLP model list...", file=sys.stderr)
    models = fetch_all_models()
    print(f"{len(models)} models total, filtering to {sorted(languages)}", file=sys.stderr)

    pairs = build_pairs(models, languages)
    print(f"{len(pairs)} pairs found", file=sys.stderr)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "languages": sorted(languages),
        "pairs": dict(sorted(pairs.items())),
    }
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
