# -*- coding: utf-8 -*-
"""Every `from <module> import <name>` names something that exists.

2026-10-20. Both report-template mirror paths in reports_store.py began:

    from .app_store import app_store   # local import to avoid a cycle

`app.services.app_store` defines the CLASS `AppStore`; the singleton is
built in `app.state`. So every call raised ImportError on its FIRST line,
and both callers wrapped it in `except Exception`. Supabase held ONE
report_template while two edges held 15 and 269, Lite's Templates tab was
empty, and nothing anywhere said why. It was read as a sync-policy problem
for months; it was an import that had never once succeeded.

A lazy import inside a function is invisible to every other check we run:
it is not executed at import time, so a smoke test that boots the app never
touches it, and it reads exactly like the dozens of correct late imports
around it. Static resolution is the only cheap way to see it.

Deliberately conservative - it resolves only first-party `app.*` and
relative imports, treats conditionally-defined names as defined, and allows
a submodule imported from a package. It is meant to have no false alarms,
so that one line of output always means a real broken import.

Read-only: parses source, imports nothing, touches no store.
"""
from __future__ import annotations

import ast
import os
import sys
import warnings

# Parsing other people's source raises SyntaxWarning for things like a
# Windows path in a non-raw string. Not our business here, and it buries
# the one line that matters.
warnings.filterwarnings("ignore", category=SyntaxWarning)

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "backend", "app")


def module_path(mod: str, from_file: str, level: int) -> str | None:
    if level:
        base = os.path.dirname(from_file)
        for _ in range(level - 1):
            base = os.path.dirname(base)
        parts = mod.split(".") if mod else []
    else:
        if not mod.startswith("app"):
            return None
        base = os.path.dirname(APP)
        parts = mod.split(".")
    p = os.path.join(base, *parts)
    if os.path.isfile(p + ".py"):
        return p + ".py"
    if os.path.isfile(os.path.join(p, "__init__.py")):
        return os.path.join(p, "__init__.py")
    return None


def toplevel_names(path: str) -> set[str] | None:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except Exception:
        return None
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, (ast.If, ast.Try)):
            # conditional definitions: be permissive, collect anything bound
            for sub in ast.walk(n):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
    return names


bad = []
for dirpath, _d, files in os.walk(APP):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        f = os.path.join(dirpath, fn)
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            tgt = module_path(node.module or "", f, node.level or 0)
            if not tgt:
                continue
            have = toplevel_names(tgt)
            if have is None:
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                if a.name not in have:
                    # could be a submodule of a package
                    pkg_dir = os.path.dirname(tgt)
                    if os.path.basename(tgt) == "__init__.py" and (
                        os.path.isfile(os.path.join(pkg_dir, a.name + ".py"))
                        or os.path.isdir(os.path.join(pkg_dir, a.name))
                    ):
                        continue
                    bad.append((os.path.relpath(f, ROOT), node.lineno,
                                node.module or ("." * (node.level or 0)),
                                a.name))

print("TrustNode - every from-import names something that exists")
print()
for rel, line, mod, name in sorted(bad):
    print("  %s:%d  from %s import %s" % (rel, line, mod, name))
print("  {0:58s}: {1}{2}".format(
    "every from-import in backend/app resolves",
    "PASS" if not bad else "FAIL",
    "" if not bad else
    " - %d import(s) name something that does not exist. Inside a "
    "function this raises only when the line runs, and an `except "
    "Exception` around it turns the feature off in silence." % len(bad)))
print()
print("RESULT: {0}".format("PASS" if not bad else "FAIL"))
sys.exit(0 if not bad else 2)
