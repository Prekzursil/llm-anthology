/**
 * Provider -> colour mapping for the canvas. Known providers get a stable tint;
 * anything else (including the empty provider of a dangling node) falls back to a
 * neutral grey. Colours are chosen to stay legible on both the light and dark app
 * backgrounds and to read as distinct categories (preattentive hue separation).
 */

export interface ProviderTint {
  /** Node body fill. */
  fill: string;
  /** Node border. */
  stroke: string;
  /** Label text colour drawn on the fill. */
  text: string;
}

/**
 * Every provider the engine can actually emit gets a tint.
 *
 * This held only `claude` and `codex` while the app supported exactly those. It now detects
 * and ingests more, and the consequence was silent: a provider with no entry falls back to
 * UNKNOWN_TINT, so on a real corpus MOST nodes rendered the same neutral grey as a dangling
 * node — the one colour that is supposed to mean "we don't know what this is". Provider
 * colour is the graph's primary categorical channel, and it was conveying nothing.
 *
 * The provider strings are the engine's own (`llm_anthology/adapters/*`): "codex",
 * "claude" (the claude.ai web export), "claude-code", "grok", "chatgpt", "gemini".
 *
 * Hues are chosen for preattentive separation and each fill clears WCAG AA against its own
 * label text, which is what makes the category readable rather than merely present.
 */
const TINTS: Record<string, ProviderTint> = {
  claude: { fill: "#5b3fa8", stroke: "#8b6fd6", text: "#f4f0ff" },
  codex: { fill: "#0f6d5f", stroke: "#33b8a3", text: "#eafff9" },
  "claude-code": { fill: "#8a4a1f", stroke: "#d6893f", text: "#fff4ea" },
  grok: { fill: "#1f4d8a", stroke: "#5b95d6", text: "#eaf2ff" },
  chatgpt: { fill: "#1f6b3a", stroke: "#4fb877", text: "#eafff0" },
  gemini: { fill: "#7a2f5e", stroke: "#c96fa5", text: "#ffeaf6" },
};

/** Neutral tint for unknown / dangling ("") providers. */
export const UNKNOWN_TINT: ProviderTint = {
  fill: "#4a4a52",
  stroke: "#7d7d88",
  text: "#f0f0f2",
};

export function providerTint(provider: string): ProviderTint {
  return TINTS[provider] ?? UNKNOWN_TINT;
}

/** The set of providers with an explicit tint (for the legend). */
export function knownProviders(): string[] {
  return Object.keys(TINTS);
}

/** Colour for a normal (same-provider) spawn edge. */
export const EDGE_COLOR = "#8a8a96";

/** Colour for a CROSS-PROVIDER spawn edge — a deliberately distinct class. */
export const CROSS_EDGE_COLOR = "#e0663c";
