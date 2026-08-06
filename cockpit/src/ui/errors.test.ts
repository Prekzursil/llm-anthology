/**
 * Tests for the engine-error presentation rules.
 *
 * The load-bearing case is the LEAK: the literal string `call open_corpus first` must never
 * reach the user. Asserting only the happy path would let a reworded engine error silently
 * reopen it, so the exact strings the Rust layer produces are pinned here.
 */
import { describe, expect, it } from "vitest";

import { NO_CORPUS_MESSAGE, engineErrorText, engineStatusText, isNoCorpusError } from "./errors";

// Verbatim from cockpit/src-tauri/src/lib.rs — the `forward` helper's None arm.
const RAW_ENGINE_ERROR = "no corpus attached: call open_corpus first";

describe("isNoCorpusError", () => {
  it("recognises the engine's not-attached error", () => {
    expect(isNoCorpusError(RAW_ENGINE_ERROR)).toBe(true);
  });

  it("recognises it when a command has wrapped it in a prefix", () => {
    expect(isNoCorpusError(`corpus_stats: ${RAW_ENGINE_ERROR}`)).toBe(true);
  });

  it("recognises it inside a real Error object", () => {
    expect(isNoCorpusError(new Error(RAW_ENGINE_ERROR))).toBe(true);
  });

  it("is case-insensitive, so a reworded engine message does not reopen the leak", () => {
    expect(isNoCorpusError("No Corpus Attached: whatever")).toBe(true);
  });

  it("does NOT match a genuine failure", () => {
    expect(isNoCorpusError(new Error("unable to open database file"))).toBe(false);
    expect(isNoCorpusError("database disk image is malformed")).toBe(false);
  });

  it("does not match null/undefined into a false positive", () => {
    expect(isNoCorpusError(null)).toBe(false);
    expect(isNoCorpusError(undefined)).toBe(false);
  });
});

describe("engineStatusText", () => {
  it("goes SILENT for the not-attached state", () => {
    // Measured in the installed app: routing every status line through engineErrorText put
    // the same sentence in the top bar three times, beside a corpus bar already saying it.
    expect(engineStatusText(RAW_ENGINE_ERROR, "stats unavailable")).toBe("");
    expect(engineStatusText(RAW_ENGINE_ERROR, "engine unavailable")).toBe("");
    expect(engineStatusText(RAW_ENGINE_ERROR, "timeline failed")).toBe("");
  });

  it("does NOT go silent for a genuine fault", () => {
    // Silence for a real error would be a worse defect than the redundancy it replaced.
    const out = engineStatusText(new Error("database disk image is malformed"), "stats unavailable");
    expect(out).toContain("stats unavailable");
    expect(out).toContain("malformed");
  });

  it("never leaks the internal method name either", () => {
    expect(engineStatusText(RAW_ENGINE_ERROR, "stats unavailable")).not.toContain("open_corpus");
  });
});

describe("engineErrorText", () => {
  it("replaces the not-attached error with an actionable instruction", () => {
    const out = engineErrorText(RAW_ENGINE_ERROR, "stats unavailable");
    expect(out).toBe(NO_CORPUS_MESSAGE);
  });

  it("never leaks the internal method name for the not-attached case", () => {
    // This is the assertion that matters: the defect was an internal RPC method name
    // rendered in the UI.
    for (const label of ["stats unavailable", "engine unavailable", "timeline failed"]) {
      expect(engineErrorText(RAW_ENGINE_ERROR, label)).not.toContain("open_corpus");
    }
  });

  it("drops the label for the not-attached case, so it does not read as two problems", () => {
    expect(engineErrorText(RAW_ENGINE_ERROR, "stats unavailable")).not.toContain("unavailable:");
  });

  it("differs from engineStatusText ONLY for the not-attached case", () => {
    // The two must not drift into two behaviours for real errors — the whole distinction is
    // about the not-attached state, nothing else.
    const real = new Error("database disk image is malformed");
    expect(engineStatusText(real, "stats unavailable")).toBe(
      engineErrorText(real, "stats unavailable"),
    );
  });

  it("KEEPS a genuine error's real message and its label", () => {
    // Suppressing real faults would trade a confusing string for an invisible failure.
    const out = engineErrorText(new Error("database disk image is malformed"), "stats unavailable");
    expect(out).toContain("stats unavailable");
    expect(out).toContain("malformed");
  });
});
