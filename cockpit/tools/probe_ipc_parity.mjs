/**
 * MOCK-vs-ENGINE structural parity probe for the 11 metadata / dedup / maintenance RPCs.
 *
 *   node cockpit/tools/probe_ipc_parity.mjs [--db <path>] [--json] [--verbose]
 *   node cockpit/tools/probe_ipc_parity.mjs --self-test
 *   node cockpit/tools/probe_ipc_parity.mjs --mutate metadata.get:dropKey
 *
 * WHY THIS EXISTS. `ipc/index.ts` selects the MOCK for every non-Tauri environment, so every
 * dev run, vitest run, screenshot and design review is served fabricated data. `mock.test.ts`
 * asserts the mock against itself and therefore cannot see a shape that diverges from the
 * engine. A divergence means a panel works perfectly in development and breaks in the shipped
 * app — the failure this project has already shipped once, where two individually-correct
 * surfaces were never exercised across the seam between them.
 *
 * WHAT IS COMPARED. STRUCTURE ONLY: key sets and value types, recursively. Values cannot
 * match — the mock serves an invented 15-thread forest and the engine serves the synthetic
 * fixture — and making them match is not the goal. What must match is the shape a panel
 * destructures.
 *
 * SYNTHETIC ONLY, AND NON-DESTRUCTIVE TO THE FIXTURE. The engine is pointed at a COPY of
 * `.scratch/fixture/synthetic.db` in a temp dir, never at a real corpus and never at the
 * shared fixture itself (the probe writes annotations and a maintenance run, which would
 * otherwise mutate a file other agents read). The Codex store and maintenance store it scans
 * are freshly-written temp files. Nothing here reads the owner's conversations.
 *
 * THE EMPTY-COLLECTION TRAP — the single easiest way to write a probe that certifies nothing.
 * If the engine returns `[]` and the mock returns `[]`, an element-shape comparison compares
 * NOTHING and reports success. Measured on the bare fixture: `conversation_metadata`,
 * `session_physical_copies` and `maintenance_runs` all hold ZERO rows, so `metadata.search`,
 * `metadata.tags`, `dedup.sessions` and `maintenance.runs` would ALL have been vacuous. Two
 * defences:
 *
 *   1. The probe SEEDS each collection through the engine's own write path first — it sets an
 *      annotation, scans a synthetic two-copy Codex store, and plans + applies a real
 *      maintenance run against throwaway files — so every array has elements to compare.
 *   2. Any array that is STILL empty on either side is reported as `UNCOMPARED`, which is NOT
 *      a pass and is counted separately in the summary and the exit code. A field that is
 *      `null` on both sides is likewise UNCOMPARED, for the same reason.
 *
 * PROVING THE COMPARATOR CAN FAIL. `--self-test` runs the comparator against hand-built
 * divergences (dropped key, changed type, extra key, nested drop, one-side-empty array,
 * both-empty array) and requires each to be caught, PLUS an identical-input control that must
 * PASS — a comparator that flags everything is as useless as one that flags nothing.
 * `--mutate <rpc>:<mode>` corrupts a live mock response in flight for an end-to-end proof.
 * Mutation lives here rather than in `mock.ts` because that file is out of this tool's scope,
 * and because a flag is reproducible where a hand-edit is not.
 */

import { spawn } from "node:child_process";
import { createServer } from "vite";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const COCKPIT = path.resolve(HERE, "..");
const REPO_ROOT = path.resolve(COCKPIT, "..");
const DEFAULT_DB = path.join(REPO_ROOT, ".scratch", "fixture", "synthetic.db");

// A fixed synthetic session id, shaped like a real Codex one so `dedup` can recover it from
// the rollout body (`tests/test_sidecar_dedup.py:29`).
const SYNTH_SESSION = "aaaaaaaa-1111-2222-3333-444444444444";
// The tag the probe writes, then searches for. Deliberately not one of the mock's seeds, so a
// hit proves the probe's own write round-tripped rather than matching pre-existing data.
const PROBE_TAG = "parityprobe";

// ---------------------------------------------------------------------------
// shape model
// ---------------------------------------------------------------------------
//
// A shape is FOLDED over every sample, not read off the first one: an array's element shape is
// the union across all its elements, so a heterogeneous list cannot hide behind element [0].
// `seen` counts make "present in 3 of 5 elements" visible as an optionality note instead of
// silently widening the key set.

function newNode() {
  return { types: new Set(), objSeen: 0, fields: new Map(), arrSamples: 0, arrSeen: 0, arr: null };
}

