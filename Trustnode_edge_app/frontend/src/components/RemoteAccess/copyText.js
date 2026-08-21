// Clipboard helper that works on NON-secure origins too.
//
// `navigator.clipboard` is only defined on secure contexts (https://,
// localhost, file://). The edge is reached over plain http://<lan-ip>:8088
// from other PCs, where the old `navigator.clipboard?.writeText(...)` was a
// silent no-op (plan 2026-08-21 §2.5 landmine 3). Fall back to the legacy
// textarea + execCommand("copy") path, which still works in every browser
// that the LAN surfaces target.
//
// Returns true when the text was (very likely) copied, false otherwise.
export async function copyText(text) {
  const value = String(text ?? "");
  if (!value) return false;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (_) {
    // fall through to the legacy path (permission denied / insecure origin)
  }
  try {
    if (typeof document === "undefined") return false;
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.left = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    document.body.removeChild(ta);
    return Boolean(ok);
  } catch (_) {
    return false;
  }
}

export default copyText;
