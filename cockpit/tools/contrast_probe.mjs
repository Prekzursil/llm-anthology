/**
 * An INDEPENDENT WCAG contrast probe for the cockpit UI.
 *
 * Why this exists: the ui-audit skill's own `wcag-contrast` rule
 * (`layout_lint.mjs:240-261`) only evaluates an element when that element's OWN
 * `background-color` has alpha > 0.1. It never walks up the ancestor chain. Since a
 * transparent background is the CSS default, most text in this app is silently SKIPPED
 * by that rule — so "0 wcag-contrast findings" is not evidence that contrast passes.
 * This probe resolves the EFFECTIVE background by compositing up the ancestor chain,
 * which is a mechanically different measurement.
 *
 * It also carries a both-states CONTROL: two injected probe nodes with known-bad and
 * known-good ratios. If the control does not flag exactly the bad one, this probe is
 * broken and its silence means nothing — so it exits non-zero instead of reporting a
 * pass. A detector that cannot fail cannot verify.
 *
 * Usage: node tools/contrast_probe.mjs <url> [--json <out>]
 */
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// playwright is CJS and only installed under the ui-audit skill, so resolve it with a
// require() rooted there. A file-URL `import()` returns undefined named exports for it.
const SKILL = join(homedir(), ".claude", "skills", "ui-audit", "package.json");
const requireFromSkill = createRequire(pathToFileURL(SKILL));

const url = process.argv[2] ?? "http://localhost:5199";
const jsonIdx = process.argv.indexOf("--json");
const jsonOut = jsonIdx > -1 ? process.argv[jsonIdx + 1] : null;

/**
 * Collect, for every element bearing its own text, the foreground colour, the
 * effective (ancestor-composited) background, the WCAG ratio, and the AA threshold
 * that applies at its font size/weight. Runs inside the page.
 */
