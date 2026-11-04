/**
 * Cross-language parity gate: the JS rail must reproduce the Python rail's OUTPUT
 * byte for byte.
 *
 * The generated unicode table already pins WHICH codepoints are flagged. This
 * covers everything layered on top — the position-aware emoji rules, badge/NCR
 * formatting, and HTML escaping — none of which lives in the table. Regenerate the
 * fixture with `python tools/gen-parity-fixture.py` whenever either rail changes.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { UNIDATA_VERSION } from "../src/generated/unicode-data.js";
import {
  badgeInvisibles,
  isSafeUrl,
  ncrInvisibles,
  neutralizeHtml,
  sanitizeForCopy,
  scanInvisibles,
} from "../src/sanitize.js";

interface ParityCase {
  input: string;
  neutralize_html: string;
  badge_invisibles: string;
  ncr_invisibles: string;
  sanitize_for_copy: string;
  scan_invisibles: Array<[number, string]>;
}

const fixture = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fixtures/sanitize-parity.json", import.meta.url)), "utf8"),
) as {
  unidata_version: string;
  cases: ParityCase[];
  urls: Array<{ url: string; safe: boolean }>;
};

/** So a failure names the offending codepoints instead of printing invisibles. */
const show = (s: string): string =>
  [...s]
    .map((c) =>
      c.codePointAt(0)! < 0x7f ? c : `<U+${c.codePointAt(0)!.toString(16).toUpperCase()}>`,
    )
    .join("");

describe("python <-> js parity", () => {
  it("was generated against the same Unicode version the table was built from", () => {
    expect(fixture.unidata_version).toBe(UNIDATA_VERSION);
  });

  it("covers a non-trivial battery", () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(30);
  });

  for (const [idx, c] of fixture.cases.entries()) {
    describe(`case ${idx}: ${show(c.input).slice(0, 48)}`, () => {
      it("neutralizeHtml", () => {
        expect(neutralizeHtml(c.input)).toBe(c.neutralize_html);
      });
      it("badgeInvisibles", () => {
        expect(badgeInvisibles(c.input)).toBe(c.badge_invisibles);
      });
      it("ncrInvisibles", () => {
        expect(ncrInvisibles(c.input)).toBe(c.ncr_invisibles);
      });
      it("sanitizeForCopy", () => {
        expect(sanitizeForCopy(c.input)).toBe(c.sanitize_for_copy);
      });
      it("scanInvisibles", () => {
        expect(scanInvisibles(c.input)).toEqual(c.scan_invisibles);
      });
    });
  }

  for (const { url, safe } of fixture.urls) {
    it(`isSafeUrl(${JSON.stringify(url)}) === ${safe}`, () => {
      expect(isSafeUrl(url)).toBe(safe);
    });
  }
});
