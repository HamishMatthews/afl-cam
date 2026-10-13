"""Is the cropped map set built from the live maps as they are now?

Stdlib only (the daemon imports it; the numpy work lives in crop_maps.py).
The crop set records a signature of the live files it came from
(names, sizes, mtimes), so a re-align or a seam refine, which rewrite them,
make it stale without anyone reading 150 MB to find out.
"""

import json
from pathlib import Path
from typing import Optional

SIG_FILE = "source.sig"
MAP_FILES = ("params.bin", "map0.bin", "map1.bin", "w0.bin", "w1.bin")


def signature(live_dir: Path) -> Optional[str]:
    """Identity of a live set, or None when a file is missing."""
    parts = []
    for name in MAP_FILES:
        try:
            st = (Path(live_dir) / name).stat()
        except OSError:
            return None
        parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)


def current(live_dir: Path, crop_dir: Path) -> Optional[dict]:
    """The crop set's record ({sig, rect, canvas}) when it was built from
    `live_dir` as it is now, else None."""
    sig = signature(live_dir)
    try:
        done = json.loads((Path(crop_dir) / SIG_FILE).read_text())
    except (OSError, ValueError):
        return None
    return done if sig is not None and done.get("sig") == sig else None