/** Fold one value into `node`. */
function merge(node, value) {
  if (value === null) {
    node.types.add("null");
    return node;
  }
  if (Array.isArray(value)) {
    node.types.add("array");
    node.arrSeen += 1;
    node.arrSamples += value.length;
    if (value.length > 0) {
      node.arr = node.arr ?? newNode();
      for (const el of value) merge(node.arr, el);
    }
    return node;
  }
  if (typeof value === "object") {
    node.types.add("object");
    node.objSeen += 1;
    for (const [k, v] of Object.entries(value)) {
      let slot = node.fields.get(k);
      if (slot === undefined) {
        slot = { node: newNode(), seen: 0 };
        node.fields.set(k, slot);
      }
      slot.seen += 1;
      merge(slot.node, v);
    }
    return node;
  }
  node.types.add(typeof value);
  return node;
}

function shapeOf(value) {
  return merge(newNode(), value);
}

/** The concrete (non-null) type names a node took. */
function concrete(node) {
  return [...node.types].filter((t) => t !== "null").sort();
}

/**
 * Diff two shapes. Emits three severities:
 *
 *   `diff`  — a real divergence: a missing/extra key, or incompatible concrete types.
 *   `uncmp` — nothing could be compared here (an array empty on a side, or a field that was
 *             null on BOTH sides). Explicitly not a pass.
 *   `note`  — compatible but worth seeing: one side null and the other typed (consistent with
 *             `T | null`), or a key present in only some elements of a list.
 */
function diffShape(pathStr, a, b, out) {
  const ca = concrete(a);
  const cb = concrete(b);
  const aNull = a.types.has("null");
  const bNull = b.types.has("null");

  if (ca.length === 0 && cb.length === 0) {
    // Both sides only ever null: `T | null` is satisfied but T itself is unverified.
    out.push({ sev: "uncmp", path: pathStr, msg: "null on BOTH sides — type not compared" });
    return;
  }
  if (ca.length === 0 || cb.length === 0) {
    const typed = ca.length === 0 ? `mock=${cb.join("|")}` : `engine=${ca.join("|")}`;
    out.push({
      sev: "note",
      path: pathStr,
      msg: `null on one side only (${typed}) — consistent with a nullable field`,
    });
    return;
  }
  if (ca.join("|") !== cb.join("|")) {
    out.push({
      sev: "diff",
      path: pathStr,
      msg: `type mismatch: engine=${ca.join("|")} mock=${cb.join("|")}`,
    });
    return;
  }
  if (aNull !== bNull) {
    out.push({
      sev: "note",
      path: pathStr,
      msg: `nullable on ${aNull ? "engine" : "mock"} only`,
    });
  }

  if (ca.includes("object")) {
    const keysA = [...a.fields.keys()].sort();
    const keysB = [...b.fields.keys()].sort();
    for (const k of keysA) {
      if (!b.fields.has(k)) {
        out.push({ sev: "diff", path: `${pathStr}.${k}`, msg: "present on ENGINE, missing on MOCK" });
      }
    }
    for (const k of keysB) {
      if (!a.fields.has(k)) {
        out.push({ sev: "diff", path: `${pathStr}.${k}`, msg: "present on MOCK, missing on ENGINE" });
      }
    }
    for (const k of keysA) {
      const sb = b.fields.get(k);
      if (sb === undefined) continue;
      const sa = a.fields.get(k);
      for (const [side, node, slot] of [
        ["engine", a, sa],
        ["mock", b, sb],
      ]) {
        if (node.objSeen > 1 && slot.seen < node.objSeen) {
          out.push({
            sev: "note",
            path: `${pathStr}.${k}`,
            msg: `present in ${slot.seen}/${node.objSeen} ${side} elements (optional)`,
          });
        }
      }
      diffShape(`${pathStr}.${k}`, sa.node, sb.node, out);
    }
  }

  if (ca.includes("array")) {
    // THE VACUOUS-PASS GUARD. An empty array on either side means the element shape was never
    // observed, so there is nothing to compare and this must not read as agreement.
    if (a.arrSamples === 0 || b.arrSamples === 0) {
      out.push({
        sev: "uncmp",
        path: `${pathStr}[]`,
        msg: `element shape NOT compared (engine n=${a.arrSamples}, mock n=${b.arrSamples})`,
      });
      return;
    }
    diffShape(`${pathStr}[]`, a.arr, b.arr, out);
  }
}

function compare(engineValue, mockValue) {
  const out = [];
  diffShape("$", shapeOf(engineValue), shapeOf(mockValue), out);
  const diffs = out.filter((f) => f.sev === "diff");
  const uncmp = out.filter((f) => f.sev === "uncmp");
  const verdict = diffs.length > 0 ? "DIFF" : uncmp.length > 0 ? "UNCMP" : "PASS";
  return { verdict, findings: out, diffs, uncmp };
}

// ---------------------------------------------------------------------------
// the engine, over real stdio NDJSON JSON-RPC 2.0
// ---------------------------------------------------------------------------

