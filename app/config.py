"""Settings read from the environment, and the constants the rest of the app shares."""
import os
import re
from pathlib import Path
from typing import Optional
from langdetect import DetectorFactory


DetectorFactory.seed = 0  # deterministic detection results across runs
# Xet transfers assemble a file elsewhere and drop it into the cache in one piece at the very
# end, which leaves report_model_download nothing to measure until the download is over.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def env_value(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Not configurable: another model can carry another licence, and other model families expect
# other language tokens, which does not fail, it translates into the wrong language.
FALLBACK_MODEL_ID = "Helsinki-NLP/opus-mt-tc-bible-big-mul-mul"
# One model resident at a time: each costs 0.5-1 GB, and the queue runs one job anyway.
MODEL_CACHE_SIZE = 1
# Characters per chunk. Not a model limit, every chunk is split into single sentences before
# translation; this bounds how many of them land in one batched call, and with it peak memory.
MAX_CHARS = 2000
MAX_FILE_MB = int(env_value("LINGUINATOR_MAX_FILE_MB", "50"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = MAX_FILE_BYTES * 10
# In hours, because a day is a coarse setting for a workbench whose history is a convenience,
# not an archive. LINGUINATOR_HISTORY_DAYS is still read where the new one is unset, so an
# existing .env does not silently start meaning something else. "none" keeps history for good.
HISTORY_HOURS: Optional[int] = None
if env_value("LINGUINATOR_HISTORY_HOURS", "").strip().lower() != "none":
    HISTORY_HOURS = int(env_value("LINGUINATOR_HISTORY_HOURS", "")
                        or int(env_value("LINGUINATOR_HISTORY_DAYS", "0") or 0) * 24
                        or 24)
# Fixed, because the compose volume is mounted here: a different path would write into the
# container filesystem and be gone with the next restart.
HISTORY_DIR = Path("/data/history")
JOBS_DIR = HISTORY_DIR / "jobs"
HISTORY_TIMEZONE = env_value("LINGUINATOR_TIMEZONE", "Europe/Berlin")
# "12h" or "24h", the same for every UI language. Anything else falls back to 24h rather than
# failing, so an outdated .env (this used to accept "auto") does not stop the container.
TIME_FORMAT = "12h" if env_value("LINGUINATOR_TIME_FORMAT", "24h").strip() == "12h" else "24h"
# One job at a time: two jobs on different language pairs would each want their own model.
JOB_WORKERS = 1


def container_cpu_limit(path: str = "/sys/fs/cgroup/cpu.max") -> int:
    """How many CPUs the container's cgroup quota allows, 0 when it sets none.

    The translator otherwise starts one thread per core it can see, and a container limited to fewer
    (`--cpus 4` on a 6-core host) gets throttled for running more: measured 119 s against 81 s
    for the same 206 sentences once the threads matched the quota.
    """
    # ponytail: cgroup v2 only; a v1 host keeps the library default as before.
    try:
        quota, period = open(path, encoding="utf-8").read().split()[:2]
    except (OSError, ValueError):
        return 0
    if quota == "max":
        return 0
    return max(1, min(os.cpu_count() or 1, -(-int(quota) // int(period))))


CPU_THREADS = int(env_value("LINGUINATOR_CPU_THREADS", "0")) or container_cpu_limit()
# What the UI starts with; a browser that has been switched keeps its own choice.
UI_LANGUAGE = env_value("LINGUINATOR_UI_LANGUAGE", "en")
# Where detection lands when it fails. Not the UI's preselection, which is auto-detect.
DEFAULT_SOURCE = "eng_Latn"
DEFAULT_TARGET = env_value("LINGUINATOR_DEFAULT_TARGET", "eng_Latn")
AUTH_ENABLED = env_value("LINGUINATOR_AUTH_ENABLED", "false").lower() in ("1", "true", "yes", "on")
AUTH_USERNAME = env_value("LINGUINATOR_AUTH_USERNAME", "Translator")
AUTH_PASSWORD = env_value("LINGUINATOR_AUTH_PASSWORD", "")
MODEL_IDLE_SECONDS = int(env_value("LINGUINATOR_MODEL_IDLE_SECONDS", "600"))
PDF_LOW_TEXT_CHARS = 20
PDF_PAGE_WIDTH = 595
PDF_PAGE_HEIGHT = 842
PDF_MARGIN = 54
PDF_LINE_HEIGHT = 14
PDF_FONT_SIZE = 11
PDF_HEADING_FONT_SIZE = 15
PDF_FOOTER_FONT_SIZE = 9
# Below this, an image is a bullet icon or a decorative divider, not something worth reinserting
# into the re-exported translation or reserving text-wrap space for.
PDF_IMAGE_MIN_SIZE = 24.0
# A gap narrower than this cannot hold a real line of text - text falls back to full width below
# the image instead of one word per line squeezed beside it.
PDF_IMAGE_MIN_TEXT_BAND = 80.0
# How far the overlay may shrink the font to make a longer translation fit its original lines.
PDF_LAYOUT_MIN_SCALE = 0.7
# Kept clear of the page edge when a paragraph has nothing to its right.
PDF_LAYOUT_EDGE_MARGIN = 20.0
# How far the first line of a paragraph may be indented past the ones under it and still count
# as part of it. Half an inch is the usual tab; a centred heading sits much further in than that
# and has to stay a paragraph of its own.
PDF_LAYOUT_MAX_INDENT = 40.0
# How far a word gap may be stretched to justify a line, as a multiple of the font's own space.
# Re-measured 14.08.2026 (the previous value, 3.0, came apart in production at 2.95 on
# Mixed_Languages.pdf): read back after drawing, a line set word by word breaks into one span per
# word at a strikingly constant factor of 2.88, independent of font size - swept 6pt to 32pt in
# 0.01 steps against real German words, same breakpoint every time to two decimals, so this is
# MuPDF's own span-grouping heuristic and not a per-size effect the old "about 3.9" measurement
# missed. Wider gaps also tear holes into the setting, so a line that would need more stays ragged.
PDF_JUSTIFY_MAX_SPACE = 2.8
# A line opening with one of these is a list item of its own, however it is placed.
PDF_LIST_MARKER = re.compile(r"^\s*(?:[-•‣▪●◦*]|\(?\d{1,3}[.)])\s")
# How far a paragraph may be tightened purely to keep the original's line count. 0.9 because no
# paragraph in the test documents needed more than that to absorb the substitute font's extra
# width; past it, an extra line is the lesser evil.
PDF_LAYOUT_TIGHTEN_SCALE = 0.9
# The same, for a paragraph with no room below it for even one more line - a table cell being the
# usual one. There the extra line does not land in a gap, it lands outside the box, so fitting the
# original line count is worth more type size than elsewhere. As low as
# PDF_LAYOUT_CROWDED_MIN_SCALE for the same reason it is: a line outside its cell is the same
# fault as a line on top of the next paragraph. At 0.6 the header cell of Powerupall's research
# table ("im Zusammenhang mit bestimmten anderen Faktoren") stopped one step short of fitting and
# hung its last word under the table, through the rule below it.
PDF_LAYOUT_BOXED_MIN_SCALE = 0.5
# And for a paragraph that would otherwise be drawn on top of the one below it. Lower than either
# of the above, because the alternative is not a cramped page but unreadable text: measured over
# the Powerupall pages reported as colliding, the four paragraphs that still ran into their
# neighbour at the ordinary minimum needed 0.54 to 0.64 to clear it.
PDF_LAYOUT_CROWDED_MIN_SCALE = 0.5
# A hard floor under both minimums above, in points rather than as a fraction of the base size.
# level_table_sizes joins a whole table into one group through its rows and columns, so on a wide
# table (Landscape_Mixed_Pages, 10 columns by 9 rows) a single cramped cell - one long word in a
# narrow column, crowded by the row below - drags every other cell down with it: measured, an 8.5pt
# table came back at a uniform 4.2pt, technically consistent but no longer legible. Below this, a
# cell overflows its row instead of shrinking further - recoverable by the reader, where six-point
# text across a whole table is not.
PDF_LAYOUT_ABSOLUTE_MIN_SIZE = 6.0
# A reflow taller than this multiple of the paragraph it replaces is not laid out at all; the
# original stays instead. Overflowing by a line or two is normal (German runs longer, and the
# substitute font wider), a block three times the height is the model having invented text, and
# it lands on top of everything below it.
PDF_LAYOUT_MAX_LINE_GROWTH = 3
# How much of a paragraph's own width a shape has to span before paragraph_width_limit accepts it
# as the box the paragraph sits in. Measured (Aug 2026) over both documents that depend on this:
# the icons and decorations Get_Started_With_Smallpdf mistook for boxes span 0.07 to 0.38 of their
# paragraph, the real table cells of Systemrequirements 0.74 (the "Any graphic card..." line, whose
# original overruns its own cell) and upwards. The gap between those two is where this sits.
PDF_LAYOUT_WALL_MIN_SPAN = 0.55
# How far a line may sit off a centre and still count as set around it, as a fraction of the text
# column's width. Measured (Aug 2026) on Powerupall, whose text column runs 65..551: every centred
# heading and page number on it sits within 2pt of the column's centre (0.4 %), while the nearest
# thing that must not be mistaken for one, a left-aligned list item, is 120pt off (25 %).
PDF_LAYOUT_CENTRE_TOLERANCE = 0.03
# Layout-PDF paragraphs are usually short, so translating one per model call wastes most of
# each call on fixed beam-search overhead. Batched via the tensor's batch dimension (not string
# concatenation), so paragraph boundaries stay exact; kept small to cap the extra padding memory
# a batch costs over a single call.
PDF_LAYOUT_BATCH_SIZE = 4

if CPU_THREADS > 0:
    os.environ.setdefault("OMP_NUM_THREADS", str(CPU_THREADS))
    os.environ.setdefault("MKL_NUM_THREADS", str(CPU_THREADS))

APP_DIR = Path(__file__).resolve().parent


def normalized_root_path(value: str) -> str:
    path = value.strip().strip("/")
    return f"/{path}" if path else ""


ROOT_PATH = normalized_root_path(os.getenv("LINGUINATOR_ROOT_PATH", ""))
