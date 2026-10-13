# -*- coding: utf-8 -*-
"""The portal database is a SETTING, and moving it cannot lock you out.

2026-10-13, owner's design: "the portal database can also be moved to a
different database where the portal website would be pointed out (to be
configured from the portal)".

It could not. `TRUSTNODE_CONTROL_PLANE_BACKEND` and the six
`TRUSTNODE_CLOUD_DB_*` variables were read from the process environment - once
at import in state.py, and again in ControlPlaneStoreCloud._get_engine(). So
moving the portal database meant editing a systemd drop-in over SSH and
restarting, which is not a portal action.

The risky part of fixing this is lock-out: the pointer to the control-plane
database cannot live inside that database, and a saved-but-wrong target would
take the portal down on the next restart with no way back except a shell. The
properties below are what make it safe, so each one is asserted:

  * a saved target wins over the environment (otherwise the portal's own
    setting is ignored on exactly the host that has env set - the VPS);
  * TRUSTNODE_CONTROL_PLANE_FORCE_ENV overrides everything (the escape hatch);
  * a corrupt or incomplete file is IGNORED, not half-applied;
  * the password is never returned by describe();
  * a target that cannot be reached is refused before it is saved.

Runs entirely in a scratch workspace and reaches no network.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-cptarget-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")
for _v in ("TRUSTNODE_CONTROL_PLANE_FORCE_ENV",):
    os.environ.pop(_v, None)

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services import control_plane_target as cpt                 # noqa: E402

print("TrustNode - the portal database is a setting")
print()


def set_env(**kw):
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def reset():
    try:
        os.unlink(cpt.target_path())
    except FileNotFoundError:
        pass
    set_env(TRUSTNODE_CONTROL_PLANE_FORCE_ENV=None,
            TRUSTNODE_CONTROL_PLANE_BACKEND="cloud",
            TRUSTNODE_CLOUD_DB_HOST="env-host.example.com",
            TRUSTNODE_CLOUD_DB_USER="env-user",
            TRUSTNODE_CLOUD_DB_PASSWORD="env-pass",
            TRUSTNODE_CLOUD_DB_NAME="postgres",
            TRUSTNODE_CLOUD_DB_PORT="5432",
            TRUSTNODE_CLOUD_DB_SSLMODE="require")


print("[with nothing saved, the environment answers - unchanged behaviour]")
reset()
cfg, src = cpt.resolve()
check("source is the environment", src == "env", src)
check("  host comes from env", cfg.get("host") == "env-host.example.com", cfg.get("host"))
check("  backend comes from env", cpt.backend() == "cloud", cpt.backend())
set_env(TRUSTNODE_CONTROL_PLANE_BACKEND="local")
check("  a local env backend is honoured", cpt.backend() == "local", cpt.backend())
set_env(TRUSTNODE_CONTROL_PLANE_BACKEND="cloud")

print()
print("[a saved target wins over the environment]")
cpt.save({"backend": "cloud", "host": "saved-host.example.com",
          "username": "saved-user", "password": "saved-pass",
          "database": "cpdb", "port": "6543", "sslmode": "require"},
         actor="tester")
cfg, src = cpt.resolve()
check("source is the saved file", src == "file", src)
check("  host is the saved one", cfg.get("host") == "saved-host.example.com",
      "env-wins would mean the portal's own setting is ignored on the one "
      "host that has env set: %s" % cfg.get("host"))
check("  so is the port and database",
      str(cfg.get("port")) == "6543" and cfg.get("database") == "cpdb", cfg)
check("  db_settings() agrees",
      cpt.db_settings().get("host") == "saved-host.example.com", cpt.db_settings())

print()
print("[the escape hatch beats everything]")
set_env(TRUSTNODE_CONTROL_PLANE_FORCE_ENV="1")
cfg, src = cpt.resolve()
check("FORCE_ENV returns the environment", src == "env_forced", src)
check("  even though a file exists", cfg.get("host") == "env-host.example.com",
      "without this, a bad saved target can only be undone from a shell")
check("  describe() says so plainly", cpt.describe().get("env_override_active") is True)
set_env(TRUSTNODE_CONTROL_PLANE_FORCE_ENV=None)

print()
print("[a broken saved file must never stop the portal booting]")
with io.open(cpt.target_path(), "w", encoding="utf-8") as fh:
    fh.write("{ this is not json")
cfg, src = cpt.resolve()
check("corrupt JSON falls back to the environment", src == "env", src)
check("  and backend() still answers", cpt.backend() in ("cloud", "local"), cpt.backend())

with io.open(cpt.target_path(), "w", encoding="utf-8") as fh:
    json.dump({"backend": "cloud", "host": "half-written.example.com"}, fh)
cfg, src = cpt.resolve()
check("an INCOMPLETE cloud target is ignored, not half-applied", src == "env",
      "a host with no username would build a broken engine: %s" % (cfg.get("host"),))

with io.open(cpt.target_path(), "w", encoding="utf-8") as fh:
    json.dump({"backend": "local"}, fh)
cfg, src = cpt.resolve()
check("  a local target needs no connection fields", src == "file" and cfg["backend"] == "local",
      (src, cfg.get("backend")))

print()
print("[the password is never handed back]")
reset()
cpt.save({"backend": "cloud", "host": "h.example.com", "username": "u",
          "password": "super-secret-value"}, actor="tester")
d = cpt.describe()
check("describe() masks it", d.get("password") == "set (18 chars)", d.get("password"))
check("  the secret itself is absent from the whole payload",
      "super-secret-value" not in json.dumps(d),
      "the portal must be able to show that a password is set without "
      "showing it")
check("  but db_settings() still has it for the engine",
      cpt.db_settings().get("password") == "super-secret-value")
check("  the file is written 0600 where the OS supports it",
      (os.stat(cpt.target_path()).st_mode & 0o077) == 0 or os.name == "nt",
      oct(os.stat(cpt.target_path()).st_mode))

print()
print("[saving is validated]")
try:
    cpt.save({"backend": "cloud", "host": "", "username": ""})
    bad_ok = True
except ValueError:
    bad_ok = False
check("a cloud target with no host/username is refused", not bad_ok,
      "saving it would point the portal at nothing on the next restart")

print()
print("[clear() is the undo]")
cpt.clear()
cfg, src = cpt.resolve()
check("the environment answers again", src == "env", src)
check("  and the file is gone", not os.path.exists(cpt.target_path()))

print()
print("[a connection test never saves, and reports usefully]")
res = cpt.test_connection({"backend": "local"})
check("a local target needs no test", res.get("ok") is True, res)
res = cpt.test_connection({"backend": "cloud", "host": "", "username": ""})
check("  a cloud target without host/username fails fast",
      res.get("ok") is False and "required" in str(res.get("error")), res)
check("  testing did not create a file", not os.path.exists(cpt.target_path()),
      "test must be side-effect free - it is what you run BEFORE committing")

print()
print("[the wiring actually uses it]")
STATE = io.open(os.path.join(ROOT, "backend", "app", "state.py"), encoding="utf-8").read()
check("state.py picks the backend from the target",
      "control_plane_target as _cp_target" in STATE and "_cp_target.backend()" in STATE,
      "otherwise the saved setting changes nothing")
check("  and still boots if the target module explodes",
      "except Exception:" in STATE.split("_cp_target.backend()")[1][:260],
      "a bad target must not stop the service starting")
CLOUD = io.open(os.path.join(ROOT, "backend", "app", "services",
                             "control_plane_store_cloud.py"), encoding="utf-8").read()
check("the cloud engine reads the resolved target",
      "_cp_target.db_settings()" in CLOUD,
      "state.py choosing 'cloud' is useless if the engine still reads env")
check("  with the env vars kept as the fallback",
      'os.environ.get("TRUSTNODE_CLOUD_DB_HOST"' in CLOUD,
      "a deployment that never touches the portal must behave as it always did")

ROUTER = io.open(os.path.join(ROOT, "backend", "app", "routers",
                              "control_plane.py"), encoding="utf-8").read()
check("the portal has endpoints for it",
      ROUTER.count('@router.get("/database")') == 1
      and ROUTER.count('@router.put("/database")') == 1
      and ROUTER.count('@router.post("/database/test")') == 1
      and ROUTER.count('@router.delete("/database")') == 1)
block = ROUTER.split('@router.put("/database")')[1][:1800]
check("  save REFUSES a target it cannot reach",
      "connection_failed" in block and "test_connection(cfg)" in block,
      "saving an unreachable target breaks the portal on next restart")
check("  all four require a GLOBAL admin",
      ROUTER.split('@router.get("/database")')[1][:900].count("_is_global_admin") >= 1
      and block.count("_is_global_admin") >= 1,
      "this points the whole portal at a different database")
check("  a blank password keeps the stored one",
      "payload.keep_password and not str(payload.password" in ROUTER,
      "the API never returns the password, so a round-trip must not erase it")

UI = io.open(os.path.join(ROOT, "frontend", "src", "components", "ControlPlane",
                          "ControlPlaneDatabase.jsx"), encoding="utf-8").read()
check("the portal screen exists", "Portal database" in UI)
check("  and will not let you save an untested target",
      "const canSave = Boolean(testResult && testResult.ok)" in UI)
check("  changing a field invalidates the previous test",
      "setTestResult(null);   // a changed target invalidates" in UI,
      "otherwise you can test host A and save host B")

print()
print("[the packaged build must contain it]")
# control_plane_target is imported LAZILY, inside functions, so PyInstaller's
# static analysis would not see it on its own - and the fallbacks are silent,
# so a missing module would quietly disable the whole feature in the shipped
# exe while every source test still passed.
SPEC = io.open(os.path.join(ROOT, "backend", "trustnode-service.spec"),
               encoding="utf-8").read()
check("the spec collects every app submodule",
      "collect_submodules('app')" in SPEC,
      "a lazy import is invisible to static analysis; this is what covers it")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
