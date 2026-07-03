import React, { useEffect, useState } from "react";
import { IntelligenceIcon } from "./icon.jsx";
import intelligenceApi from "./api.js";

/*
 * TrustNode Intelligence sidebar section.
 *
 * Mounts above the user-login footer in the host sidebar. Renders the
 * SAME markup the host NAV_SECTIONS render (className="nav-section",
 * "nav-group-btn", "nav-item nav-subitem", etc.) so it inherits every
 * theme rule the host already ships — dark/light mode, hover states,
 * collapsed-sidebar icon mode, all of it.
 *
 * Props from the host:
 *   - activePage:        current page key (host's setActivePage state)
 *   - onNavigate(key):   callback when a child item is clicked
 *   - sidebarCollapsed:  host's narrow-icon-only mode (so we collapse
 *                        the header text the same way)
 *
 * Self-hides when /api/intelligence/status returns 404 (module not
 * loaded) or licensed:false. Independent of the host's permission gate.
 */
// Operator 2026-07-01: cache the last known "licensed" state in
// localStorage so the menu appears instantly on the next boot instead
// of waiting for the /api/intelligence/status round-trip. The status
// call still runs in the background and corrects the cache on mismatch
// (customer unlicensed → menu hides on next refresh).
const _INTEL_LICENSE_CACHE_KEY = "trustnode_intelligence_licensed_v1";

function _readLicenseCache() {
  try {
    const raw = localStorage.getItem(_INTEL_LICENSE_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch { return null; }
}

function _writeLicenseCache(state) {
  try { localStorage.setItem(_INTEL_LICENSE_CACHE_KEY, JSON.stringify(state)); } catch {}
}

export default function IntelligenceMenu({ activePage, onNavigate, sidebarCollapsed }) {
  // Start from the cached answer so the menu appears immediately on
  // reboot. null only when we've never fetched.
  const _cached = _readLicenseCache();
  const [licensed, setLicensed] = useState(_cached ? Boolean(_cached.licensed) : null);
  const [insightsEnabled, setInsightsEnabled] = useState(_cached ? Boolean(_cached.insights !== false) : true);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await intelligenceApi.getStatus();
        if (cancelled) return;
        const nextLicensed = Boolean(status?.licensed);
        const nextInsights = Boolean(status?.features?.insights !== false);
        setLicensed(nextLicensed);
        setInsightsEnabled(nextInsights);
        _writeLicenseCache({ licensed: nextLicensed, insights: nextInsights });
      } catch (err) {
        // Network error → keep the cached state if we have one.
        if (!cancelled && _cached == null) setLicensed(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Auto-expand when the user navigates INTO one of our pages (so the
  // active item is visible). Auto-collapse rule matches host NAV_SECTIONS:
  // they don't auto-collapse, so we don't either; just auto-open.
  useEffect(() => {
    if (activePage === "intelligence_chat" || activePage === "intelligence_insights") {
      setExpanded(true);
    }
  }, [activePage]);

  if (!licensed) return null;

  // Mirror the host's collapsed-sidebar abbreviation pattern
  // (section.title.slice(0,2).toUpperCase()).
  const headerText = sidebarCollapsed ? "AI" : "TrustNode Intelligence";

  const renderItem = (pageKey, label) => {
    const active = activePage === pageKey;
    // Match host items 1:1 — same wrapper button classes and the
    // span.nav-icon slot where the menu icon goes. Theme rules already
    // target these classes so dark/light just work.
    if (sidebarCollapsed) {
      return (
        <button
          key={pageKey}
          className={`nav-item nav-icon-only ${active ? "active" : ""}`}
          onClick={() => onNavigate && onNavigate(pageKey)}
          title={label}
        >
          <span className="nav-icon-center"><IntelligenceIcon size={16} /></span>
        </button>
      );
    }
    return (
      <button
        key={pageKey}
        className={`nav-item nav-subitem ${active ? "active" : ""}`}
        onClick={() => onNavigate && onNavigate(pageKey)}
        title={label}
      >
        <span className="nav-icon"><IntelligenceIcon size={14} /></span>
        <span>{label}</span>
      </button>
    );
  };

  return (
    <div className="nav-section">
      <button className="nav-group-btn" onClick={() => setExpanded((v) => !v)}>
        {headerText}
        {!sidebarCollapsed ? <span>{expanded ? "-" : "+"}</span> : null}
      </button>
      {!sidebarCollapsed && expanded ? (
        <>
          {renderItem("intelligence_chat", "Chat")}
          {insightsEnabled ? renderItem("intelligence_insights", "Insights") : null}
        </>
      ) : null}
      {sidebarCollapsed ? (
        <>
          {renderItem("intelligence_chat", "Chat")}
          {insightsEnabled ? renderItem("intelligence_insights", "Insights") : null}
        </>
      ) : null}
    </div>
  );
}
