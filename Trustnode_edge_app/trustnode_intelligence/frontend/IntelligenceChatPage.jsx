import React, { useEffect, useRef, useState, useCallback } from "react";
import intelligenceApi from "./api.js";
import { ChatList } from "./components/ChatList.jsx";
import { ChatMessage } from "./components/ChatMessage.jsx";
import { DataSourceToggle } from "./components/DataSourceToggle.jsx";
import { EffortSlider } from "./components/EffortSlider.jsx";
import { InsightEditor } from "./components/InsightEditor.jsx";
import { InsightPreviewModal } from "./components/InsightPreviewModal.jsx";
import { PredefinedQueries } from "./components/PredefinedQueries.jsx";

/**
 * TrustNode Intelligence — Chat page.
 *
 * Clean rewrite 2026-07-02. Goals:
 *   - Light + fast.
 *   - Provider-agnostic (OpenAI, Anthropic-via-router, Ollama, any OpenAI-compat).
 *   - Optimistic UI: user message appears immediately.
 *   - Auth-safe: 401s show a plain message, don't lock the UI.
 *   - Preserves the Save-as-Insight flow (preview modal + editor).
 *   - SURVIVES UNMOUNT: the host renders this page with
 *     `{activePage === 'intelligence_chat' ? <IntelligenceChatPage/> : null}`,
 *     so switching screens UNMOUNTS the component and destroys its state.
 *     We keep a module-level cache (survives unmount) + persist the active
 *     chat id in localStorage, so coming back restores instantly instead of
 *     showing an empty page while it re-fetches. React state is seeded from
 *     the cache on mount; the network refresh then reconciles in the
 *     background.
 */

// Module-level cache — persists across mount/unmount cycles (survives screen
// switches; cleared only on full page reload / logout).
const _CACHE = {
  status: null,
  chats: [],
  activeId: null,
  messagesByChat: {},   // chatId -> messages[]
  dataSource: "local",
  catalogTags: null,    // customer's real tag names for the query palette
};
const _ACTIVE_ID_KEY = "trustnode_intelligence_active_chat_v1";

function _persistActiveId(id) {
  try {
    if (id) localStorage.setItem(_ACTIVE_ID_KEY, id);
    else localStorage.removeItem(_ACTIVE_ID_KEY);
  } catch {}
}
function _readActiveId() {
  try { return localStorage.getItem(_ACTIVE_ID_KEY) || null; } catch { return null; }
}

