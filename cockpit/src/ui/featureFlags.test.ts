/**
 * The maintenance plane's opt-in (DECISION G-11), which is a SAFETY default and so is tested
 * as one.
 *
 * The plane it gates is the highest data-loss surface in the app — it archives, moves and
 * quarantines files under the owner's live Codex store — and the lowest core value for a new
 * user, whose floor is import / search / read / export. It already has a real safety model
 * (typed confirmation, protected targets, dry-run default, checkpoint/restore); this is not a
 * repair of that model, it is the switch that keeps the whole plane out of reach until someone
 * deliberately asks for it.
 *
 * Which makes the ASYMMETRY the point of this file: every ambiguous, corrupt, absent or hostile
 * stored value must read as OFF. There is no input a test here can find that turns a
 * destructive plane on by accident, and that is the property, not the tidiness of the parser.
 *
 * Node environment: `makeFeatureFlag` takes its storage as an argument precisely so this needs
 * no DOM, the same seam `corpusBar`'s `makeCorpusStore` uses.
 */
import { describe, expect, it } from "vitest";

import type { WebStorageLike } from "./corpusBar";
import {
  FEATURE_ON_TOKEN,
  MAINTENANCE_FLAG_KEY,
  localFeatureFlag,
  makeFeatureFlag,
  parseFeatureFlag,
} from "./featureFlags";

/** A Web Storage stand-in over a Map, with the throwing modes a hardened webview has. */
function fakeStorage(
  seed: Record<string, string> = {},
  fail: { read?: boolean; write?: boolean; remove?: boolean } = {},
): WebStorageLike & { readonly map: Map<string, string> } {
  const map = new Map(Object.entries(seed));
  return {
    map,
    getItem(key) {
      if (fail.read === true) throw new Error("site data disabled");
      return map.get(key) ?? null;
    },
    setItem(key, value) {
      if (fail.write === true) throw new Error("quota exceeded");
      map.set(key, value);
    },
    removeItem(key) {
      if (fail.remove === true) throw new Error("site data disabled");
      map.delete(key);
    },
  };
}

describe("parseFeatureFlag — default OFF", () => {
  it("reads an absent value as OFF", () => {
    expect(parseFeatureFlag(null)).toBe(false);
  });

  it("reads the token it writes as ON", () => {
    // Round-trip, so a change to the written token cannot silently stop being readable. There
    // is no token for OFF on purpose -- off is the ABSENCE of the key, one representation
    // rather than two.
    expect(parseFeatureFlag(FEATURE_ON_TOKEN)).toBe(true);
  });

  it("accepts the spellings a human would hand-type, trimmed and case-insensitive", () => {
    // Not generosity for its own sake: someone who edits storage by hand and types `true`
    // then sees nothing happen concludes the switch is broken, not that they mistyped.
    for (const raw of ["1", "true", "TRUE", " on ", "yes", "On"]) {
      expect(parseFeatureFlag(raw)).toBe(true);
    }
  });

  it("reads EVERY other value as OFF, including the plausible negatives", () => {
    for (const raw of [
      "",
      " ",
      "0",
      "false",
      "off",
      "no",
      "null",
      "undefined",
      "2",
      "enabled",
      "{}",
      "[object Object]",
      "\u0000",
      "tru",
      "1 1",
    ]) {
      expect(parseFeatureFlag(raw)).toBe(false);
    }
  });
});

