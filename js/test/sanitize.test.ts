/**
 * Contract for the hidden-unicode neutralizer + HTML escaping + link allowlist.
 * Ported 1:1 from tests/test_sanitize.py — the two rails must agree exactly.
 *
 * Security-critical: exports carry hidden zero-width / bidi / PUA prompt-injection.
 * The renderer must PRESERVE evidence (visible inert badge) but NEUTRALIZE it
 * (never emit the raw invisible, never let it reach a copy surface), while never
 * breaking legitimate emoji (VS16 / in-sequence ZWJ).
 */
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";

import { BITMAP_SHA256, FLAGGED_RANGES, UNIDATA_VERSION } from "../src/generated/unicode-data.js";
import {
  badgeInvisibles,
  isLocalMediaPath,
  isSafeUrl,
  ncrInvisibles,
  neutralizeHtml,
  sanitizeForCopy,
  scanInvisibles,
} from "../src/sanitize.js";

describe("isLocalMediaPath — media sink allowlist", () => {
  // Ported 1:1 from tests/test_sanitize.py — the two rails must agree exactly.
  it("allows genuine relative paths", () => {
    expect(isLocalMediaPath("pic.png")).toBe(true);
    expect(isLocalMediaPath("media/pic.png")).toBe(true);
    expect(isLocalMediaPath("a/b/c/pic.png")).toBe(true);
  });

  it("blocks backslash protocol-relative — the actual bypass", () => {
    // A browser normalises "\" to "/" AFTER a naive check, so these reached the
    // DOM as protocol-relative URLs and fetched remotely. 3/3 measured on Python.
    expect(isLocalMediaPath("/\\evil.example.com/x.png")).toBe(false);
    expect(isLocalMediaPath("\\/evil.example.com/x.png")).toBe(false);
    expect(isLocalMediaPath("\\\\evil.example.com\\share\\x.png")).toBe(false);
  });

  it("blocks traversal and absolute paths", () => {
    expect(isLocalMediaPath("../../../../etc/passwd")).toBe(false);
    expect(isLocalMediaPath("a/../../b.png")).toBe(false);
    expect(isLocalMediaPath("/etc/passwd")).toBe(false);
    expect(isLocalMediaPath("//evil.example.com/x.png")).toBe(false);
  });

  it("blocks schemes and drive letters", () => {
    expect(isLocalMediaPath("https://evil.example.com/x.png")).toBe(false);
    expect(isLocalMediaPath("data:image/svg+xml,<svg onload=alert(1)>")).toBe(false);
    expect(isLocalMediaPath("C:/Windows/System32/x.png")).toBe(false);
    expect(isLocalMediaPath("C:\\Windows\\x.png")).toBe(false);
  });

  it("rejects empty and non-string input", () => {
    expect(isLocalMediaPath("")).toBe(false);
    expect(isLocalMediaPath(null)).toBe(false);
    expect(isLocalMediaPath(123)).toBe(false);
  });
});