export default function IntelligenceChatPage() {
  // Seed from the module cache so a remount shows the last view instantly.
  const [status, setStatus] = useState(_CACHE.status);
  const [chats, setChats] = useState(_CACHE.chats);
  const [activeId, setActiveId] = useState(_CACHE.activeId || _readActiveId());
  const [messages, setMessages] = useState(() => {
    const aid = _CACHE.activeId || _readActiveId();
    return (aid && _CACHE.messagesByChat[aid]) || [];
  });
  const [draft, setDraft] = useState("");              // input box
  const [busy, setBusy] = useState(false);             // send in-flight
  const [dataSource, setDataSource] = useState(_CACHE.dataSource);
  // Effort slider: "instant" = fast data answers; "high" = full AI analysis.
  // Persisted so the user's choice sticks across sessions.
  const [mode, setMode] = useState(() => {
    try {
      const m = localStorage.getItem("trustnode_intelligence_mode");
      return m === "instant" || m === "high" ? m : "high";
    } catch { return "high"; }
  });
  const setModePersist = (m) => {
    setMode(m);
    try { localStorage.setItem("trustnode_intelligence_mode", m); } catch {}
  };
  const [error, setError] = useState("");
  const [authError, setAuthError] = useState(false);   // sticky 401 flag
  // Real tags from THIS customer's DB — used to build the predefined-query
  // palette with the customer's own tag names (never hardcoded demo tags).
  const [liveTags, setLiveTags] = useState(_CACHE.catalogTags || []);
  const [previewDraft, setPreviewDraft] = useState(null);
  const [insightDraft, setInsightDraft] = useState(null);
  const [saveStatus, setSaveStatus] = useState("");
  const scrollRef = useRef(null);
  const activeIdRef = useRef(null);
  activeIdRef.current = activeId;

  // Keep the module cache in sync with the latest state on every change,
  // so the NEXT mount (after a screen switch) restores exactly this view.
  useEffect(() => { _CACHE.status = status; }, [status]);
  useEffect(() => { _CACHE.chats = chats; }, [chats]);
  useEffect(() => { _CACHE.activeId = activeId; _persistActiveId(activeId); }, [activeId]);
  useEffect(() => { _CACHE.dataSource = dataSource; }, [dataSource]);
  useEffect(() => {
    if (activeId) _CACHE.messagesByChat[activeId] = messages;
  }, [messages, activeId]);

  // ------ helpers ---------------------------------------------------------

  const looksLikeAuthError = (e) => {
    const s = String(e?.message || e || "");
    // Catch 401, "Invalid token", "Invalid token format", etc.
    return /401|authentication required|_401|invalid token|token format/i.test(s);
  };

  const surfaceError = useCallback((e, prefix = "") => {
    if (looksLikeAuthError(e)) {
      setAuthError(true);
      setError("Your session expired. Please log out and log in again.");
    } else {
      setError((prefix ? prefix + ": " : "") + String(e?.message || e));
    }
  }, []);

  // ------ status ----------------------------------------------------------

  const refreshStatus = useCallback(async () => {
    try {
      const s = await intelligenceApi.getStatus();
      setStatus(s);
    } catch (e) {
      // status is public — a failure here is a network/backend hiccup.
      // Don't set an error; the next tick will retry.
    }
  }, []);

  useEffect(() => {
    // One fetch at mount. NO polling — an every-Nseconds status ping
    // burns anyio threadpool slots that are needed by the (slow) AI
    // send_message call, which used to cause hard wedges. Instead we
    // refresh on window focus + right before every Send, which are the
    // moments the user actually needs a fresh answer.
    refreshStatus();
    // Fetch the customer's real tags ONCE so the predefined-query palette uses
    // their own tag names. Cached across remounts; silent on failure (the
    // palette just falls back to generic phrasings).
    if (!_CACHE.catalogTags) {
      intelligenceApi.getCatalog()
        .then((c) => {
          const tags = c?.tags || [];
          _CACHE.catalogTags = tags;
          setLiveTags(tags);
        })
        .catch(() => {});
    }
    const onFocus = () => refreshStatus();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshStatus]);

  // ------ chats -----------------------------------------------------------

  // Guard: while a send is in flight (or just finished) we must NOT let any
  // background refresh re-fetch and overwrite the live messages array — that
  // caused the "message disappears then reappears" flicker.
  const busyRef = useRef(false);

  const refreshChats = useCallback(async (opts = {}) => {
    try {
      const r = await intelligenceApi.listChats();
      const list = r?.chats || [];
      setChats(list);
      setAuthError(false);
      // listOnly: only update the sidebar list; DON'T touch the open chat's
      // messages (used after a send so the optimistic bubbles stay put).
      if (opts.listOnly || busyRef.current) return;
      // Restore the persisted active chat if it still exists; otherwise
      // fall back to the first chat. Never clobber an already-selected one.
      const current = activeIdRef.current;
      if (current && list.some((c) => c.id === current)) {
        // Already open — leave its messages alone (cache/live state is truth).
        return;
      } else if (list.length) {
        selectChat(list[0].id);
      } else {
        setActiveId(null);
        setMessages([]);
      }
    } catch (e) {
      surfaceError(e);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [surfaceError]);

  const selectChat = useCallback(async (id, opts = {}) => {
    if (busyRef.current) return;  // don't switch/refetch mid-send
    const _ts = (typeof performance !== "undefined" ? performance.now() : Date.now());
    const _tl = (p) => { try { console.log(`[tn-intel-ui] selectChat(${id}) ${p} +${((typeof performance!=="undefined"?performance.now():Date.now())-_ts).toFixed(0)}ms`); } catch {} };
    _tl("start");
    setActiveId(id);
    setError("");
    // Show cached messages immediately (instant), then refresh from server.
    const cached = _CACHE.messagesByChat[id];
    if (cached) setMessages(cached);
    _tl(`cached-shown(${cached ? cached.length : 0} msgs)`);
    try {
      const r = await intelligenceApi.getChat(id);
      _tl(`getChat-returned(${(r?.chat?.messages || []).length} msgs)`);
      // Only apply if the user is still on this chat AND not mid-send.
      if (activeIdRef.current === id && !busyRef.current) {
        setMessages(r?.chat?.messages || []);
        setDataSource(r?.chat?.data_source || "local");
        _tl("setMessages-called");
      }
    } catch (e) {
      if (!opts.silent) surfaceError(e);
    }
  }, [surfaceError]);

  useEffect(() => { refreshChats(); }, [refreshChats]);

  // Scroll to bottom whenever messages change.
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, busy]);

  // ------ actions ---------------------------------------------------------

  const newChat = async () => {
    setError("");
    const _ts = (typeof performance !== "undefined" ? performance.now() : Date.now());
    const _tl = (p) => { try { console.log(`[tn-intel-ui] newChat ${p} +${((typeof performance!=="undefined"?performance.now():Date.now())-_ts).toFixed(0)}ms`); } catch {} };
    _tl("start");
    try {
      const r = await intelligenceApi.createChat({ title: "New chat", data_source: dataSource });
      _tl("createChat-returned");
      // Optimistic: add to the sidebar + open it with an empty message list —
      // no round-trips, instant UI.
      setActiveId(r.id);
      setMessages([]);
      setChats((prev) => (prev.some((c) => c.id === r.id) ? prev
        : [{ id: r.id, title: "New chat", data_source: dataSource }, ...prev]));
      _tl("state-updated (UI should be done)");
    } catch (e) {
      surfaceError(e, "Could not create chat");
    }
  };

  const deleteChat = async (id) => {
    // Optimistic: remove from the sidebar immediately so the UI feels instant.
    setChats((prev) => prev.filter((c) => c.id !== id));
    if (id === activeId) {
      setActiveId(null);
      setMessages([]);
    }
    try {
      await intelligenceApi.deleteChat(id);
    } catch (e) {
      surfaceError(e, "Could not delete chat");
      refreshChats(); // resync on failure
    }
  };

  const renameChat = async (id, title) => {
    // Optimistic rename in the sidebar.
    setChats((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)));
    try {
      await intelligenceApi.renameChat(id, title);
    } catch (e) {
      surfaceError(e, "Could not rename chat");
      refreshChats();
    }
  };

  const sendTo = async (chatId, text) => {
    // Optimistic: user bubble appears immediately, stamped with local time.
    const sentAt = Date.now();
    const userMsg = { role: "user", content: text, created_utc: new Date(sentAt).toISOString() };
    busyRef.current = true;               // block background message refetches
    setMessages((m) => [...m, userMsg]);
    setBusy(true);
    setError("");
    try {
      const r = await intelligenceApi.sendMessage(chatId, text, dataSource, mode);
      const assistantMsg = {
        role: "assistant",
        content: r?.content || "(no response)",
        tool_results: r?.tool_log || [],
        created_utc: new Date().toISOString(),
        latency_ms: Date.now() - sentAt,   // send→reply round-trip
      };
      setMessages((m) => [...m, assistantMsg]);
    } catch (e) {
      const isAuth = looksLikeAuthError(e);
      const msg = isAuth
        ? "Your session expired. Please log out and log in again."
        : "The assistant could not respond. " + String(e?.message || e);
      setMessages((m) => [...m, { role: "assistant", content: msg }]);
      if (isAuth) setAuthError(true);
    } finally {
      setBusy(false);
      busyRef.current = false;
      // Only refresh the sidebar LIST (title may have auto-updated) — never
      // re-fetch the open chat's messages, so the bubbles never flicker.
      setTimeout(() => { refreshChats({ listOnly: true }); }, 150);
    }
  };

  const send = async (presetText) => {
    // presetText lets a predefined-query chip send directly without typing.
    const text = (typeof presetText === "string" ? presetText : draft).trim();
    if (!text || busy) return;
    setDraft("");
    // Operator 2026-07-02: do NOT fire refreshStatus() here. The Electron
    // renderer limits ~6 concurrent connections per host; a long AI send
    // holds one for several seconds, and firing extra status/chat requests
    // alongside it caused collisions that surfaced as "Failed to fetch".
    // Keep the send path to the MINIMUM number of requests.
    let chatId = activeId;
    if (!chatId) {
      try {
        const r = await intelligenceApi.createChat({
          title: text.slice(0, 40),
          data_source: dataSource,
        });
        chatId = r.id;
        setActiveId(chatId);
        // Optimistically add the new chat to the sidebar without a full
        // refresh round-trip (which would compete with the send request).
        setChats((prev) => {
          if (prev.some((c) => c.id === chatId)) return prev;
          return [{ id: chatId, title: text.slice(0, 40), data_source: dataSource }, ...prev];
        });
      } catch (e) {
        surfaceError(e, "Could not create chat");
        setDraft(text); // put the draft back so it isn't lost
        return;
      }
    }
    await sendTo(chatId, text);
  };

  // ------ derived UI states ----------------------------------------------

  // Show the "not configured" banner ONLY when we've confirmed via /status
  // that the module is licensed but the endpoint URL is missing. Never
  // show while we're still loading (status === null), so the user isn't
  // startled by a false-positive on cold start.
  const bannerConfig = status && status.licensed && !status.endpoint_configured
    ? "not_configured"
    : authError ? "auth_expired" : null;

  // ------ render ----------------------------------------------------------

  return (
    <div
      className="card"
      style={{
        display: "flex",
        height: "calc(100vh - 120px)",
        background: "var(--card)",
        color: "var(--text)",
        border: "1px solid var(--stroke)",
        borderRadius: "var(--radius-lg, 12px)",
        overflow: "hidden",
      }}
    >
      <ChatList
        chats={chats}
        activeId={activeId}
        onSelect={selectChat}
        onCreate={newChat}
        onDelete={deleteChat}
        onRename={renameChat}
      />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {/* Header */}
        <div style={{
          padding: "10px 16px",
          borderBottom: "1px solid var(--stroke)",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16,
          background: "var(--surface-elev, var(--card))",
        }}>
          <div style={{ fontSize: 14, fontWeight: 600 }}>TrustNode Intelligence — Chat</div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <small style={{ color: "var(--muted)" }}>Data source</small>
            <DataSourceToggle value={dataSource} onChange={setDataSource} disabled={busy} />
          </div>
        </div>

        {/* Banner (single-state) */}
        {bannerConfig === "not_configured" ? (
          <div style={{
            margin: 16, padding: "10px 14px", borderRadius: 6,
            background: "color-mix(in srgb, #eab308 12%, var(--card))",
            border: "1px solid color-mix(in srgb, #eab308 40%, var(--stroke))",
            color: "var(--text)", fontSize: 13,
          }}>
            The AI assistant is not configured yet. An administrator needs to set the endpoint URL in the developer portal.
          </div>
        ) : bannerConfig === "auth_expired" ? (
          <div style={{
            margin: 16, padding: "10px 14px", borderRadius: 6,
            background: "color-mix(in srgb, #dc2626 12%, var(--card))",
            border: "1px solid color-mix(in srgb, #dc2626 40%, var(--stroke))",
            color: "var(--text)", fontSize: 13,
          }}>
            Your session expired. Please log out and log in again to continue.
          </div>
        ) : null}

        {/* Messages */}
        <div ref={scrollRef} style={{
          flex: 1, overflowY: "auto", padding: "16px 24px", background: "var(--bg)",
        }}>
          {messages.length === 0 && !busy ? (
            <PredefinedQueries onPick={(q) => send(q)} tags={liveTags} />
          ) : null}
          {messages.map((m, i) => {
            const handleSave = m.role === "assistant" ? () => {
              let prevUser = "";
              for (let k = i - 1; k >= 0; k--) {
                if (messages[k]?.role === "user") {
                  prevUser = String(messages[k].content || "");
                  break;
                }
              }
              const tools = m.tool_results || m.tool_log || [];
              const tool_plan = tools.map((t) => ({ name: t.name, args: t.args || {} }));
              const title = prevUser
                ? prevUser.slice(0, 60) + (prevUser.length > 60 ? "…" : "")
                : "Saved chat insight";
              setPreviewDraft({
                content: String(m.content || ""),
                toolLog: tools,
                title,
                prompt: prevUser,
                tool_plan,
              });
            } : undefined;
            // Clickable disambiguation options only on the LAST assistant
            // message (the current question), and only when not busy.
            const isLastAssistant = m.role === "assistant" && i === messages.length - 1;
            const handlePick = (isLastAssistant && !busy)
              ? (opt) => {
                  if (busy) return;
                  const chatId = activeId;
                  if (chatId) sendTo(chatId, opt);
                }
              : undefined;
            return (
              <ChatMessage
                key={i}
                role={m.role}
                content={m.content}
                toolLog={m.tool_results || m.tool_log}
                createdUtc={m.created_utc}
                latencyMs={m.latency_ms}
                onSaveAsInsight={handleSave}
                onPickOption={handlePick}
              />
            );
          })}
          {busy ? (
            <div style={{
              color: "var(--muted)", fontSize: 12, fontStyle: "italic", margin: "6px 0",
            }}>
              TrustNode Intelligence is thinking…
            </div>
          ) : null}
        </div>

        {/* Error line (non-banner errors) */}
        {error && !bannerConfig ? (
          <div style={{ padding: "6px 16px", color: "#dc2626", fontSize: 12 }}>{error}</div>
        ) : null}

        {/* Composer */}
        <div style={{
          padding: 12, borderTop: "1px solid var(--stroke)",
          background: "var(--surface-elev, var(--card))",
          display: "flex", gap: 8, alignItems: "center",
        }}>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!busy) send();
              }
            }}
            placeholder={busy ? "Thinking — your next message will queue…" : "Ask about your process data…"}
            rows={2}
            autoFocus
            style={{
              flex: 1, resize: "none", padding: 10,
              background: "var(--bg)", color: "var(--text)",
              caretColor: "var(--text)",
              border: "1px solid var(--stroke)", borderRadius: 6,
              fontSize: 13, fontFamily: "inherit",
              minHeight: 56, lineHeight: 1.4,
              opacity: busy ? 0.8 : 1,
            }}
          />
          <EffortSlider mode={mode} onChange={setModePersist} />
          <button
            className="btn btn-primary"
            onClick={send}
            disabled={busy || !draft.trim()}
            style={{ padding: "0 20px", height: 34 }}
          >
            Send
          </button>
        </div>
      </div>

      {/* Save-as-insight preview */}
      {previewDraft ? (
        <InsightPreviewModal
          title={previewDraft.title || "Preview"}
          subtitle={previewDraft.prompt ? `From: ${previewDraft.prompt}` : ""}
          content={previewDraft.content}
          toolLog={previewDraft.toolLog}
          onClose={() => setPreviewDraft(null)}
          actionFooter={
            <>
              <button
                className="btn btn-secondary"
                onClick={() => setPreviewDraft(null)}
                style={{ padding: "8px 14px", fontSize: 13 }}
              >Close</button>
              <button
                className="btn btn-primary"
                onClick={() => {
                  setInsightDraft({
                    title: previewDraft.title,
                    description: "",
                    prompt: previewDraft.prompt || "Re-run the saved tool plan and narrate the results.",
                    tool_plan: previewDraft.tool_plan || [],
                    data_source: dataSource,
                    schedule_cron: "",
                    email_to: "",
                  });
                  setPreviewDraft(null);
                }}
                style={{ padding: "8px 14px", fontSize: 13, fontWeight: 500 }}
              >Save as Insight…</button>
            </>
          }
        />
      ) : null}

      {/* Save-as-insight editor */}
      {insightDraft ? (
        <InsightEditor
          initial={insightDraft}
          onCancel={() => { setInsightDraft(null); setSaveStatus(""); }}
          onSave={async (payload) => {
            // Return true on success / false on failure so the editor can
            // reset its "Saving…" state instead of hanging forever.
            try {
              await intelligenceApi.createInsight(payload);
              setInsightDraft(null);
              setSaveStatus("Insight saved.");
              setTimeout(() => setSaveStatus(""), 2500);
              return true;
            } catch (e) {
              surfaceError(e, "Save insight failed");
              return false;
            }
          }}
        />
      ) : null}

      {/* Toast */}
      {saveStatus ? (
        <div style={{
          position: "fixed", right: 24, bottom: 24, zIndex: 9999,
          padding: "8px 14px", fontSize: 12,
          background: "color-mix(in srgb, var(--teal, #14a89a) 22%, var(--card))",
          color: "var(--text)", border: "1px solid var(--teal, #14a89a)",
          borderRadius: 6, boxShadow: "0 6px 18px rgba(0,0,0,0.3)",
        }}>{saveStatus}</div>
      ) : null}
    </div>
  );
}
