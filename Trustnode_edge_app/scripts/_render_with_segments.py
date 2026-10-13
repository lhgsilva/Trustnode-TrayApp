# -*- coding: utf-8 -*-
"""Render a template the way a CYCLE-TRIGGERED run does, and print the path.

Used by smoke_tcu_test_report.py. It runs as its own process so the renderer
picks up the caller's scratch TRUSTNODE_DATA_DIR / TRUSTNODE_APP_STORE_PATH -
importing app.state migrates whatever database the environment points at, so
this must never inherit a live one.

    python _render_with_segments.py <template.json> <segments.json> <from> <to>
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.services.report_renderer import render_template_to_pdf  # noqa: E402


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: _render_with_segments.py <template.json> <segments.json> <from> <to>")
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        template = json.load(fh)
    with open(sys.argv[2], encoding="utf-8") as fh:
        segments = json.load(fh)
    path, _bytes, _sha = render_template_to_pdf(
        template, window=(sys.argv[3], sys.argv[4]), segments=segments)
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
