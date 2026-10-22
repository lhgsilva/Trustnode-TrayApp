# -*- coding: utf-8 -*-
"""Run the first-window safety guard (Node) from the Python release gate.

The rule lives in desktop/main.js and is JavaScript, so the check itself
is test_first_window_safety.mjs. The gate runs `sys.executable <script>`
for every source check, so this wrapper is what puts it in the gate.

A missing node FAILS rather than passing quietly: a check that silently
skips is how a startup defect reaches a customer's new machine.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "test_first_window_safety.mjs")

node = shutil.which("node")
if not node:
    print("node is not on PATH - cannot run the first-window safety guard.")
    print("RESULT: FAIL - the check could not run (a skipped check is not a pass)")
    sys.exit(2)
if not os.path.exists(SCRIPT):
    print("missing %s" % SCRIPT)
    print("RESULT: FAIL")
    sys.exit(2)

sys.exit(subprocess.call([node, SCRIPT]))
