/**
 * The palette must cover every provider the ENGINE can put on a node.
 *
 * Why this needs a test at all: the fallback for an unknown provider is a neutral grey that
 * legitimately means "dangling / we don't know what this is". So a provider with no tint does
 * not look like a bug — it looks like a correct unknown. That is exactly how the app shipped
 * with tints for only `claude` and `codex` while the engine emitted six, leaving most nodes on
 * a real corpus rendering as the "unknown" colour. Provider is the graph's primary categorical
 * channel and it was conveying nothing.
 *
 * A test that only pinned the TS list would not have caught it, because the regression happens
 * on the PYTHON side — an adapter gains a provider and nothing here changes. So this reads the
 * engine's own registry and diffs the two sets.
 */
import { describe, expect, it } from "vitest";

// Read as a Vite `?raw` asset rather than via `node:fs`. The cockpit tsconfig is
// browser-only and has no `@types/node`; adding it to satisfy one test would let APP code
// typecheck references to `process`/`Buffer` that do not exist in the Tauri webview. Vite's
// client types already declare `*?raw`, so this costs nothing and stays honest about the
// runtime. (`build` is `tsc && vite build`, so a type error here breaks the release build.)
import DISCOVER_PY from "../../../llm_anthology/discover.py?raw";

import { knownProviders, providerTint, UNKNOWN_TINT } from "./palette";

/**
 * `anthology` is the label `discover.py` gives a *previously built index file* it finds on
 * disk. It is a source KIND, not a chat provider — the conversations inside such an index
 * carry their own original provider — so no node is ever tagged with it and it deliberately
 * has no tint. Everything else in the registry is a real conversation provider.
 */
const NOT_A_NODE_PROVIDER = new Set(["anthology"]);

function engineProviders(): string[] {
  const found = [...DISCOVER_PY.matchAll(/provider\s*=\s*["']([a-z0-9._-]+)["']/g)]
    .map((m) => m[1]);
  return [...new Set(found)].filter((p) => !NOT_A_NODE_PROVIDER.has(p)).sort();
}

describe("provider palette", () => {
  it("can actually read the engine registry", () => {
    // Guard the guard. If the relative path or the regex ever stops matching, every
    // assertion below would pass vacuously against an empty list.
    const providers = engineProviders();
    expect(providers.length).toBeGreaterThan(3);
    expect(providers).toContain("codex");
  });

  it("gives every engine provider its own tint", () => {
    const missing = engineProviders().filter((p) => !knownProviders().includes(p));
    expect(
      missing,
      `these providers exist in the engine but render as the "unknown" grey: ${missing.join(", ")}`,
    ).toEqual([]);
  });

  it("has no tint for a provider the engine cannot emit", () => {
    // The other direction: a stale entry is dead weight and misleads the legend.
    const engine = new Set([...engineProviders(), ...NOT_A_NODE_PROVIDER]);
    expect(knownProviders().filter((p) => !engine.has(p))).toEqual([]);
  });

  it("has NO tint for a model VENDOR, which is a different field", () => {
    // The engine carries two facts that both read like "provider": the ADAPTER
    // (ThreadNode.provider — "codex") and the MODEL VENDOR (ThreadNode.model_provider —
    // "openai"). Graph nodes used to deliver the vendor under the name `provider`, and
    // because no vendor string is a palette key, every Codex node on a real corpus drew
    // the "unknown" grey. Measured: 'openai' in 92.8% of 250 real rollouts, absent in the
    // rest, never "codex".
    //
    // The tempting wrong fix is to add "openai" here and watch the grey go away. That
    // would paint by vendor and silently merge Codex and ChatGPT into one category. This
    // pins the right fix: vendors are NOT palette keys — tint by `provider`.
    for (const vendor of ["openai", "anthropic", "google", "xai"]) {
      expect(knownProviders(), `${vendor} is a model vendor, not an adapter`)
        .not.toContain(vendor);
      expect(providerTint(vendor)).toBe(UNKNOWN_TINT);
    }
  });

  it("falls back to the unknown tint for a dangling node", () => {
    // A dangling spawn edge points at an id with no record, so its provider is "".
    expect(providerTint("")).toBe(UNKNOWN_TINT);
    expect(providerTint("not-a-real-provider")).toBe(UNKNOWN_TINT);
  });

  it("gives each provider a visually distinct fill", () => {
    // Same-coloured categories are the same failure as no colour at all.
    const fills = knownProviders().map((p) => providerTint(p).fill);
    expect(new Set(fills).size).toBe(fills.length);
  });

  it("keeps label text legible on its own fill", () => {
    // The categorical channel is worthless if the label on it cannot be read. WCAG AA for
    // normal text is 4.5:1; these are short bold-ish labels but AA is the honest bar.
    for (const provider of [...knownProviders(), "unknown"]) {
      const tint = provider === "unknown" ? UNKNOWN_TINT : providerTint(provider);
      expect(contrastRatio(tint.fill, tint.text), `${provider} label on its fill`)
        .toBeGreaterThanOrEqual(4.5);
    }
  });
});

/** WCAG 2.x relative-luminance contrast ratio between two #rrggbb colours. */
function contrastRatio(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

function luminance(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m) throw new Error(`not a #rrggbb colour: ${hex}`);
  const channels = [0, 2, 4].map((i) => {
    const c = parseInt(m[1].slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}
