# -*- coding: utf-8 -*-
"""Default downtime and quality reasons, seeded once.

A downtime popup with an empty reason list is worse than no popup: the operator
confirms nothing and every stop lands as "Unknown". These are the categories
from the spec, seeded idempotently on first boot and never re-seeded once the
site has edited them.
"""
from __future__ import annotations

from typing import Any, Dict, List

# (category, reason, is_planned)
DEFAULT_DOWNTIME_REASONS: List[tuple] = [
    ("Equipment failure", "Mechanical breakdown", False),
    ("Equipment failure", "Machine jam", False),
    ("Electrical fault", "Drive fault", False),
    ("Electrical fault", "Power loss", False),
    ("Mechanical fault", "Tool wear", False),
    ("Mechanical fault", "Belt or chain fault", False),
    ("Sensor fault", "Sensor not detecting", False),
    ("Waiting for material", "No raw material", False),
    ("Waiting for material", "Upstream machine stopped", False),
    ("Waiting for operator", "No operator available", False),
    ("Changeover", "Product changeover", True),
    ("Cleaning", "Scheduled cleaning", True),
    ("Maintenance", "Planned maintenance", True),
    ("Maintenance", "Unplanned maintenance", False),
    ("Quality issue", "Out-of-specification product", False),
    ("Planned stop", "Break", True),
    ("Planned stop", "No production planned", True),
    ("Energy waste", "Machine left powered", False),
    ("Unknown", "Unknown", False),
]

# (category, reason)
DEFAULT_QUALITY_REASONS: List[tuple] = [
    ("Machine defect", "Dimensional out of tolerance"),
    ("Machine defect", "Surface defect"),
    ("Material defect", "Raw material out of specification"),
    ("Material defect", "Contamination"),
    ("Operator error", "Incorrect setup"),
    ("Operator error", "Handling damage"),
    ("Process issue", "Process drift"),
    ("Process issue", "Temperature deviation"),
    ("Startup scrap", "Startup scrap"),
    ("Changeover scrap", "Changeover scrap"),
    ("Unknown", "Unknown"),
]

# A machine with no power rules cannot use the power path at all, so a new
# machine that enables power monitoring gets this starter set. The numbers are
# deliberately conservative and MUST be tuned per machine - different machines
# draw very different power, which is why the rules are per-machine rows and
# not global constants.
DEFAULT_POWER_RULES: List[Dict[str, Any]] = [
    {"name": "Off", "measurement": "power_kw", "min_value": None,
     "max_value": 0.5, "min_duration_s": 120, "generated_status": "off",
     "priority": 10},
    {"name": "Idle", "measurement": "power_kw", "min_value": 0.5,
     "max_value": 3.0, "min_duration_s": 180, "generated_status": "idle",
     "priority": 20},
    {"name": "Production", "measurement": "power_kw", "min_value": 3.0,
     "max_value": None, "min_duration_s": 60, "generated_status": "production",
     "priority": 30},
    {"name": "Energy waste (powered, not producing)", "measurement": "power_kw",
     "min_value": 2.0, "max_value": None, "min_duration_s": 300,
     "generated_status": "energy_waste", "requires_no_count": 1, "priority": 5},
]


def seed_defaults(store: Any) -> Dict[str, int]:
    """Insert the default reason lists if the tables are empty.

    Only seeds an EMPTY table. A site that deleted a reason on purpose must not
    have it reappear on the next restart.
    """
    added = {"downtime_reasons": 0, "quality_reasons": 0}

    existing = store.list_entities("downtime_reasons")
    if not existing:
        for order, (category, reason, planned) in enumerate(DEFAULT_DOWNTIME_REASONS):
            store.save_entity("downtime_reasons", {
                "category": category, "reason": reason,
                "is_planned": bool(planned), "enabled": True,
                "sort_order": (order + 1) * 10,
            }, actor="system")
            added["downtime_reasons"] += 1

    existing_q = store.list_entities("quality_reasons")
    if not existing_q:
        for order, (category, reason) in enumerate(DEFAULT_QUALITY_REASONS):
            store.save_entity("quality_reasons", {
                "category": category, "reason": reason, "enabled": True,
                "sort_order": (order + 1) * 10,
            }, actor="system")
            added["quality_reasons"] += 1

    return added


def seed_power_rules_for_machine(store: Any, machine_id: str) -> int:
    """Starter power rules for one machine, only when it has none."""
    if store.list_entities("power_state_rules", machine_id=machine_id):
        return 0
    n = 0
    for rule in DEFAULT_POWER_RULES:
        payload = dict(rule)
        payload["machine_id"] = machine_id
        payload["enabled"] = True
        store.save_entity("power_state_rules", payload, actor="system")
        n += 1
    return n
