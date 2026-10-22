# -*- coding: utf-8 -*-
"""Run the portal read-only guard (Node) from the Python release gate.

The rule being guarded lives in frontend/src/api.js and is JavaScript, so
the check itself is test_portal_not_readonly.mjs. The release gate runs
`sys.executable <script>` for every source check, so this thin wrapper is
what makes that check part of the gate.

If node is not on PATH the wrapper FAILS rather than passing quietly - a
check that silently skips is exactly the kind of thing that let the
portal ship read-only in the first place.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "test_portal_not_readonly.mjs")

node = shutil.which("node")
if not node:
    print("node is not on PATH - cannot run the portal read-only guard.")
    print("RESULT: FAIL - the check could not run (a skipped check is not a pass)")
    sys.exit(2)

if not os.path.exists(SCRIPT):
    print("missing %s" % SCRIPT)
    print("RESULT: FAIL")
    sys.exit(2)

sys.exit(subprocess.call([node, SCRIPT]))