describe("makeFeatureFlag", () => {
  it("is OFF on a machine that has never set it", () => {
    expect(makeFeatureFlag(fakeStorage()).enabled()).toBe(false);
  });

  it("is ON once set, and the value lands under the documented key", () => {
    const storage = fakeStorage();
    const flag = makeFeatureFlag(storage);
    flag.set(true);
    expect(flag.enabled()).toBe(true);
    expect(storage.map.get(MAINTENANCE_FLAG_KEY)).toBe(FEATURE_ON_TOKEN);
  });

  it("REMOVES the key when turned off, so off and never-set are the same state", () => {
    // One representation of off rather than two. A stored `"0"` would be a second thing that
    // has to keep parsing as off forever.
    const storage = fakeStorage({ [MAINTENANCE_FLAG_KEY]: "1" });
    const flag = makeFeatureFlag(storage);
    flag.set(false);
    expect(flag.enabled()).toBe(false);
    expect(storage.map.has(MAINTENANCE_FLAG_KEY)).toBe(false);
  });

  it("is OFF with no storage at all — the node runner, and a webview with none", () => {
    const flag = makeFeatureFlag(null);
    expect(flag.enabled()).toBe(false);
    flag.set(true);
    // Nothing to persist into, so nothing persisted. It must not pretend otherwise.
    expect(flag.enabled()).toBe(false);
  });

  it("is OFF when the storage READ throws", () => {
    // A hardened webview with site data disabled throws on access. The corpus store treats
    // that as "nothing remembered"; here it has to fail CLOSED, which is the same direction
    // but for a much sharper reason.
    expect(
      makeFeatureFlag(fakeStorage({ [MAINTENANCE_FLAG_KEY]: "1" }, { read: true })).enabled(),
    ).toBe(false);
  });

  it("survives a WRITE that throws, leaving the flag as it was", () => {
    const flag = makeFeatureFlag(fakeStorage({}, { write: true }));
    expect(() => flag.set(true)).not.toThrow();
    expect(flag.enabled()).toBe(false);
  });

  it("survives a REMOVE that throws", () => {
    // Refusing to forget must not throw out of a click handler. The flag stays ON, which is
    // honest: the plane really is still enabled for the next launch.
    const flag = makeFeatureFlag(fakeStorage({ [MAINTENANCE_FLAG_KEY]: "1" }, { remove: true }));
    expect(() => flag.set(false)).not.toThrow();
    expect(flag.enabled()).toBe(true);
  });

  it("honours a caller-chosen key, so one store can hold more than one flag", () => {
    const storage = fakeStorage();
    const other = makeFeatureFlag(storage, "cockpit.features.somethingElse");
    other.set(true);
    expect(storage.map.has(MAINTENANCE_FLAG_KEY)).toBe(false);
    expect(makeFeatureFlag(storage).enabled()).toBe(false);
    expect(other.enabled()).toBe(true);
  });
});

describe("localFeatureFlag", () => {
  it("reads OFF under the node runner, where localStorage does not exist", () => {
    // The same premise `corpusBar.test.ts` asserts on purpose, and the reason a global DOM
    // flip is not an option for this suite (`vitest.config.ts`).
    expect(globalThis.localStorage).toBeUndefined();
    expect(localFeatureFlag().enabled()).toBe(false);
  });

  it("reads OFF in a webview where merely TOUCHING localStorage throws", () => {
    // A DIFFERENT failure from a storage whose METHODS throw, and the reason the probe itself
    // is wrapped rather than only the calls: with site data disabled some webviews throw on the
    // property READ, so there is never an object to call `getItem` on. Same shape as
    // `corpusBar.test.ts:239-262`, and here it is the difference between failing closed and
    // taking the whole top bar down with a `SecurityError` out of a constructor.
    const before = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get(): never {
        throw new Error("SecurityError: access to storage is denied");
      },
    });
    try {
      const flag = localFeatureFlag();
      expect(flag.enabled()).toBe(false);
      // And it stays off however hard it is asked: with no storage there is nowhere for an
      // opt-in to live, so claiming one took would be a lie that survives exactly one session.
      expect(() => flag.set(true)).not.toThrow();
      expect(flag.enabled()).toBe(false);
    } finally {
      if (before === undefined) Reflect.deleteProperty(globalThis, "localStorage");
      else Object.defineProperty(globalThis, "localStorage", before);
    }
  });
});

describe("the key itself", () => {
  it("is namespaced under the app, so it cannot collide with the corpus key", () => {
    expect(MAINTENANCE_FLAG_KEY.startsWith("cockpit.")).toBe(true);
    expect(MAINTENANCE_FLAG_KEY).toContain("maintenance");
  });
});
