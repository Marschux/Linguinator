"""The app as one namespace: every module's names, re-exported for app.server and the tests."""

from .config import *  # noqa: F401,F403
from .langs import *  # noqa: F401,F403
from .models import *  # noqa: F401,F403
from .translation import *  # noqa: F401,F403
from .textdocs import *  # noqa: F401,F403
from .history import *  # noqa: F401,F403
from .jobqueue import *  # noqa: F401,F403
from .ocr import *  # noqa: F401,F403
from .pdf_text import *  # noqa: F401,F403
from .heron import *  # noqa: F401,F403
from .pdf_layout import *  # noqa: F401,F403
from .formats import *  # noqa: F401,F403
from .pdf_scan import *  # noqa: F401,F403
from .pipeline import *  # noqa: F401,F403
from .routes import *  # noqa: F401,F403
from . import config, langs, models, translation, textdocs, history, jobqueue, ocr, pdf_text, heron, pdf_layout, formats, pdf_scan, pipeline, routes  # noqa: F401
