/**
 * FEATURE FLAGS, and today there is exactly one: the maintenance/execute plane (DECISION G-11),
 * OFF by default.
 *
 * WHY THAT PLANE AND NOT ANOTHER. It is simultaneously the highest data-loss surface in the app
 * — it archives, moves and quarantines files under the owner's live Codex session store — and
 * the lowest core value for a new user, whose floor is import / search / read / export. Nothing
 * on that floor reads, writes or even constructs it. This is NOT a repair of its safety model:
 * `maintenancePanel` already gates execution behind a typed confirmation phrase derived from the
 * ALLOWED count, refuses protected targets, plans dry-run first and writes a restorable
 * checkpoint for every run. All of that stays exactly as it is. What was missing is that a
 * stranger who has just imported a ChatGPT export was one Tab away from it.
 *
 * DEFAULT-OFF IS A SAFETY PROPERTY, SO THE PARSER IS ASYMMETRIC. Absent, empty, corrupt,
 * unreadable, unrecognised — every one of those reads as OFF. Only a deliberate opt-in token
 * turns it on. There is no stored value that can enable a destructive plane by accident, which
 * is the property `featureFlags.test.ts` exists to pin.
 *
 * The storage seam is `corpusBar`'s {@link WebStorageLike}, reused rather than re-declared: a
 * flag store and a last-corpus store need the identical three operations and the identical
 * tolerance for a Web Storage that is absent or throws, and two copies of that interface would
 * be two things to keep in sync for no gain.
 *
 * WHERE THE USER FLIPS IT: `ui/maintenanceGate.ts`, a checkbox beside the button it governs.
 * WHAT A HEADLESS PROBE MUST NOW DO: `cockpit/tools/smoke_boot.mjs` and `tools/probe_csp.mjs`
 * both click `#btn-maintenance`, which is hidden while this flag is off — they need
 * `localStorage.setItem("cockpit.features.maintenance", "1")` before load, or they will fail on
 * a button that is deliberately unreachable. Those files are outside this unit's scope, so the
 * fix is reported rather than applied; the key is exported here so the probes never retype it.
 */

import type { WebStorageLike } from "./corpusBar";

/** Web Storage key holding the maintenance plane's opt-in. */
export const MAINTENANCE_FLAG_KEY = "cockpit.features.maintenance";

/**
 * The value written for ON. `"1"` rather than `"true"` for no deep reason — it is short and
 * unambiguous — but it is written in ONE place and read back through {@link parseFeatureFlag},
 * so the two can never disagree.
 *
 * There is deliberately no counterpart for OFF. Off is stored as ABSENCE of the key (see
 * {@link makeFeatureFlag}), so a `serializeFeatureFlag(false)` would be a second representation
 * of off that nothing writes and everything would have to keep parsing forever. Exported
 * because the probes in `cockpit/tools/` have to write it (see this module's header) and should
 * not retype it.
 */
export const FEATURE_ON_TOKEN = "1";

/**
 * Spellings accepted as ON, compared lowercased and trimmed.
 *
 * A deliberate courtesy, not looseness: enabling this is a rare, deliberate act, and the
 * plausible way to do it is by hand in a devtools console. Someone who types `true` and sees
 * nothing happen concludes the switch is broken rather than that they mistyped. Every OTHER
 * value — including `"0"`, `"false"`, `"off"` and anything corrupt — is OFF.
 */
const ON_SPELLINGS: ReadonlySet<string> = new Set([FEATURE_ON_TOKEN, "true", "on", "yes"]);

/** A stored value -> whether the feature is on. Anything unrecognised is OFF. */
export function parseFeatureFlag(raw: string | null): boolean {
  if (raw === null) return false;
  return ON_SPELLINGS.has(raw.trim().toLowerCase());
}

/** One opt-in feature, readable and settable. */
export interface FeatureFlag {
  enabled(): boolean;
  set(on: boolean): void;
}

/**
 * A {@link FeatureFlag} over `storage`, tolerant of every way Web Storage can be absent or
 * refuse: not present at all (`null` — the node test runner), throwing on read (a webview with
 * site data disabled), throwing on write (quota), or throwing on remove.
 *
 * Every failure mode resolves toward the SAFE answer rather than the convenient one. A read
 * that throws is OFF, not "assume what it was". A write that throws leaves the flag off, so a
 * storage-less webview cannot be talked into a half-enabled state that survives one session and
 * not the next. A remove that throws leaves it ON, which is honest for the opposite reason: the
 * plane really is still enabled for the next launch, and reporting otherwise would be the lie.
 *
 * Turning OFF removes the key, so `off` and `never set` are ONE state. A stored `"0"` would be
 * a second representation that has to keep parsing as off forever.
 */
export function makeFeatureFlag(
  storage: WebStorageLike | null,
  key: string = MAINTENANCE_FLAG_KEY,
): FeatureFlag {
  return {
    enabled(): boolean {
      if (storage === null) return false;
      try {
        return parseFeatureFlag(storage.getItem(key));
      } catch {
        return false;
      }
    },
    set(on: boolean): void {
      if (storage === null) return;
      try {
        if (on) storage.setItem(key, FEATURE_ON_TOKEN);
        else storage.removeItem(key);
      } catch {
        // Persisting is best-effort. It must never throw out of a click handler, and the
        // caller re-reads `enabled()` rather than assuming the write took.
      }
    },
  };
}

/**
 * The default flag: `localStorage` when the environment has one. Merely *touching* the global
 * can throw in a hardened webview, so the probe itself is guarded — same as
 * `corpusBar.localCorpusStore`.
 */
export function localFeatureFlag(key: string = MAINTENANCE_FLAG_KEY): FeatureFlag {
  let storage: WebStorageLike | null = null;
  try {
    storage = globalThis.localStorage ?? null;
  } catch {
    storage = null;
  }
  return makeFeatureFlag(storage, key);
}
