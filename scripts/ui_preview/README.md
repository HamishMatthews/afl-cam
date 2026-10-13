# UI preview

Mock rig for reviewing `afl_cam/web/index.html` without the board.

    python3 scripts/ui_preview/build.py            # -> build/ui-preview.html
    NODE_PATH=... node scripts/ui_preview/shots.js build/shots

Open the HTML in a browser and use the scenario bar. See docs/ui.md.
