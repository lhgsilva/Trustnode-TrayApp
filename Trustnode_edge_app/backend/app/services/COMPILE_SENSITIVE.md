# Compiling sensitive modules to .pyd

Operator note 2026-06-18.

The current build already strips all `.py` source files at PyInstaller
time — only `.pyc` (in archives) and `.pyd` extensions reach the
customer machine. `uncompyle6` against `.pyc` recovers ~90% of source.

If we want to raise the bar further for specific modules — without
paying PyArmor — Cython AOT compile turns Python source into a C
extension. The `.pyd` output is no longer trivially decompilable.

## Candidate modules

Modules worth compiling first (low coupling, high sensitivity):

- `license_signature.py` — the public key check. If decompiled and
  replaced to always return verified=true, the whole license model
  collapses.
- `license_gate.py` — the data-write veto. Same risk.
- `control_plane_store.py` — license + module storage.

## Build steps

1. Install build tools on the dev/CI machine:
   ```
   pip install cython
   # Windows: install Visual Studio Build Tools (MSVC)
   ```

2. Add a tiny `setup_cython.py` next to this file:
   ```python
   from setuptools import setup
   from Cython.Build import cythonize

   setup(
       name="trustnode_compiled",
       ext_modules=cythonize(
           [
               "license_signature.py",
               "license_gate.py",
           ],
           compiler_directives={"language_level": "3"},
       ),
   )
   ```

3. Run during build:
   ```
   python setup_cython.py build_ext --inplace
   ```
   This produces `license_signature.cp312-win_amd64.pyd` alongside the
   `.py`. PyInstaller picks up the `.pyd` automatically.

4. After the `.pyd` is generated, DELETE the original `.py` from the
   build output (NOT from the source tree) so only the compiled form
   is shipped:
   ```
   del backend\dist\trustnode-service\_internal\app\services\license_signature.py
   ```

## Why this isn't enabled by default

- Adds a Visual Studio Build Tools dependency to the build environment.
- GitHub Actions Windows runners need the same setup (~5 min extra per
  build).
- A determined attacker with `ghidra` can still reverse-engineer a
  `.pyd`, just much more slowly. Not unbreakable.
- The license-signature mechanism (Phase 2a) already stops the most
  common attack path (editing the SQLite to flip module flags). Cython
  protects against the rarer "replace license_signature.py with a stub
  that returns verified=true" attack.

If a future customer's threat model demands this, follow the steps
above. Until then, the current build already meets the
"users should not see Python source files" requirement — verified
2026-06-18 by inspecting `backend/dist/trustnode-service/_internal/`.