const COLLECT = () => {
  const parse = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || "");
    if (!m) return null;
    const p = m[1].split(",").map((x) => parseFloat(x.trim()));
    if (p.length < 3 || p.some((n) => Number.isNaN(n))) return null;
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });
  const lum = (c) => {
    const f = (v) => {
      const n = v / 255;
      return n <= 0.03928 ? n / 12.92 : Math.pow((n + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };

  /**
   * Composite backgrounds from the element up to the root. `seed` is the pseudo-element's
   * own background when measuring generated content, since that layer sits in front of
   * the host's.
   */
  const effectiveBg = (el, seed) => {
    let acc = seed && seed.a > 0 ? seed : null;
    let img = false;
    if (acc && acc.a >= 0.999) return { bg: acc, hadImage: false, resolved: true };
    for (let n = el; n; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.backgroundImage && st.backgroundImage !== "none") img = true;
      const c = parse(st.backgroundColor);
      if (!c || c.a === 0) continue;
      acc = acc === null ? c : over(acc, c);
      if (acc.a >= 0.999) return { bg: acc, hadImage: img, resolved: true };
    }
    // Nothing opaque found: composite what we have over the canvas default (white).
    const base = { r: 255, g: 255, b: 255, a: 1 };
    return { bg: acc === null ? base : over(acc, base), hadImage: img, resolved: acc !== null };
  };

  const ownText = (el) =>
    Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .join(" ")
      .trim();

  const cssPath = (el) => {
    const bits = [];
    for (let n = el; n && n.nodeType === 1 && bits.length < 4; n = n.parentElement) {
      let s = n.tagName.toLowerCase();
      if (n.id) {
        bits.unshift(`${s}#${n.id}`);
        break;
      }
      if (n.classList.length) s += `.${Array.from(n.classList).join(".")}`;
      bits.unshift(s);
    }
    return bits.join(" > ");
  };

  const out = [];
  for (const el of document.querySelectorAll("*")) {
    const st = getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;

    // Text can come from a real text node OR from generated content (the empty-state
    // labels are `::after { content: attr(data-empty) }`, which has no text node).
    const afterSt = getComputedStyle(el, "::after");
    const after = afterSt.content;
    const generated = after && after !== "none" && after !== "normal" ? after.replace(/^"|"$/g, "") : "";
    const direct = ownText(el);
    const text = direct || generated;
    if (!text) continue;

    // A pseudo-element carries its OWN colour and font metrics. Reading the host's
    // instead silently measures the wrong pair — it reported --text/14px for a label
    // the stylesheet sets to --muted/12px.
    const styleForText = direct ? st : afterSt;

    const fgRaw = parse(styleForText.color);
    if (!fgRaw) continue;
    const { bg, hadImage, resolved } = effectiveBg(el, direct ? null : parse(afterSt.backgroundColor));
    const fg = fgRaw.a < 1 ? over(fgRaw, bg) : fgRaw;

    const size = parseFloat(styleForText.fontSize) || 16;
    const weight = parseInt(styleForText.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const threshold = large ? 3.0 : 4.5;

    const ownBg = parse(styleForText.backgroundColor);
    out.push({
      selector: cssPath(el),
      text: text.slice(0, 60),
      fg: styleForText.color,
      bgResolved: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
      ownBgAlpha: ownBg ? ownBg.a : 0,
      // The exact condition at layout_lint.mjs:247 — anything false here was skipped.
      measuredByAuditSkill: !!(ownBg && ownBg.a > 0.1),
      fromGeneratedContent: !ownText(el) && !!generated,
      ratio: Math.round(ratio(fg, bg) * 100) / 100,
      threshold,
      large,
      fontSize: size,
      fontWeight: weight,
      pass: ratio(fg, bg) >= threshold,
      bgHadImage: hadImage,
      bgFullyResolved: resolved,
    });
  }
  return out;
};

/** Inject known-bad and known-good nodes so the probe's verdict is falsifiable. */
const CONTROL = () => {
  const host = document.createElement("div");
  host.id = "__contrast_control__";
  host.style.cssText = "position:fixed;left:0;top:0;z-index:99999;background:#0e0e11;padding:4px";
  const bad = document.createElement("div");
  bad.id = "__contrast_control_bad__";
  // #3a3a44 on #0e0e11 is ~1.7:1 — must be flagged by any working checker.
  bad.style.cssText = "color:#3a3a44;background:#0e0e11;font-size:14px";
  bad.textContent = "control bad";
  const good = document.createElement("div");
  good.id = "__contrast_control_good__";
  good.style.cssText = "color:#ffffff;background:#0e0e11;font-size:14px";
  good.textContent = "control good";
  host.append(bad, good);
  document.body.appendChild(host);
};

const main = async () => {
  const { chromium } = requireFromSkill("playwright");
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForSelector("#app");
  await page.waitForTimeout(1800);

  // --- both-states control -------------------------------------------------
  await page.evaluate(CONTROL);
  const withControl = await page.evaluate(COLLECT);
  const bad = withControl.find((e) => e.selector.includes("__contrast_control_bad__"));
  const good = withControl.find((e) => e.selector.includes("__contrast_control_good__"));
  const controlOk = bad && good && bad.pass === false && good.pass === true;
  console.log("=== both-states control ===");
  console.log(`  known-BAD  : ${bad ? `${bad.ratio}:1 flagged=${!bad.pass}` : "NOT FOUND"}`);
  console.log(`  known-GOOD : ${good ? `${good.ratio}:1 flagged=${!good.pass}` : "NOT FOUND"}`);
  if (!controlOk) {
    console.log("\nFAILED:contrast-probe control did not fire — this probe measures nothing.");
    await browser.close();
    process.exit(2);
  }
  console.log("  control OK — the probe can distinguish fail from pass.\n");

  await page.evaluate(() => document.getElementById("__contrast_control__")?.remove());
  const rows = await page.evaluate(COLLECT);
  await browser.close();

  const fails = rows.filter((r) => !r.pass);
  const skipped = rows.filter((r) => !r.measuredByAuditSkill);
  console.log("=== effective-background contrast (dark theme) ===");
  console.log(`  text-bearing elements measured : ${rows.length}`);
  console.log(`  of those, SKIPPED by ui-audit  : ${skipped.length}  (own bg alpha <= 0.1)`);
  console.log(`  failures vs WCAG AA            : ${fails.length}`);
  if (rows.some((r) => r.bgHadImage)) {
    console.log(`  NOTE: ${rows.filter((r) => r.bgHadImage).length} element(s) sit over a background-image; ratio is colour-only.`);
  }
  for (const f of fails) {
    console.log(
      `  FAIL ${f.ratio}:1 < ${f.threshold}:1  ${f.selector}\n` +
        `       fg=${f.fg} bg=${f.bgResolved} ${f.fontSize}px/${f.fontWeight}` +
        `${f.measuredByAuditSkill ? "" : "  [invisible to ui-audit]"}\n` +
        `       text="${f.text}"`,
    );
  }

  const worst = [...rows].sort((a, b) => a.ratio - b.ratio).slice(0, 8);
  console.log("\n  tightest 8 (lowest ratio first):");
  for (const w of worst) {
    console.log(`    ${String(w.ratio).padStart(6)}:1 / ${w.threshold}  ${w.selector}  "${w.text.slice(0, 34)}"`);
  }

  if (jsonOut) {
    writeFileSync(jsonOut, JSON.stringify({ url, rows, fails, skipped: skipped.length }, null, 2));
    console.log(`\n  detail -> ${jsonOut}`);
  }

  console.log(
    fails.length === 0
      ? "\nSUCCESS:contrast-probe 0 AA failures across " + rows.length + " text-bearing elements"
      : `\nFAILED:contrast-probe ${fails.length} AA failure(s)`,
  );
  process.exit(fails.length === 0 ? 0 : 1);
};

main().catch((e) => {
  console.log(`FAILED:contrast-probe ${e.message}`);
  process.exit(2);
});