describe("generated table integrity", () => {
  it("matches the hash stamped by the Python generator", () => {
    const canon = FLAGGED_RANGES.map(([s, n]) => `${s}:${n}`).join(";");
    expect(createHash("sha256").update(canon, "utf8").digest("hex")).toBe(BITMAP_SHA256);
  });

  it("records which Unicode version the Python rail was built against", () => {
    expect(UNIDATA_VERSION).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it("is sorted and non-overlapping so the binary search is sound", () => {
    for (let i = 1; i < FLAGGED_RANGES.length; i++) {
      const prev = FLAGGED_RANGES[i - 1]!;
      expect(FLAGGED_RANGES[i]![0]).toBeGreaterThan(prev[0] + prev[1] - 1);
    }
  });
});

describe("HTML escaping", () => {
  it("escapes plain text", () => {
    expect(neutralizeHtml("a < b & c > d")).toBe("a &lt; b &amp; c &gt; d");
  });

  it("renders a script tag inert", () => {
    const out = neutralizeHtml("<script>alert(1)</script>");
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });
});

describe("invisible / dangerous codepoints become visible inert badges", () => {
  it("badges a zero-width space", () => {
    const out = neutralizeHtml("a​b");
    expect(out).toContain('data-cp="U+200B"');
    expect(out).not.toContain("​");
    expect(out.startsWith("a")).toBe(true);
    expect(out.endsWith("b")).toBe(true);
  });

  it("badges a bidi override", () => {
    expect(neutralizeHtml("x‮y")).toContain('data-cp="U+202E"');
  });

  it("badges a TAG-block char", () => {
    expect(neutralizeHtml("t\u{E0041}g")).toContain('data-cp="U+E0041"');
  });

  it("badges a private-use char (ChatGPT citation marker)", () => {
    expect(neutralizeHtml("cd")).toContain('data-cp="U+E200"');
  });

  it("leaves tab and newline alone", () => {
    const out = neutralizeHtml("a\tb\nc");
    expect(out).not.toContain("data-cp");
    expect(out).toContain("\t");
    expect(out).toContain("\n");
  });

  it("flags an invisible Hangul filler (category Lo, no category test catches it)", () => {
    expect(neutralizeHtml("aㅤb")).toContain('data-cp="U+3164"');
  });

  it("badges a lone surrogate instead of crashing the build", () => {
    const out = neutralizeHtml("a\uD800b");
    expect(out).toContain('data-cp="U+D800"');
    expect(/[\uD800-\uDFFF]/.test(out.replace(/&#x[0-9A-F]+;/g, ""))).toBe(false);
    expect(sanitizeForCopy("a\uD800b")).toBe("ab");
  });
});

describe("legitimate emoji survive", () => {
  it("preserves VS16 after a pictograph", () => {
    const out = neutralizeHtml("❤️");
    expect(out).toContain("️");
    expect(out).not.toContain("data-cp");
  });

  it("preserves ZWJ inside an emoji sequence", () => {
    const out = neutralizeHtml("\u{1F468}‍\u{1F469}");
    expect(out).toContain("‍");
    expect(out).not.toContain("data-cp");
  });

  it("badges a bare ZWJ", () => {
    expect(neutralizeHtml("a‍b")).toContain('data-cp="U+200D"');
  });

  it("badges a bare VS16 but not one after a pictograph", () => {
    expect(neutralizeHtml("a️b")).toContain('data-cp="U+FE0F"');
    expect(neutralizeHtml("❤️")).not.toContain("data-cp");
  });
});

describe("forensic scan", () => {
  it("reports the codepoints it found", () => {
    const cps = new Set(scanInvisibles("a​b‮c").map(([, cp]) => cp));
    expect(cps.has("U+200B")).toBe(true);
    expect(cps.has("U+202E")).toBe(true);
  });

  it("returns nothing for clean text", () => {
    expect(scanInvisibles("just normal text 123")).toEqual([]);
  });
});

describe("copy / agent-feed surface is stripped, not badged", () => {
  it("strips invisibles", () => {
    expect(sanitizeForCopy("a​‮b")).toBe("ab");
  });

  it("keeps emoji", () => {
    expect(sanitizeForCopy("❤️")).toBe("❤️");
  });
});

describe("link scheme allowlist", () => {
  it("allows http and https", () => {
    expect(isSafeUrl("https://example.com")).toBe(true);
    expect(isSafeUrl("http://x.y/z")).toBe(true);
  });

  it("blocks dangerous schemes", () => {
    expect(isSafeUrl("javascript:alert(1)")).toBe(false);
    expect(isSafeUrl("data:text/html,<script>")).toBe(false);
    expect(isSafeUrl("file:///etc/passwd")).toBe(false);
    expect(isSafeUrl("  JavaScript:alert(1)")).toBe(false);
    expect(isSafeUrl("")).toBe(false);
  });
});

describe("variation-selector smuggling", () => {
  it("is neutralised on every surface", () => {
    // VS1-16 (U+FE00-FE0F) + VS17-256 (U+E0100-E01EF) are category Mn, so NO
    // category test catches them - a 256-value channel carrying arbitrary bytes.
    const payload = [...Buffer.from("SECRET")]
      .map((b) =>
        b < 16 ? String.fromCodePoint(0xfe00 + b) : String.fromCodePoint(0xe0100 + b - 16),
      )
      .join("");
    const text = "ordinary sentence" + payload;
    expect((neutralizeHtml(text).match(/cp-badge/g) ?? []).length).toBeGreaterThanOrEqual(6);
    expect(sanitizeForCopy(text)).toBe("ordinary sentence");
    expect(scanInvisibles(text).length).toBeGreaterThanOrEqual(6);
  });
});

describe("markup-safe variants", () => {
  it("ncrInvisibles stays inert inside an attribute", () => {
    const out = ncrInvisibles('title="a​b"');
    expect(out).toContain("&#x200B;");
    expect(out).not.toContain("<span");
  });

  it("badgeInvisibles leaves surrounding HTML intact", () => {
    const out = badgeInvisibles("<em>a​b</em>");
    expect(out.startsWith("<em>")).toBe(true);
    expect(out.endsWith("</em>")).toBe(true);
    expect(out).toContain('data-cp="U+200B"');
    expect(out).not.toContain("​");
  });
});
