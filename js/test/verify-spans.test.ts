/**
 * `htmlVisibleTokens` drops the badge, `<style>` and `<head>` spans with a single linear
 * scan (verify.ts `dropSpans`) instead of a lazy `[\s\S]*?` regex. These cases pin the
 * spans the regex matched — opener to the FIRST closer after it, case-insensitively, and
 * nothing at all when no closer follows — and the linear bound that replaced it.
 */
import { describe, expect, it } from "vitest";

import { htmlVisibleTokens } from "../src/verify.js";

describe("htmlVisibleTokens span dropping", () => {
  it("drops each opener up to the first closer after it, in any letter case", () => {
    const h =
      '<p>keep</p><SPAN CLASS="cp-badge">zw</span>tail</span>' +
      "<style>p { color: red }</STYLE><head><title>gone</title></HEAD>end";
    expect(htmlVisibleTokens(h)).toEqual(["keep", "tail", "end"]);
  });

  it("keeps an opener that no closer follows, as the regex did", () => {
    expect(htmlVisibleTokens('<span class="cp-badge">orphan<style>still')).toEqual([
      "orphan",
      "still",
    ]);
  });

  it("stays linear on many openers and no closer", () => {
    // 50,000 openers with no closer: the lazy regex re-scanned from every opener to the
    // end of this ~1.2 MB string, which is quadratic. One pass takes milliseconds.
    const h = '<span class="cp-badge">x'.repeat(50_000);
    const started = performance.now();
    const tokens = htmlVisibleTokens(h);
    expect(performance.now() - started).toBeLessThan(5_000);
    expect(tokens).toHaveLength(50_000);
  });
});

describe("htmlVisibleTokens tag stripping", () => {
  it("cuts each tag from its < to the first > after it, as /<[^>]+>/g did", () => {
    expect(htmlVisibleTokens("a<b<c>d")).toEqual(["a", "d"]);
  });

  it("leaves <> as text and stops at a < that no > follows", () => {
    expect(htmlVisibleTokens("x<>y<z>w")).toEqual(["x", "y", "w"]);
    expect(htmlVisibleTokens("a<b")).toEqual(["a", "b"]);
  });

  it("stays linear on many < and no >", () => {
    // the regex retried from every `<` to the end of this string: quadratic
    const started = performance.now();
    expect(htmlVisibleTokens(`${"<".repeat(50_000)}x`)).toEqual(["x"]);
    expect(performance.now() - started).toBeLessThan(5_000);
  });
});
