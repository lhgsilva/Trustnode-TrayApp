from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from typing import Any


EXPLICIT_NODE_ID_RE = re.compile(
    r'ns=\d+;(?:s="[^"]+"|s=[^,\n;|]+|i=\d+|g=[0-9a-fA-F-]+|b=[^,\n;|]+)'
)


@dataclass
class OpcResolvedTarget:
    requested: str
    resolved_node_id: str
    matched_by: str


def is_explicit_node_id(value: str) -> bool:
    return bool(EXPLICIT_NODE_ID_RE.fullmatch(str(value or "").strip()))


def split_requested_identifiers(values: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        explicit = [m.group(0).strip() for m in EXPLICIT_NODE_ID_RE.finditer(text)]
        if explicit:
            for node_id in explicit:
                if node_id not in out:
                    out.append(node_id)
            continue
        for token in re.split(r"[,;\n|]+", text):
            token_txt = token.strip()
            if token_txt and token_txt not in out:
                out.append(token_txt)
    return out


def _normalize_text(value: str) -> str:
    txt = str(value or "").strip().strip('"').strip("'").lower()
    txt = txt.replace("\\", "/")
    txt = re.sub(r"\s+", " ", txt)
    return txt


def _opc_node_class_name(node_class: object) -> str:
    try:
        name = getattr(node_class, "name", None)
        if name:
            return str(name)
    except Exception:
        pass
    try:
        txt = str(node_class)
        if txt and not txt.isdigit():
            return txt.split(".")[-1]
        num = int(node_class)  # type: ignore[arg-type]
        mapping = {
            1: "Object",
            2: "Variable",
            4: "Method",
            8: "ObjectType",
            16: "VariableType",
            32: "ReferenceType",
            64: "DataType",
            128: "View",
        }
        return mapping.get(num, str(num))
    except Exception:
        return "Unknown"


def _match_keys_for_display(display_name: str, browse_name: str, browse_path: str) -> list[str]:
    keys = {
        _normalize_text(display_name),
        _normalize_text(browse_name),
        _normalize_text(browse_path),
        _normalize_text(browse_path.replace("/", ".")),
    }
    return [k for k in keys if k]


def _build_variable_index(client: Any, max_nodes: int = 12000, max_depth: int = 20) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    from opcua import ua  # type: ignore

    try:
        root = client.get_node(ua.ObjectIds.ObjectsFolder)
    except Exception:
        root = client.get_root_node()

    key_map: dict[str, list[str]] = {}
    variables: list[dict[str, str]] = []
    seen: set[str] = set()
    queue = deque([(root, 0, "")])

    while queue and len(seen) < max_nodes:
        node, depth, parent_path = queue.popleft()
        if depth > max_depth:
            continue
        try:
            node_id = node.nodeid.to_string()
        except Exception:
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        try:
            node_class = _opc_node_class_name(node.get_node_class())
        except Exception:
            node_class = "Unknown"
        try:
            browse_obj = node.get_browse_name()
            browse_name = str(getattr(browse_obj, "Name", "") or "")
        except Exception:
            browse_name = ""
        try:
            display_obj = node.get_display_name()
            display_name = str(getattr(display_obj, "Text", "") or "")
        except Exception:
            display_name = ""
        next_path = f"{parent_path}/{browse_name}" if parent_path and browse_name else (browse_name or parent_path)

        if node_class.endswith("Variable"):
            variables.append(
                {
                    "node_id": node_id,
                    "display_name": display_name,
                    "browse_name": browse_name,
                    "browse_path": next_path,
                }
            )
            for key in _match_keys_for_display(display_name, browse_name, next_path):
                key_map.setdefault(key, [])
                if node_id not in key_map[key]:
                    key_map[key].append(node_id)

        if depth < max_depth:
            try:
                children = node.get_children()
            except Exception:
                children = []
            for child in children:
                queue.append((child, depth + 1, next_path))

    return key_map, variables


def resolve_requested_nodes(client: Any, requested_identifiers: list[str]) -> tuple[list[OpcResolvedTarget], list[str]]:
    requested = split_requested_identifiers(requested_identifiers)
    if not requested:
        return [], []

    needs_lookup = [item for item in requested if not is_explicit_node_id(item)]
    lookup_map: dict[str, list[str]] = {}
    if needs_lookup:
        lookup_map, _ = _build_variable_index(client)

    resolved: list[OpcResolvedTarget] = []
    unresolved: list[str] = []
    seen_ids: set[str] = set()

    for item in requested:
        if is_explicit_node_id(item):
            if item not in seen_ids:
                resolved.append(OpcResolvedTarget(requested=item, resolved_node_id=item, matched_by="explicit"))
                seen_ids.add(item)
            continue

        chosen: str | None = None
        matched_by = "unknown"
        key = _normalize_text(item)
        candidates = lookup_map.get(key, [])
        if candidates:
            chosen = candidates[0]
            matched_by = "browse_lookup"
        else:
            # Last fallback: treat token as provided NodeId format for servers that accept it.
            try:
                node = client.get_node(item)
                node_class = str(node.get_node_class()).split(".")[-1]
                if node_class.endswith("Variable"):
                    chosen = node.nodeid.to_string()
                    matched_by = "direct_get_node"
            except Exception:
                chosen = None

        if chosen:
            if chosen not in seen_ids:
                resolved.append(OpcResolvedTarget(requested=item, resolved_node_id=chosen, matched_by=matched_by))
                seen_ids.add(chosen)
        else:
            unresolved.append(item)

    return resolved, unresolved
