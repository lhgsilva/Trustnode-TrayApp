import json
import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import urllib.request
import urllib.error

router = APIRouter(prefix="/api/ui-source", tags=["ui-source"])


class UISourceConfig(BaseModel):
    mode: Literal["local", "remote", "external"] = "local"
    remote_url: str = ""
    local_path: str = ""


class UISourceTestRequest(BaseModel):
    remote_url: str


class UISourceTestResult(BaseModel):
    ok: bool
    status_code: int | None = None
    message: str


def _get_user_ui_source_path() -> str:
    """Return the persisted ui-source config path.

    Resolution order:
      1. TRUSTNODE_UI_SOURCE_PATH env var (explicit override)
      2. Windows: %APPDATA%/trustnode-edge-desktop/ui-source.json
      3. Linux / cloud VPS: $TRUSTNODE_DATA_DIR/ui-source.json,
         falling back to ~/.trustnode_edge/ui-source.json
    """
    explicit = os.getenv("TRUSTNODE_UI_SOURCE_PATH", "").strip()
    if explicit:
        os.makedirs(os.path.dirname(explicit) or ".", exist_ok=True)
        return explicit

    appdata = os.getenv("APPDATA")
    if appdata:
        target_dir = os.path.join(appdata, "trustnode-edge-desktop")
        os.makedirs(target_dir, exist_ok=True)
        return os.path.join(target_dir, "ui-source.json")

    # Non-Windows path (the cloud VPS). The endpoint was previously
    # raising 500 on every portal page-load and triggering smoke-test
    # alert emails. Resolve to a stable location under the user's
    # trustnode data dir.
    data_dir = os.getenv("TRUSTNODE_DATA_DIR", "").strip()
    if not data_dir:
        data_dir = os.path.join(os.path.expanduser("~"), ".trustnode_edge")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "ui-source.json")


@router.get("/config", response_model=UISourceConfig)
def get_ui_source_config() -> UISourceConfig:
    try:
        path = _get_user_ui_source_path()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not os.path.exists(path):
        return UISourceConfig()

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:  # pragma: no cover - filesystem/runtime safety
        raise HTTPException(status_code=500, detail=f"Failed reading config: {exc}") from exc

    mode = raw.get("mode", "local")
    if mode not in ("local", "remote", "external"):
        mode = "local"

    return UISourceConfig(
        mode=mode,
        remote_url=str(raw.get("remoteUrl", "")).strip(),
        local_path=str(raw.get("localPath", "")).strip(),
    )


@router.put("/config", response_model=UISourceConfig)
def set_ui_source_config(payload: UISourceConfig) -> UISourceConfig:
    if payload.mode == "remote":
        lower = payload.remote_url.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            raise HTTPException(status_code=400, detail="remote_url must start with http:// or https://")
    if payload.mode == "external" and not payload.local_path.strip():
        raise HTTPException(status_code=400, detail="local_path is required for external mode")

    try:
        path = _get_user_ui_source_path()
        doc = {
            "mode": payload.mode,
            "remoteUrl": payload.remote_url.strip(),
            "localPath": payload.local_path.strip(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
    except Exception as exc:  # pragma: no cover - filesystem/runtime safety
        raise HTTPException(status_code=500, detail=f"Failed writing config: {exc}") from exc

    return payload


@router.post("/test", response_model=UISourceTestResult)
def test_ui_source(payload: UISourceTestRequest) -> UISourceTestResult:
    url = payload.remote_url.strip()
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        raise HTTPException(status_code=400, detail="remote_url must start with http:// or https://")

    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "TrustnodeEdge/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = int(resp.status)
            if 200 <= code < 400:
                return UISourceTestResult(ok=True, status_code=code, message=f"Reachable (HTTP {code})")
            return UISourceTestResult(ok=False, status_code=code, message=f"Unexpected HTTP {code}")
    except urllib.error.HTTPError as err:
        return UISourceTestResult(ok=False, status_code=int(err.code), message=f"HTTP error {err.code}")
    except Exception as err:  # pragma: no cover - runtime network conditions
        return UISourceTestResult(ok=False, status_code=None, message=f"Connection failed: {err}")
