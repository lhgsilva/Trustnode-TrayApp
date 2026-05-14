#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = os.environ.get("TN_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TENANT_ID = os.environ.get("TN_TENANT_ID", "default")
ADMIN_USER = os.environ.get("TN_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("TN_ADMIN_PASS", "admin")
OUT_DIR = os.environ.get("TN_SMOKE_REPORT_DIR", os.path.join("tests", "reports"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def req(session: requests.Session, method: str, path: str, **kwargs) -> requests.Response:
    url = f"{BASE_URL}{path}"
    for attempt in range(1, 6):
        try:
            return session.request(method, url, timeout=25, **kwargs)
        except requests.RequestException:
            if attempt == 5:
                raise
            time.sleep(0.7 * attempt)
    raise RuntimeError("unreachable")


def ok(response: requests.Response, label: str) -> dict:
    if response.status_code >= 400:
        try:
            detail = json.dumps(response.json())
        except Exception:
            detail = response.text
        raise RuntimeError(f"{label} failed ({response.status_code}): {detail}")
    return response.json() if response.text else {}


def bool_nonempty(value) -> bool:
    return bool(str(value or "").strip())


def main() -> None:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    customer_id = f"smk-login-cust-{run_id}"
    edge_id = f"smk-login-edge-{run_id}"
    edge_name = f"SMK Login Edge {run_id}"
    license_id = f"smk-login-lic-{run_id}"
    edge_admin_user = f"edgeadmin_{uuid.uuid4().hex[:6]}"
    edge_admin_pass = "Admin#12345"

    report = {
        "run_id": run_id,
        "started_utc": now_iso(),
        "base_url": BASE_URL,
        "tenant_id": TENANT_ID,
        "customer_id": customer_id,
        "edge_id": edge_id,
        "license_id": license_id,
        "edge_admin_user": edge_admin_user,
        "steps": [],
        "ok": False,
    }

    session = requests.Session()
    login = ok(
        req(session, "POST", "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}),
        "admin login",
    )
    token = str(login.get("token") or login.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("admin login token missing")
    session.headers.update({"Authorization": f"Bearer {token}"})
    report["steps"].append({"step": "admin_login", "ok": True})

    modules_cat = ok(req(session, "GET", "/api/control-plane/modules"), "module catalog")
    module_rows = modules_cat.get("modules") if isinstance(modules_cat.get("modules"), list) else []
    modules = [
        {"module_key": str(row.get("key") or "").strip(), "enabled": True}
        for row in module_rows
        if bool_nonempty(row.get("key"))
    ]
    if not modules:
        raise RuntimeError("module catalog empty")

    ok(
        req(
            session,
            "POST",
            f"/api/control-plane/customers?tenant_id={TENANT_ID}",
            json={
                "customer_id": customer_id,
                "company_name": f"Smoke Login Customer {run_id}",
                "contact_email": f"smoke-login+{run_id}@example.com",
                "status": "active",
                "metadata": {"source": "smoke_login_activation_flow"},
            },
        ),
        "create customer",
    )
    ok(
        req(
            session,
            "POST",
            f"/api/control-plane/edges?tenant_id={TENANT_ID}",
            json={
                "edge_id": edge_id,
                "edge_name": edge_name,
                "customer_id": customer_id,
                "site": "Smoke Site",
                "area": "Line A",
                "equipment": "Smoke Eq",
                "status": "active",
                "metadata": {"source": "smoke_login_activation_flow"},
            },
        ),
        "create edge",
    )

    start_dt = datetime.now(timezone.utc)
    end_dt = start_dt + timedelta(days=365)
    ok(
        req(
            session,
            "POST",
            f"/api/control-plane/licenses?tenant_id={TENANT_ID}",
            json={
                "license_id": license_id,
                "customer_id": customer_id,
                "plan_code": "standard",
                "status": "active",
                "start_utc": start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "end_utc": end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "max_edges": 3,
                "max_users": 10,
                "metadata": {"source": "smoke_login_activation_flow"},
            },
        ),
        "create license",
    )
    ok(
        req(
            session,
            "PUT",
            f"/api/control-plane/licenses/{license_id}/modules",
            json={"modules": modules},
        ),
        "set license modules",
    )

    issued = ok(
        req(
            session,
            "POST",
            f"/api/control-plane/activation-code/issue?tenant_id={TENANT_ID}",
            json={
                "customer_id": customer_id,
                "edge_id": edge_id,
                "license_id": license_id,
                "edge_name": edge_name,
                "ttl_minutes": 180,
                "metadata": {"source": "smoke_login_activation_flow"},
            },
        ),
        "issue activation code",
    )
    activation_code = str(((issued.get("row") or {}).get("activation_code") or "")).strip()
    if not activation_code:
        raise RuntimeError("activation code missing")

    register = ok(
        req(
            session,
            "POST",
            "/api/control-plane/edge-link/register",
            json={
                "activation_code": activation_code,
                "edge_id": edge_id,
                "edge_name": edge_name,
                "site": "Smoke Site",
                "area": "Line A",
                "equipment": "Smoke Eq",
                "admin_username": edge_admin_user,
                "admin_password": edge_admin_pass,
            },
        ),
        "edge-link register",
    )

    reg_license = register.get("license") if isinstance(register.get("license"), dict) else {}
    finalize = ok(
        req(
            session,
            "POST",
            "/api/control-plane/edge-link/local-finalize",
            json={
                "tenant_id": str(register.get("tenant_id") or TENANT_ID),
                "edge_id": str(register.get("edge_id") or edge_id),
                "edge_name": str(register.get("edge_name") or edge_name),
                "customer_id": str(register.get("customer_id") or customer_id),
                "license_id": str(register.get("license_id") or license_id),
                "license_status": str(reg_license.get("status") or "active"),
                "license_plan_code": str(reg_license.get("plan_code") or "standard"),
                "license_start_utc": str(reg_license.get("start_utc") or ""),
                "license_end_utc": str(reg_license.get("end_utc") or ""),
                "license_max_edges": int(reg_license.get("max_edges") or 0),
                "license_max_users": int(reg_license.get("max_users") or 0),
                "license_modules": reg_license.get("modules") if isinstance(reg_license.get("modules"), list) else [],
                "cloud_api_url": BASE_URL,
                "primary_domain": "",
                "admin_username": edge_admin_user,
                "admin_password": edge_admin_pass,
            },
        ),
        "local finalize",
    )

    license_check = ok(
        req(
            session,
            "GET",
            f"/api/control-plane/edge-link/license-check?tenant_id={TENANT_ID}&edge_id={edge_id}",
        ),
        "license check",
    )

    user_rows = ok(
        req(session, "GET", f"/api/control-plane/users?tenant_id={TENANT_ID}"),
        "list users",
    )
    users = user_rows.get("rows") if isinstance(user_rows.get("rows"), list) else []
    cloud_user = next(
        (
            row
            for row in users
            if str((row or {}).get("username") or "").strip() == edge_admin_user
            and str((row or {}).get("customer_id") or "").strip() == customer_id
        ),
        None,
    )

    edge_login = ok(
        req(
            requests.Session(),
            "POST",
            "/api/auth/login",
            json={"username": edge_admin_user, "password": edge_admin_pass},
        ),
        "edge admin login",
    )
    edge_token = str(edge_login.get("token") or edge_login.get("access_token") or "").strip()

    lic = license_check.get("license") if isinstance(license_check.get("license"), dict) else {}
    lic_modules = lic.get("modules") if isinstance(lic.get("modules"), list) else []
    assertions = {
        "register_ok": bool(register.get("ok")),
        "finalize_ok": bool(finalize.get("ok")),
        "license_check_ok": bool(license_check.get("ok")),
        "license_id_match": str(lic.get("license_id") or "").strip() == license_id,
        "license_start_present": bool_nonempty(lic.get("start_utc")),
        "license_end_present": bool_nonempty(lic.get("end_utc")),
        "license_modules_present": len(lic_modules) > 0,
        "customer_linked": str((license_check.get("edge") or {}).get("customer_id") or "").strip() == customer_id,
        "cloud_user_created": cloud_user is not None,
        "edge_admin_can_login": bool_nonempty(edge_token),
    }

    report["finished_utc"] = now_iso()
    report["assertions"] = assertions
    report["ok"] = all(assertions.values())
    report["activation_code"] = activation_code
    report["register"] = register
    report["finalize"] = finalize
    report["license_check"] = license_check
    report["cloud_user_row"] = cloud_user or {}

    os.makedirs(OUT_DIR, exist_ok=True)
    out_file = os.path.join(OUT_DIR, f"smoke_login_activation_{run_id}.json")
    with open(out_file, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": out_file,
                "assertions": assertions,
                "ids": {
                    "customer_id": customer_id,
                    "edge_id": edge_id,
                    "license_id": license_id,
                    "activation_code": activation_code,
                    "edge_admin_user": edge_admin_user,
                },
            },
            indent=2,
        )
    )
    if not report["ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()