class Engine {
  constructor(dbPath) {
    this.next = 1;
    this.buf = "";
    this.waiting = new Map();
    this.stderr = "";
    this.proc = spawn("python", ["-m", "llm_anthology.sidecar", "--index", dbPath], {
      cwd: REPO_ROOT,
      env: { ...process.env, PYTHONPATH: REPO_ROOT, PYTHONIOENCODING: "utf-8" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc.stdout.setEncoding("utf-8");
    this.proc.stdout.on("data", (chunk) => this.onData(chunk));
    // An UNDRAINED stderr pipe is a deadlock, not an inefficiency: the OS buffer fills, the
    // child blocks inside its stderr write and never reaches the stdout response
    // (`cockpit/src-tauri/src/sidecar.rs:169-175` records the measured threshold).
    this.proc.stderr.setEncoding("utf-8");
    this.proc.stderr.on("data", (c) => {
      this.stderr += c;
    });
    this.exited = new Promise((res) => this.proc.on("exit", (code) => res(code)));
  }

  onData(chunk) {
    this.buf += chunk;
    let nl;
    while ((nl = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, nl).trim();
      this.buf = this.buf.slice(nl + 1);
      if (line === "") continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        continue; // not a response line
      }
      const slot = this.waiting.get(msg.id);
      if (slot !== undefined) {
        this.waiting.delete(msg.id);
        slot(msg);
      }
    }
  }

  /** One request; resolves with the raw envelope (result OR error), never throws on -32xxx. */
  call(method, params = {}, timeoutMs = 30_000) {
    const id = this.next++;
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiting.delete(id);
        reject(new Error(`engine timeout after ${timeoutMs}ms on ${method}`));
      }, timeoutMs);
      this.waiting.set(id, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
      this.proc.stdin.write(`${payload}\n`);
    });
  }

  /** The `result`, or throw with the engine's own code+message. */
  async result(method, params = {}) {
    const msg = await this.call(method, params);
    if (msg.error !== undefined) {
      const e = new Error(`${method} -> rpc error ${JSON.stringify(msg.error)}`);
      e.rpc = msg.error;
      throw e;
    }
    return msg.result;
  }

  async close() {
    this.proc.stdin.end();
    await Promise.race([this.exited, new Promise((r) => setTimeout(r, 3000))]);
    if (this.proc.exitCode === null) this.proc.kill();
  }
}

// ---------------------------------------------------------------------------
// synthetic workspace
// ---------------------------------------------------------------------------

/** A minimal but realistic Codex rollout (`tests/test_sidecar_dedup.py:45-61`). */
function rolloutLines(sessionId, pad = 0) {
  const lines = [
    {
      type: "session_meta",
      timestamp: "2026-03-23T10:00:00Z",
      payload: {
        session_id: sessionId,
        cwd: "C:/work",
        model_provider: "openai",
        git: { branch: "main" },
      },
    },
    {
      type: "response_item",
      timestamp: "2026-03-23T10:00:01Z",
      payload: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "hello" }],
      },
    },
  ];
  for (let i = 0; i < pad; i += 1) {
    lines.push({
      type: "response_item",
      timestamp: "2026-03-23T10:00:02Z",
      payload: {
        type: "message",
        role: "assistant",
        content: [{ type: "output_text", text: `reply ${i} padded out` }],
      },
    });
  }
  return lines.map((l) => JSON.stringify(l)).join("\n") + "\n";
}

