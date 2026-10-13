"""Build a self-contained UI preview: the phone page with a mock rig injected.

    python3 scripts/ui_preview/build.py [out.html]

The result runs from file:// or any static host, with no daemon, and has a
scenario bar (ready, recording, low fps, crop, separate, error, ...). The
mode and crop toggles and the record button also work, so a reviewer can
exercise the preview state logic. See scripts/ui_preview/README.md.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
page = (ROOT / "afl_cam/web/index.html").read_text()
mock = (Path(__file__).parent / "mock.js").read_text()
page = page.replace('<script src="/hls.min.js"></script>\n', "")
page = page.replace("</body>", f"<script>\n{mock}\n</script>\n</body>")
out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "ui-preview.html"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(page)
print(out)
