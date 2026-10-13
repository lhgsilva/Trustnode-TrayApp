# -*- coding: utf-8 -*-
"""Render the cycle-validation report inside a scratch workspace.

Invoked by validate_plc_cycle_report.py as its own process, so the renderer
imports app.state against the SCRATCH store. Importing it migrates whatever
database the environment points at, which must never be the live one.

Emits machine-readable lines the caller turns into checks:
    CHECK PASS <name> | <detail>
    PDF <path>
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.services.report_renderer import (          # noqa: E402
    render_template_to_pdf, resolve_trigger_window)


def emit(name: str, ok: bool, detail: str = "") -> None:
    print("CHECK %s %s | %s" % ("PASS" if ok else "FAIL", name, detail))


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)

    gw = spec["gateway"]
    cycle = spec.get("cycle") or []
    inner = spec.get("inner") or []
    numeric = spec.get("numeric") or []

    if cycle:
        main_from, main_to = cycle[0], cycle[1]
        emit("a complete cycle was available to report on", True,
             "%s -> %s" % (main_from, main_to))
    else:
        # No cycle has been run yet. Still render over the data that IS there,
        # so the operator can see real values in a real report - the cycle
        # sections simply have nothing to show.
        main_from, main_to = spec.get("span") or ["", ""]
        emit("a complete cycle was available to report on", False,
             "no start/end pair yet - reporting over the collected span instead")
        if not main_from:
            return 2

    # What a report SECTION does: find its window from the tag conditions.
    main_win = cycle and resolve_trigger_window(
        {"start": {"gateway_id": gw, "tag_name": spec["start_tag"],
                   "operator": "!=", "value": 0},
         "stop": {"gateway_id": gw, "tag_name": spec["end_tag"],
                  "operator": "!=", "value": 0}},
        main_from, main_to) or ("", "")
    if cycle:
        emit("the main window is found from the bits themselves",
             bool(main_win[0]) and not main_win[0].startswith("9999"),
             "%s -> %s" % main_win)

    sections = [
        {"type": "header", "title": "PLC cycle validation",
         "subtitle": "main start/end trigger with inner triggers",
         "show_generated_at": True},
        {"type": "static_table", "title": "Cycle",
         "header": ["What", "Value"],
         "rows": [["Gateway", gw], ["Start tag", spec["start_tag"]],
                  ["End tag", spec["end_tag"]],
                  ["Window", "%s -> %s" % (main_from, main_to)]]},
    ]

    window = {"preset": "custom", "from_utc": main_from, "to_utc": main_to}
    for tag in numeric[:2]:
        sections.append({
            "type": "line_chart", "title": "%s - whole cycle" % tag,
            "time_range": dict(window), "x_axis_label": "Time",
            "y_axis_label": tag,
            "series": [{"id": tag, "label": tag, "gateway_id": gw, "tag_name": tag}],
        })

    ok_inner = 0
    for item in inner:
        trig = {"start": {"gateway_id": gw, "tag_name": item["start"],
                          "operator": "!=", "value": 0},
                "stop": {"gateway_id": gw, "tag_name": item["end"],
                         "operator": "!=", "value": 0}}
        win = resolve_trigger_window(trig, main_from, main_to)
        inside = (bool(win[0]) and not win[0].startswith("9999")
                  and main_from <= win[0] <= main_to)
        emit("  inner trigger %d resolves inside the cycle" % item["n"], inside,
             "%s -> %s" % win)
        if inside:
            ok_inner += 1
        if numeric:
            tag = numeric[0]
            sections.append({
                "type": "line_chart",
                "title": "%s - inner cycle %d" % (tag, item["n"]),
                "time_range": dict(window), "trigger": trig,
                "side_by_side": item["n"] > 1,
                "x_axis_label": "Time", "y_axis_label": tag,
                "series": [{"id": tag, "label": tag, "gateway_id": gw,
                            "tag_name": tag}],
            })

    emit("every inner trigger resolved", ok_inner == len(inner) and bool(inner),
         "%d of %d" % (ok_inner, len(inner)))

    sections.append({
        "type": "table", "title": "Readings", "time_range": dict(window),
        "row_limit": 40,
        "series": [{"id": t, "label": t, "gateway_id": gw, "tag_name": t}
                   for t in numeric[:3]],
        "columns": ([{"key": "ts", "title": "Time"}] +
                    [{"key": "value", "series_id": t, "title": t}
                     for t in numeric[:3]]),
    })

    try:
        path, nbytes, _sha = render_template_to_pdf(
            {"name": "PLC cycle validation", "definition": {"sections": sections}},
            window=(main_from, main_to))
    except Exception as exc:                            # noqa: BLE001
        emit("the report rendered", False, str(exc)[:160])
        return 2
    emit("the report rendered", nbytes > 10000, "%d bytes" % nbytes)
    print("PDF %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