function setupWorkspace(dbSource) {
  // `realpathSync.native` (not plain `realpathSync`) is what expands an 8.3 SHORT path —
  // `os.tmpdir()` returns `...\PREKZU~1\...` on this box, and mixing a short root with a long
  // target would make the engine's own store-root containment check compare unlike forms.
  const base = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "ipc-parity-")));
  const db = path.join(base, "parity.db");
  // A COPY: the probe writes annotations and a maintenance run, and the shared fixture is read
  // by other tooling.
  fs.copyFileSync(dbSource, db);

  // A two-copy Codex store: the same session id in `sessions` and `sessions_backup`, so
  // `dedup.scan` collapses them into ONE logical session with a duplicate — which is what
  // makes `dedup.sessions` non-empty and its element shape comparable.
  const codexHome = path.join(base, "codex_home");
  for (const [sub, pad] of [
    ["sessions", 0],
    ["sessions_backup", 6],
  ]) {
    const dir = path.join(codexHome, sub);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, `rollout-a-${SYNTH_SESSION}.jsonl`),
      rolloutLines(SYNTH_SESSION, pad),
      "utf-8",
    );
  }

  // Throwaway maintenance targets. The engine really deletes these under `apply: true`; they
  // exist only inside this temp dir and are quarantined into `checkpoint/deleted`.
  const storeRoot = path.join(base, "store");
  fs.mkdirSync(storeRoot, { recursive: true });
  const targets = ["a.jsonl", "b.jsonl"].map((name) => {
    const p = path.join(storeRoot, name);
    fs.writeFileSync(p, `synthetic-body-${name}\n`, "utf-8");
    return p;
  });

  // A SECOND store for the plan-only protected/duplicate case. Separate from the store above
  // so nothing here can ever be reached by the one plan this probe actually executes.
  const storeRoot2 = path.join(base, "store2");
  fs.mkdirSync(storeRoot2, { recursive: true });
  const plainTarget = path.join(storeRoot2, "c.jsonl");
  fs.writeFileSync(plainTarget, "synthetic-body-c\n", "utf-8");
  // A path that SPELLS a protected marker — `\.codex\sessions\`
  // (`llm_anthology/maintenance.py:277-281`) — while still living inside `storeRoot2`, so it
  // passes `_classify` and is then refused by `_is_protected`. Real file, real mtime: the
  // guard resolves `os.path.realpath` as well as the literal string (`maintenance.py:383`).
  const protectedDir = path.join(storeRoot2, ".codex", "sessions");
  fs.mkdirSync(protectedDir, { recursive: true });
  const protectedTarget = path.join(protectedDir, "live.jsonl");
  fs.writeFileSync(protectedTarget, "synthetic-body-live\n", "utf-8");

  // A Codex store whose rollout has a TORN last line. This is the lever that populates
  // `dedup.scan.errors[]`: `scan_store` passes `codex_rollout.ingest_sessions`' errors through
  // verbatim and its docstring is explicit that a MISSING root is "an empty result, not an
  // error" (`llm_anthology/dedup.py:244-252`) — so a nonexistent path yields nothing to
  // compare, and only a partial parse does.
  const codexHomeTorn = path.join(base, "codex_home_torn");
  const tornDir = path.join(codexHomeTorn, "sessions");
  fs.mkdirSync(tornDir, { recursive: true });
  fs.writeFileSync(
    path.join(tornDir, `rollout-t-${SYNTH_SESSION}.jsonl`),
    `${rolloutLines(SYNTH_SESSION).trimEnd()}\n{"type":"response_item","payload":{"type":\n`,
    "utf-8",
  );

  return {
    base,
    db,
    codexHome,
    codexHomeTorn,
    storeRoot,
    storeRoot2,
    plainTarget,
    protectedTarget,
    targets,
    checkpointRoot: path.join(base, "checkpoint"),
  };
}

// ---------------------------------------------------------------------------
// mutation (for the end-to-end detector proof)
// ---------------------------------------------------------------------------

function applyMutation(value, mode) {
  const clone = structuredClone(value);
  const target = Array.isArray(clone) ? clone[0] : clone;
  if (target === undefined || target === null || typeof target !== "object") {
    throw new Error("mutation needs an object or a non-empty array of objects");
  }
  const keys = Object.keys(target);
  if (mode === "dropKey") {
    delete target[keys[0]];
    return clone;
  }
  if (mode === "changeType") {
    target[keys[0]] = { unexpected: "object where a scalar was" };
    return clone;
  }
  if (mode === "extraKey") {
    target.__probeInvented = 1;
    return clone;
  }
  if (mode === "emptyArray") {
    return Array.isArray(clone) ? [] : clone;
  }
  throw new Error(`unknown mutation mode: ${mode}`);
}

// ---------------------------------------------------------------------------
// the 11 cases
// ---------------------------------------------------------------------------
//
// ORDER IS LOAD-BEARING. Several methods only return anything once an earlier one has written:
// `metadata.search`/`tags` need a `metadata.set`, `dedup.sessions` needs a `dedup.scan`,
// `maintenance.runs` needs an APPLIED `maintenance.execute`, and `maintenance.restore` needs
// the manifest that execute issued. `metadata.clear` runs last so it cannot wipe the seed the
// search and facet cases depend on.
//
// Each side is driven with ITS OWN valid ids. The mock's `metadata.search` is an INNER JOIN
// against its own 15-thread fixture, so annotating an engine conversation id there would
// legitimately return nothing — a fixture mismatch, not a shape divergence.

