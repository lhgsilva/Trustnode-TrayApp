/* Test entry: mount DashboardDesigner alone, with stub props, and expose a
   small driver on window so a jsdom script can DRIVE it.
 *
 * Built by vite.designer.config.mjs into dist_designer/ and executed by
 * scripts/test_dashboard_text_entry.mjs. It exists because "the text field
 * does not let me enter text" cannot be answered by reading JSX - a
 * controlled input that re-renders its old value looks identical in source
 * to one that works.
 *
 * Not shipped: nothing in the app imports this, and the release build does
 * not produce dist_designer.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { DashboardDesigner } from "./src/components/Dashboard/DashboardDesigner.jsx";
import { ReportTemplateDesigner } from "./src/components/Reports/ReportTemplateDesigner.jsx";

const noop = () => {};
const empty = async () => [];

function Harness() {
  const [widgets, setWidgets] = React.useState([]);
  const [profiles, setProfiles] = React.useState([]);
  // The real Dashboard page re-renders constantly: live tag values arrive
  // about once a second and the parent re-renders with a NEW object identity
  // for several props. If an ancestor re-render resets the editor's local
  // form state, typing is impossible - which is what the operator reported.
  // Drive that here, fast, so the test would catch it.
  const [tick, setTick] = React.useState(0);
  React.useEffect(() => {
    if (!window.__churn) return undefined;
    const t = setInterval(() => setTick((n) => n + 1), 60);
    return () => clearInterval(t);
  }, []);
  window.__widgets = widgets;
  window.__renders = (window.__renders || 0) + 1;
  return (
    <DashboardDesigner
      canEdit
      widgets={widgets}
      setWidgets={(next) => setWidgets(typeof next === "function" ? next(widgets) : next)}
      serverProfiles={profiles}
      onProfilesChange={setProfiles}
      widgetLatencyById={{ tick }}
      gatewayIntervalsById={{ tick }}
      triggerRules={[]}
      tagRows={[]}
      dataLogView={[]}
      formatTagForDisplay={(t) => String(t || "")}
      fetchHistoricalRows={empty}
      gatewayCatalog={[]}
      tagsByGateway={{}}
      onOpenTagMonitor={noop}
      onExportDiagnostics={noop}
      fetchWidgetRows={empty}
      fetchWidgetStats={async () => ({})}
      fetchWidgetRuleStats={async () => ({})}
    />
  );
}

/* Real gateways and tags, because an EMPTY list makes every picker in the
 * page look identical to a broken one. 2026-10-11, reported: "I cannnot
 * select the gateway and tags" on a section trigger - a harness that passed
 * [] could never have caught a section that forgets to hand the options
 * down. */
const HARNESS_GATEWAYS = [
  { id: "gw-plc", name: "PLC GW", power_meter: false },
  { id: "gw-meter", name: "EM1 Meter", power_meter: true },
];
const HARNESS_TAGS = {
  "gw-plc": ["Cycle_Active", "Phase_Name", "Temp_C", "Pressure_bar"],
  "gw-meter": ["active_power_w", "voltage_v"],
};

/* The reports template designer, mounted the way App.jsx mounts it: with
   onNotify as an INLINE arrow whose identity changes on every render, and
   with a notification re-rendering the parent. That combination is what
   turned a retry effect into an unthrottled fetch loop on 2026-10-07. */
function ReportsHarness() {
  const [banner, setBanner] = React.useState("");
  // App.jsx re-renders continuously - live tag values, gateway status and
  // runtime polling all land every second or so. That is the other half of
  // the loop: each parent render hands down a NEW inline onNotify, so an
  // effect that depends on it re-runs that often. Without this churn the
  // harness cannot see the bug (it only re-rendered when the banner text
  // changed, and React bails out on an identical value).
  const [tick, setTick] = React.useState(0);
  React.useEffect(() => {
    if (!window.__churn) return undefined;
    const t = setInterval(() => setTick((n) => n + 1), 60);
    return () => clearInterval(t);
  }, []);
  window.__renders = (window.__renders || 0) + 1;
  window.__tick = tick;
  return (
    <>
      <div>{banner}</div>
      <ReportTemplateDesigner
        gatewayOptions={HARNESS_GATEWAYS}
        tagsByGateway={HARNESS_TAGS}
        emailSettings={{}}
        formatTagForDisplay={(t) => String(t || "")}
        onNotify={(n) => {
          // New function identity every render, and it sets state - exactly
          // what App.jsx does.
          if (n?.type === "error") setBanner(String(n.message || ""));
        }}
      />
    </>
  );
}

const host = document.getElementById("root");
createRoot(host).render(
  window.__harness === "reports" ? <ReportsHarness /> : <Harness />);
window.__mounted = true;
