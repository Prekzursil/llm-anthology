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

const TINTS: Record<string, ProviderTint> = {
  claude: { fill: "#5b3fa8", stroke: "#8b6fd6", text: "#f4f0ff" },
  codex: { fill: "#0f6d5f", stroke: "#33b8a3", text: "#eafff9" },
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
