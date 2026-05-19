"""Replace the embedded PNG assets inside the portal bundler stub
with the already-compressed versions on disk.

Approach:
  1. Read /var/www/trustnode/portal/v1/index.html
  2. Parse the <script type='__bundler/manifest'> JSON
  3. For each asset entry that's an image/png, base64-decode it and
     compare its SHA256 to our compressed files on disk
  4. If we find a match (by visual identity OR a per-image heuristic),
     swap its `data` field with the base64 of the compressed file
  5. Also recompress assets that are still large but didn't match — we
     can re-encode their bytes through Pillow with quantize+optimize
  6. Write the manifest back and save the bundler stub

Idempotent. --dry-run reports savings without writing.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
import paramiko

ROOT = Path(__file__).resolve().parents[1]
env = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")


def _ps(s):
    try: print(s)
    except UnicodeEncodeError: print(s.encode("ascii", errors="replace").decode("ascii"))


parser = argparse.ArgumentParser()
parser.add_argument("--commit", action="store_true",
                    help="Write the rebundled file. Without this, dry-run only.")
args = parser.parse_args()


c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)


# Inline Python to run on the VPS (Pillow lives in the backend venv).
PY = r"""
import os, sys, json, re, base64, hashlib, io, gzip
from PIL import Image

PORTAL = "/var/www/trustnode/portal/v1/index.html"
COMMIT = """ + ("True" if args.commit else "False") + r"""

print(f"reading {PORTAL} ({os.path.getsize(PORTAL)/1024/1024:.2f} MB)")
with open(PORTAL, "r", encoding="utf-8") as f:
    html = f.read()

# Locate the manifest script block
m = re.search(r'<script type="__bundler/manifest"[^>]*>(.+?)</script>', html, re.DOTALL)
if not m:
    print("FATAL: manifest script not found")
    sys.exit(1)
manifest_text = m.group(1)
manifest_start = m.start(1)
manifest_end = m.end(1)
manifest = json.loads(manifest_text)
print(f"manifest: {len(manifest)} assets")

# Helper: gzip if entry says it's compressed, else raw
def decode_entry(entry):
    raw_b64 = entry.get("data", "")
    raw = base64.b64decode(raw_b64)
    if entry.get("compressed"):
        raw = gzip.decompress(raw)
    return raw

def encode_entry(entry, new_bytes):
    out_bytes = new_bytes
    if entry.get("compressed"):
        out_bytes = gzip.compress(new_bytes, compresslevel=9)
    entry["data"] = base64.b64encode(out_bytes).decode("ascii")
    return entry

# Recompress every image/png entry by re-encoding through Pillow with
# palette quantization + maximum compression. Skip non-image entries.
total_before = 0
total_after = 0
changed = 0
for uuid, entry in manifest.items():
    mime = entry.get("mime", "")
    raw_b64 = entry.get("data", "")
    before = len(raw_b64)
    total_before += before
    if not mime.startswith("image/"):
        total_after += before
        continue
    try:
        raw = decode_entry(entry)
        img = Image.open(io.BytesIO(raw))
        # For PNGs, downscale oversized ones + quantize to palette if photo-like.
        w, h = img.size
        max_dim = 1600  # backgrounds get capped here; logos resize if >max_dim
        # logos are typically RGBA — keep alpha by NOT quantizing
        has_alpha = (img.mode in ("RGBA", "LA")) or (img.mode == "P" and "transparency" in img.info)
        if w > max_dim or h > max_dim:
            scale = max_dim / max(w, h)
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.LANCZOS)
        # Re-encode
        buf = io.BytesIO()
        if mime == "image/png" and not has_alpha:
            # Photo-style PNG -> quantize to palette
            try:
                pal = img.convert("RGB").quantize(
                    colors=256, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG
                )
                pal.save(buf, "PNG", optimize=True)
            except Exception:
                img.save(buf, "PNG", optimize=True)
        elif mime == "image/png":
            # PNG with alpha: re-save optimized but keep RGBA
            img.save(buf, "PNG", optimize=True)
        elif mime in ("image/jpeg", "image/jpg"):
            img.convert("RGB").save(buf, "JPEG", quality=82, optimize=True, progressive=True)
        else:
            # SVG / other — skip
            total_after += before
            continue
        new_bytes = buf.getvalue()
        if len(new_bytes) >= len(raw):
            # No improvement; keep original
            total_after += before
            continue
        encode_entry(entry, new_bytes)
        after = len(entry["data"])
        total_after += after
        savings = (1 - after/before) * 100
        print(f"  {mime:12s} {uuid[:8]}.. {before/1024:7.0f} KB -> {after/1024:7.0f} KB  (-{savings:.0f}%)")
        changed += 1
    except Exception as exc:
        total_after += before
        print(f"  [skip] {uuid[:8]} ({mime}): {exc}")

print()
print(f"total: {total_before/1024/1024:.2f} MB -> {total_after/1024/1024:.2f} MB")
print(f"changed: {changed} assets")

if not COMMIT:
    print("\n[DRY-RUN] no file written. Re-run with --commit to apply.")
    sys.exit(0)

# Rewrite the manifest block with the new JSON
new_manifest = json.dumps(manifest, separators=(",", ":"))
new_html = html[:manifest_start] + new_manifest + html[manifest_end:]

bak = PORTAL + ".bak-rebundle"
if not os.path.exists(bak):
    import shutil
    shutil.copy2(PORTAL, bak)
    print(f"backup: {bak}")

tmp = PORTAL + ".new"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(new_html)
os.replace(tmp, PORTAL)
# nginx owns the file
import subprocess
subprocess.run(["chown", "nginx:nginx", PORTAL], check=False)

print(f"\nwrote {PORTAL} ({os.path.getsize(PORTAL)/1024/1024:.2f} MB)")
"""

sftp = c.open_sftp()
with sftp.open("/tmp/_tn_rebundle.py", "w") as f:
    f.write(PY)
sftp.close()

stdin, stdout, _ = c.exec_command(
    "/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python "
    "/tmp/_tn_rebundle.py 2>&1",
    timeout=180,
)
_ps(stdout.read().decode())

c.exec_command("rm -f /tmp/_tn_rebundle.py")
c.close()