function buildCases(ws) {
  const plan = {
    store_root: ws.storeRoot,
    checkpoint_root: ws.checkpointRoot,
    action: "delete",
    targets: ws.targets.map((p, i) => ({ session_id: `s${i}`, file_path: p })),
  };
  return [
    {
      rpc: "metadata.set",
      engine: (e, ctx) =>
        e.result("metadata.set", {
          conversation_id: ctx.engineCid,
          alias: "parity probe",
          tags: [PROBE_TAG, "Second"],
          notes: "written by probe_ipc_parity",
        }),
      mock: (m, ctx) =>
        m.metadataSet({
          conversation_id: ctx.mockCid,
          alias: "parity probe",
          tags: [PROBE_TAG, "Second"],
          notes: "written by probe_ipc_parity",
        }),
    },
    {
      rpc: "metadata.get",
      engine: (e, ctx) => e.result("metadata.get", { conversation_id: ctx.engineCid }),
      mock: (m, ctx) => m.metadataGet(ctx.mockCid),
    },
    {
      rpc: "metadata.search",
      engine: (e) => e.result("metadata.search", { tag: PROBE_TAG }),
      mock: (m) => m.metadataSearch({ tag: PROBE_TAG }),
    },
    {
      rpc: "metadata.tags",
      engine: (e) => e.result("metadata.tags", {}),
      mock: (m) => m.metadataTags(),
    },
    {
      rpc: "dedup.scan",
      engine: (e) => e.result("dedup.scan", { codex_home: ws.codexHome }),
      mock: (m) => m.dedupScan(ws.codexHome),
    },
    {
      rpc: "dedup.sessions",
      engine: (e) => e.result("dedup.sessions", {}),
      mock: (m) => m.dedupSessions(),
    },
    {
      rpc: "maintenance.plan",
      engine: async (e, ctx) => {
        const preview = await e.result("maintenance.plan", plan);
        ctx.enginePlan = preview;
        return preview;
      },
      mock: async (m, ctx) => {
        const preview = await m.maintenancePlan(plan);
        ctx.mockPlan = preview;
        return preview;
      },
    },
    {
      // A SECOND plan whose two targets name ONE physical file, so both sides populate
      // `blocked[]`. Without this the whole `MaintenanceBlocked` DTO — 3 keys plus a nested
      // `MaintenanceCopy` — has NO parity evidence, because a healthy plan blocks nothing and
      // the array is empty on both sides. It is also the likeliest real panel bug on this
      // surface: feeding a dedup session's canonical AND duplicate path into one plan does
      // exactly this. Plan is pure, so this costs nothing and disturbs no later case.
      rpc: "maintenance.plan (blocked)",
      engine: (e) =>
        e.result("maintenance.plan", {
          ...plan,
          targets: [
            { session_id: "dup", file_path: ws.targets[0] },
            { session_id: "dup", file_path: ws.targets[0] },
          ],
        }),
      mock: (m) =>
        m.maintenancePlan({
          ...plan,
          targets: [
            { session_id: "dup", file_path: ws.targets[0] },
            { session_id: "dup", file_path: ws.targets[0] },
          ],
        }),
    },
    {
      rpc: "maintenance.execute",
      engine: async (e, ctx) => {
        const out = await e.result("maintenance.execute", {
          plan_id: ctx.enginePlan.plan_id,
          confirmation: ctx.enginePlan.required_typed_confirmation,
          apply: true,
        });
        ctx.engineManifest = out.manifest_path;
        return out;
      },
      mock: async (m, ctx) => {
        const out = await m.maintenanceExecute({
          plan_id: ctx.mockPlan.plan_id,
          confirmation: ctx.mockPlan.required_typed_confirmation,
          apply: true,
        });
        ctx.mockManifest = out.manifest_path;
        return out;
      },
    },
    {
      rpc: "maintenance.runs",
      engine: (e) => e.result("maintenance.runs", {}),
      mock: (m) => m.maintenanceRuns(),
    },
    {
      rpc: "maintenance.restore",
      // `apply: false` — the shape is identical either way and a dry run keeps the probe from
      // depending on a second filesystem mutation succeeding.
      engine: (e, ctx) =>
        e.result("maintenance.restore", { manifest_path: ctx.engineManifest, apply: false }),
      mock: (m, ctx) => m.maintenanceRestore({ manifest_path: ctx.mockManifest, apply: false }),
    },
    {
      // PLAN ONLY — this plan_id is NEVER handed to `maintenance.execute`. Only the single
      // plan above is ever executed, and only against `storeRoot`; this case uses the separate
      // `storeRoot2`, so no path reachable from here can be moved or deleted.
      //
      // Three targets to make `blocked[]` NON-EMPTY ON BOTH SIDES, which is what the plain
      // plan case cannot do (a healthy plan blocks nothing): one plain target, the SAME file
      // again (both sides block it `duplicate-target`), and a path spelling a protected marker
      // (only the ENGINE blocks that — see the semantic observations).
      rpc: "maintenance.plan (protected+dup)",
      engine: (e) =>
        e.result("maintenance.plan", {
          store_root: ws.storeRoot2,
          checkpoint_root: ws.checkpointRoot,
          action: "delete",
          targets: [
            { session_id: "p1", file_path: ws.plainTarget },
            { session_id: "p1", file_path: ws.plainTarget },
            { session_id: "p2", file_path: ws.protectedTarget },
          ],
        }),
      mock: (m) =>
        m.maintenancePlan({
          store_root: ws.storeRoot2,
          checkpoint_root: ws.checkpointRoot,
          action: "delete",
          targets: [
            { session_id: "p1", file_path: ws.plainTarget },
            { session_id: "p1", file_path: ws.plainTarget },
            { session_id: "p2", file_path: ws.protectedTarget },
          ],
        }),
    },
    {
      // Populates `errors[]` on the ENGINE via a torn rollout line. The mock's `dedupScan`
      // hardcodes `errors: []`, so this is expected to stay UNCOMPARED — but with the cause
      // NAMED (engine n>=1, mock n=0) instead of the uninformative n=0/n=0.
      rpc: "dedup.scan (torn rollout)",
      engine: (e) => e.result("dedup.scan", { codex_home: ws.codexHomeTorn }),
      mock: (m) => m.dedupScan(ws.codexHomeTorn),
    },
    {
      // Populates `unaccounted[]` on the ENGINE. The applied run above quarantined the
      // originals into `<checkpoint>/deleted`; deleting those copies leaves BOTH ends of every
      // recorded move missing, which is exactly the unaccounted condition
      // (`llm_anthology/maintenance.py:735`). `skip_unaccounted` is required or the engine
      // refuses the batch outright (`:742-747`), and `apply: false` keeps this a pure read —
      // a dry restore still computes and returns the list (`:750-753`), so NO new execute and
      // no further filesystem mutation is needed.
      rpc: "maintenance.restore (unaccounted)",
      engine: (e, ctx) => {
        const quarantine = path.join(ws.checkpointRoot, "deleted");
        if (fs.existsSync(quarantine)) fs.rmSync(quarantine, { recursive: true, force: true });
        return e.result("maintenance.restore", {
          manifest_path: ctx.engineManifest,
          apply: false,
          skip_unaccounted: true,
        });
      },
      mock: (m, ctx) =>
        m.maintenanceRestore({
          manifest_path: ctx.mockManifest,
          apply: false,
          skip_unaccounted: true,
        }),
    },
    {
      rpc: "metadata.clear",
      engine: (e, ctx) => e.result("metadata.clear", { conversation_id: ctx.engineCid }),
      mock: (m, ctx) => m.metadataClear(ctx.mockCid),
    },
  ];
}

