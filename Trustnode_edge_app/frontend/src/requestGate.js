// 2026-11-03. One gate for every background read the UI makes.
//
// The browser allows six connections to the edge. About ten pollers run on
// every page, and a page like Reports fires several reads on mount. Nothing
// stopped them from all being in flight at once, so a slow page elsewhere
// could occupy every socket and the Reports page's three reads expired in
// the browser with nothing wrong on the backend ("Reconnecting to the
// reports service..."). Reads now pass through this gate: at most
// MAX_VISIBLE concurrent while the window is visible, MAX_HIDDEN while it
// is hidden (the pollers keep ticking but trickle), FIFO. Writes never
// queue here - an operator's save is never behind a poll.
//
// Pure module, no DOM: tested with node.

export const MAX_VISIBLE = 4;
export const MAX_HIDDEN = 1;

const state = { active: 0, waiting: [], hidden: false };

export function setHidden(hidden) {
  state.hidden = Boolean(hidden);
  _drain();
}

function _limit() {
  return state.hidden ? MAX_HIDDEN : MAX_VISIBLE;
}

function _drain() {
  while (state.active < _limit() && state.waiting.length) {
    const next = state.waiting.shift();
    state.active += 1;
    next();
  }
}

/** Resolves when a slot is free. Returns a release function; call it once. */
export function acquire() {
  return new Promise((resolve) => {
    const grant = () => {
      let released = false;
      resolve(() => {
        if (released) return;
        released = true;
        state.active = Math.max(0, state.active - 1);
        _drain();
      });
    };
    if (state.active < _limit()) {
      state.active += 1;
      grant();
    } else {
      state.waiting.push(grant);
    }
  });
}

export function gateStats() {
  return { active: state.active, waiting: state.waiting.length, limit: _limit() };
}
