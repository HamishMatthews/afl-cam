"""Session folder helpers — outputs/YYYYMMDD_HHMMSS/ per recording session."""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import OUTPUTS_DIR

log = logging.getLogger(__name__)


def new_session_dir(now: Optional[datetime] = None) -> Path:
    """Create and return a new session folder: ``outputs/YYYYMMDD_HHMMSS/``.

    If the stamped folder already exists (two sessions started in the same
    second), append ``-1``, ``-2``, ...  The DB stores the authoritative path,
    so a suffixed name is harmless.
    """
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    out = OUTPUTS_DIR / stamp
    suffix = 1
    while out.exists():
        out = OUTPUTS_DIR / f"{stamp}-{suffix}"
        suffix += 1
    out.mkdir(parents=True, exist_ok=True)
    return out


def session_dir_from_created_at(created_at: str) -> Path:
    """Map ``'YYYY-MM-DD HH:MM:SS'`` to ``outputs/YYYYMMDD_HHMMSS`` (no mkdir)."""
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.fromisoformat(created_at)
    return OUTPUTS_DIR / dt.strftime("%Y%m%d_%H%M%S")


def session_dir_is_safe(path: Path) -> bool:
    """True if *path* resolves inside the outputs directory."""
    try:
        path.resolve().relative_to(OUTPUTS_DIR.resolve())
        return True
    except ValueError:
        return False


def remove_session_dir(path: Path):
    """Recursively delete a session folder, refusing anything outside outputs/."""
    if not session_dir_is_safe(path):
        raise ValueError(f"Refusing to delete {path}: outside {OUTPUTS_DIR}")
    if path.exists():
        shutil.rmtree(path)
        log.info("Removed session folder: %s", path)