// ---------------------------------------------------------------------------
// semantic observations
// ---------------------------------------------------------------------------
//
// The structural comparator deliberately ignores VALUES, so a behavioural divergence behind a
// matching shape is invisible to it. These print the specific values that decide whether a
// remaining UNCOMPARED shape is forceable at all — including the FALSIFICATION of the obvious
// lever for `last_write_ms`.

async function observations(engine, mockIpc, ws, rows) {
  const out = [];
  const blocked = rows.find((r) => r.rpc === "maintenance.plan (protected+dup)");
  if (blocked !== undefined && blocked.engineSample !== undefined) {
    const reasons = (v) => (v.blocked ?? []).map((b) => b.reason).sort().join(",") || "(none)";
    out.push(
      `blocked reasons      engine=[${reasons(blocked.engineSample)}] ` +
        `mock=[${reasons(blocked.mockSample)}]`,
    );
    out.push(
      `allowed counts       engine=${blocked.engineSample.allowed.length} ` +
        `mock=${blocked.mockSample.allowed.length}  ` +
        `(engine phrase ${JSON.stringify(blocked.engineSample.required_typed_confirmation)}, ` +
        `mock ${JSON.stringify(blocked.mockSample.required_typed_confirmation)})`,
    );
  }

  // FALSIFICATION of "give the target a real mtime". The engine builds its `SessionCopy` from
  // ONLY session_id / file_path / size_bytes and hardcodes `store_kind=UNKNOWN`
  // (`llm_anthology/sidecar.py:1553-1557`); it never stats the file and never reads a
  // client-supplied `last_write_ms` or `is_hot`. If that reading is wrong, the values below
  // come back echoed and this line disproves it.
  const echo = await engine.result("maintenance.plan", {
    store_root: ws.storeRoot2,
    checkpoint_root: ws.checkpointRoot,
    action: "delete",
    targets: [
      {
        session_id: "echo",
        file_path: ws.plainTarget,
        size_bytes: 4242,
        last_write_ms: 1_700_000_000_123,
        is_hot: true,
      },
    ],
  });
  const t = echo.allowed[0];
  out.push(
    `client-sent fields   sent last_write_ms=1700000000123 is_hot=true size_bytes=4242 -> ` +
      `engine returned last_write_ms=${JSON.stringify(t.last_write_ms)} ` +
      `is_hot=${JSON.stringify(t.is_hot)} size_bytes=${JSON.stringify(t.size_bytes)}`,
  );
  const mockEcho = await mockIpc.maintenancePlan({
    store_root: ws.storeRoot2,
    checkpoint_root: ws.checkpointRoot,
    action: "delete",
    targets: [{ session_id: "echo", file_path: ws.plainTarget, size_bytes: 4242 }],
  });
  out.push(
    `mock same surface    last_write_ms=${JSON.stringify(mockEcho.allowed[0].last_write_ms)} ` +
      `is_hot=${JSON.stringify(mockEcho.allowed[0].is_hot)}`,
  );

  // SAFETY ASSERTION, not decoration. Every plan built against `storeRoot2` — including the
  // one naming a protected path — must be PLAN-ONLY: nothing there may ever be handed to
  // `maintenance.execute`. Only the single plan over `storeRoot` is executed. If a future edit
  // wires storeRoot2 into an execute, these files vanish and this line says so out loud
  // instead of the probe quietly deleting things it was told not to touch.
  const survived = [ws.plainTarget, ws.protectedTarget].filter((p) => fs.existsSync(p));
  out.push(
    `plan-only guarantee  ${survived.length}/2 storeRoot2 files intact ` +
      `${survived.length === 2 ? "(OK — no plan over storeRoot2 was executed)" : "*** VIOLATED ***"}`,
  );
  return out;
}

