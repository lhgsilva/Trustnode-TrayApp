import React, { useState } from "react";

// Local HH:MM from a utc/iso timestamp. "" on failure.
function fmtClock(ts) {
  if (!ts) return "";
  try {
    let s = String(ts).trim();
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/.test(s) && !s.includes("T")) {
      s = s.replace(" ", "T");
      if (!/[Z+]/.test(s.slice(10))) s += "Z";
    }
    const d = new Date(s);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch { return ""; }
}

// Operator 2026-06-30: use host CSS vars (--card, --stroke, --muted,
// --text) so the panel respects light/dark mode. Teal accent for actions.
// Operator 2026-07-02: show the chat time (HH:MM) as a compact subtitle,
// and allow renaming the chat title inline (double-click or the ✎ button).
export function ChatList({ chats, activeId, onSelect, onCreate, onDelete, onRename }) {
  const [editingId, setEditingId] = useState(null);
  const [draftTitle, setDraftTitle] = useState("");

  const startEdit = (c) => {
    setEditingId(c.id);
    setDraftTitle(c.title || "New chat");
  };
  const commitEdit = (id) => {
    const t = draftTitle.trim();
    setEditingId(null);
    if (t && onRename) onRename(id, t);
  };

  return (
    <div style={{
      width: 240, borderRight: "1px solid var(--stroke)",
      display: "flex", flexDirection: "column", height: "100%",
      background: "var(--surface-elev, var(--card))",
      color: "var(--text)",
    }}>
      <div style={{ padding: 10 }}>
        <button
          className="btn btn-primary"
          onClick={onCreate}
          style={{ width: "100%", padding: "8px 12px", fontSize: 13, fontWeight: 500 }}
        >
          + New chat
        </button>
      </div>
      <div style={{ flex: 1, overflowY: "auto" }}>
        {(chats || []).map((c) => {
          const active = c.id === activeId;
          const time = fmtClock(c.updated_utc || c.created_utc);
          const isEditing = editingId === c.id;
          return (
            <div
              key={c.id}
              onClick={() => !isEditing && onSelect && onSelect(c.id)}
              onDoubleClick={(e) => { e.stopPropagation(); startEdit(c); }}
              style={{
                padding: "7px 12px", cursor: "pointer",
                background: active ? "color-mix(in srgb, var(--teal, #14a89a) 16%, transparent)" : "transparent",
                borderLeft: active ? "3px solid var(--teal, #14a89a)" : "3px solid transparent",
                color: "var(--text)",
                display: "flex", justifyContent: "space-between", alignItems: "center",
                gap: 4,
              }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                {isEditing ? (
                  <input
                    autoFocus
                    value={draftTitle}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onBlur={() => commitEdit(c.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitEdit(c.id);
                      else if (e.key === "Escape") setEditingId(null);
                    }}
                    style={{
                      width: "100%", fontSize: 13, padding: "2px 4px",
                      background: "var(--bg)", color: "var(--text)",
                      border: "1px solid var(--teal, #14a89a)", borderRadius: 4,
                    }}
                  />
                ) : (
                  <>
                    <div style={{
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      fontSize: 13, lineHeight: 1.3,
                    }}>
                      {c.title || "New chat"}
                    </div>
                    {time ? (
                      <div style={{ fontSize: 10, color: "var(--muted)", marginTop: 1 }}>{time}</div>
                    ) : null}
                  </>
                )}
              </div>
              {!isEditing ? (
                <div style={{ display: "flex", gap: 2, flexShrink: 0 }}>
                  <button
                    onClick={(e) => { e.stopPropagation(); startEdit(c); }}
                    style={{
                      border: "none", background: "transparent", color: "var(--muted)",
                      cursor: "pointer", padding: 2, fontSize: 12,
                    }}
                    title="Rename chat"
                  >✎</button>
                  <button
                    onClick={(e) => { e.stopPropagation(); onDelete && onDelete(c.id); }}
                    style={{
                      border: "none", background: "transparent", color: "var(--muted)",
                      cursor: "pointer", padding: 2, fontSize: 14,
                    }}
                    title="Delete chat"
                  >×</button>
                </div>
              ) : null}
            </div>
          );
        })}
        {(!chats || chats.length === 0) ? (
          <div style={{ padding: 14, color: "var(--muted)", fontSize: 12 }}>
            No chats yet. Create one to start.
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default ChatList;