// ---------------------------------------------------------------------------
// self-test: prove the comparator can FAIL, and can PASS
// ---------------------------------------------------------------------------

function selfTest() {
  const row = { id: "x", n: 1, flag: true, tags: ["a"], nested: { k: "v" } };
  const cases = [
    ["identical input PASSES (control)", { a: [row], b: [row] }, "PASS", null],
    [
      "a dropped key is caught",
      { a: [row], b: [applyMutation([row], "dropKey")[0]] },
      "DIFF",
      "$[].id",
    ],
    [
      "a changed type is caught",
      { a: [row], b: [applyMutation([row], "changeType")[0]] },
      "DIFF",
      "$[].id",
    ],
    [
      "an invented extra key is caught",
      { a: [row], b: [applyMutation([row], "extraKey")[0]] },
      "DIFF",
      "$[].__probeInvented",
    ],
    [
      "a key dropped INSIDE a nested object is caught",
      { a: [row], b: [{ ...row, nested: {} }] },
      "DIFF",
      "$[].nested.k",
    ],
    [
      "an array empty on ONE side is UNCMP, never PASS",
      { a: [row], b: [] },
      "UNCMP",
      "$[]",
    ],
    [
      "an array empty on BOTH sides is UNCMP, never PASS",
      { a: [], b: [] },
      "UNCMP",
      "$[]",
    ],
    [
      "a field null on BOTH sides is UNCMP, never PASS",
      { a: [{ ...row, n: null }], b: [{ ...row, n: null }] },
      "UNCMP",
      "$[].n",
    ],
    [
      "a nullable field (null one side) is a NOTE, not a DIFF",
      { a: [{ ...row, n: null }], b: [row] },
      "PASS",
      null,
    ],
  ];

  let failed = 0;
  for (const [name, { a, b }, want, wantPath] of cases) {
    const got = compare(a, b);
    const pathOk =
      wantPath === null ||
      got.findings.some((f) => f.path === wantPath && f.sev !== "note");
    const ok = got.verdict === want && pathOk;
    if (!ok) failed += 1;
    const detail =
      wantPath === null ? "" : ` [expect a finding at ${wantPath}]`;
    console.log(
      `${ok ? "ok  " : "FAIL"} ${name} -> ${got.verdict} (want ${want})${detail}`,
    );
    if (!ok) {
      for (const f of got.findings) console.log(`       ${f.sev} ${f.path}: ${f.msg}`);
    }
  }
  console.log(
    `\nself-test: ${cases.length - failed}/${cases.length} passed — the comparator ` +
      `${failed === 0 ? "detects every seeded divergence AND passes an identical pair" : "IS BROKEN"}`,
  );
  return failed === 0 ? 0 : 1;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const out = { db: DEFAULT_DB, json: false, verbose: false, selfTest: false, mutate: null };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--db") out.db = path.resolve(argv[++i]);
    else if (a === "--json") out.json = true;
    else if (a === "--verbose") out.verbose = true;
    else if (a === "--self-test") out.selfTest = true;
    else if (a === "--mutate") out.mutate = argv[++i];
    else throw new Error(`unknown argument: ${a}`);
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selfTest) return selfTest();

  if (!fs.existsSync(args.db)) {
    console.error(`fixture not found: ${args.db}`);
    console.error("build it with: python .scratch/fixture/make_fixture.py");
    return 2;
  }
  const [mutateRpc, mutateMode] = args.mutate ? args.mutate.split(":") : [null, null];

  const ws = setupWorkspace(args.db);
  const server = await createServer({
    root: COCKPIT,
    server: { middlewareMode: true },
    appType: "custom",
    logLevel: "error",
  });
  const engine = new Engine(ws.db);
  const rows = [];
  let notes = [];
  let exitCode = 0;

  try {
    const { mockIpc } = await server.ssrLoadModule("/src/ipc/mock.ts");
    // The mock is not gated on a corpus, but the engine refuses every read until one is
    // attached; `--index` already did that, so this only proves the process is alive before a
    // failure downstream could be blamed on the wrong thing.
    const health = await engine.result("health.ping", {});
    if (health.corpus_ready !== true) throw new Error("engine attached no corpus");

    // A real conversation id read out of the LIVE engine, not hardcoded, so the probe cannot
    // pass against a stale assumption about the fixture. `search.query` is the only exposed
    // way to enumerate conversations and it REFUSES a blank `q` (-32602), so a few candidate
    // terms are tried; running out of them is a loud failure rather than a skipped case,
    // because every metadata case downstream needs a real id to be meaningful.
    let engineCid = null;
    for (const q of ["synthetic", "session", "root", "the"]) {
      const hits = await engine.result("search.query", { q, limit: 1 });
      if (hits.hits.length > 0) {
        engineCid = hits.hits[0].conversation_id;
        break;
      }
    }
    if (engineCid === null) {
      throw new Error(
        "no conversation found in the fixture via search.query — cannot derive a real " +
          "conversation_id, so the metadata cases would be vacuous. Rebuild the fixture: " +
          "python .scratch/fixture/make_fixture.py",
      );
    }
    const ctx = {
      engineCid,
      mockCid: "orch", // a real id in the mock's own 15-thread fixture
    };

    for (const c of buildCases(ws)) {
      let engineValue;
      let mockValue;
      try {
        engineValue = await c.engine(engine, ctx);
      } catch (err) {
        rows.push({ rpc: c.rpc, verdict: "ENGERR", findings: [], msg: String(err.message) });
        exitCode = Math.max(exitCode, 1);
        continue;
      }
      try {
        mockValue = await c.mock(mockIpc, ctx);
        if (mutateRpc === c.rpc) mockValue = applyMutation(mockValue, mutateMode);
      } catch (err) {
        rows.push({ rpc: c.rpc, verdict: "MOCKERR", findings: [], msg: String(err.message) });
        exitCode = Math.max(exitCode, 1);
        continue;
      }
      const res = compare(engineValue, mockValue);
      rows.push({
        rpc: c.rpc,
        verdict: res.verdict,
        findings: res.findings,
        engineSample: engineValue,
        mockSample: mockValue,
      });
    }
    try {
      notes = await observations(engine, mockIpc, ws, rows);
    } catch (err) {
      notes = [`observations FAILED: ${String(err.message)}`];
    }
  } finally {
    await engine.close();
    await server.close();
    fs.rmSync(ws.base, { recursive: true, force: true });
  }

  const counts = { PASS: 0, DIFF: 0, UNCMP: 0, ENGERR: 0, MOCKERR: 0 };
  for (const r of rows) counts[r.verdict] = (counts[r.verdict] ?? 0) + 1;
  if (counts.DIFF > 0 || counts.ENGERR > 0 || counts.MOCKERR > 0) exitCode = 1;
  else if (counts.UNCMP > 0) exitCode = 3;

  if (args.json) {
    console.log(JSON.stringify({ counts, rows, notes }, null, 2));
  } else {
    console.log(`MOCK-vs-ENGINE structural parity — ${rows.length} methods\n`);
    for (const r of rows) {
      console.log(`${r.verdict.padEnd(7)} ${r.rpc}${r.msg ? `  ${r.msg}` : ""}`);
      for (const f of r.findings) {
        if (f.sev === "note" && !args.verbose) continue;
        console.log(`        ${f.sev.padEnd(5)} ${f.path}: ${f.msg}`);
      }
    }
    console.log(
      `\nPASS ${counts.PASS}  DIFF ${counts.DIFF}  UNCMP ${counts.UNCMP}` +
        `  ENGERR ${counts.ENGERR}  MOCKERR ${counts.MOCKERR}`,
    );
    console.log(
      "UNCMP is NOT a pass: nothing could be compared there (empty array, or null on both " +
        "sides). Re-run with --verbose for compatible notes.",
    );
    if (notes.length > 0) {
      console.log("");
      console.log("SEMANTIC OBSERVATIONS (values, which the structural diff ignores)");
      for (const n of notes) console.log(`  ${n}`);
    }
  }
  return exitCode;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(err);
    process.exit(1);
  },
);
