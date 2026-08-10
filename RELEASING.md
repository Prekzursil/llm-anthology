# Releasing

This product has never been released. There is no version tag on this repository — all 52
existing tags are `rescue/*` — and no GitHub Release. This document is the runbook for
firing the first one and every one after it.

`.github/workflows/release.yml` does all three publishes. **You** do the tag and the
Release; the workflow does the rest.

---

## What a release consists of

One commit produces three artifacts that share one version number:

| artifact | where it goes | auth |
|---|---|---|
| `llm-anthology` wheel + sdist | PyPI | Trusted Publishing (OIDC), no stored token |
| `llm-anthology` tarball | npm | OIDC once configured — **but see the first-publish problem** |
| `LLM Anthology_<v>_x64-setup.exe` | attached to the GitHub Release | `GITHUB_TOKEN` |

Each one is built, then validated in a **tokenless** job (`permissions: {}`), then
published from the *same* uploaded artifact. Never collapse those three jobs into one:
installing an artifact executes its runtime dependencies, and the point of the split is
that the job which runs untrusted code holds no credential it could use to change what
ships. Read the header comment in `release.yml` before editing it.

---

## Part 1 — one-time owner setup

None of this can be done by the workflow, by CI, or by an agent. All of it is
**UNVERIFIED from inside the repository** — there is no way to check any of it without
actually firing a release, so treat each item as *not done* until you have done it.

### 1a. PyPI trusted publisher (required)

`llm-anthology` is **not registered on PyPI** (verified: `GET
https://pypi.org/pypi/llm-anthology/json` → 404, 2026-08-08). PyPI supports *pending*
publishers, so you can configure this before the project exists and the very first
publish works over OIDC with no token.

Go to <https://pypi.org/manage/account/publishing/> → "Add a new pending publisher" and
enter **exactly**:

| field | value |
|---|---|
| PyPI Project Name | `llm-anthology` |
| Owner | `Prekzursil` |
| Repository name | `llm-anthology` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

> **The Repository field is the GitHub repo, `llm-anthology` — not the package
> name.** An earlier comment in `release.yml` said `Repository: llm-anthology`, which is
> the package name and would not have matched. Following it would have failed the first
> publish with an OIDC claim error that looks like a permissions problem.

The `pypi` environment on the GitHub side needs no configuration; the job declares it and
GitHub creates it on first use. Add required reviewers there if you want a human gate
before the upload.

### 1b. npm auth (required, and the first release is a special case)

`llm-anthology` is **not registered on npm** either (verified: `GET
https://registry.npmjs.org/llm-anthology` → 404, 2026-08-08). This matters, because npm
is **not** like PyPI here:

> **npm cannot create a package over OIDC.** A trusted publisher is configured on an
> existing package's settings page, so the package must already exist before OIDC can be
> configured — and OIDC cannot be used to bring it into existence. There is no pending-publisher
> equivalent. ([npm/cli#8544](https://github.com/npm/cli/issues/8544))

So the first release needs a token, and only the first:

1. Create a **granular access token** at <https://www.npmjs.com/settings/~/tokens> with
   *Read and write* on packages. Scope it as narrowly as npm lets you and give it a short
   expiry — it is needed for one publish.
2. Add it to this repository as the secret **`NPM_TOKEN`**
   (Settings → Secrets and variables → Actions → New repository secret).
3. Fire the release (Part 2). The `npm` job detects the secret and uses it; the log says
   `auth: NPM_TOKEN`.
4. **Afterwards**, on <https://www.npmjs.com/package/llm-anthology/access>, configure the
   trusted publisher: repository `Prekzursil/llm-anthology`, workflow file
   `release.yml` (the filename, including `.yml`, case-sensitive and exact). Leave the
   Environment field **blank** — the `npm` job deliberately declares no `environment:`,
   and a mismatch here is rejected.
5. **Delete the `NPM_TOKEN` secret and revoke the token.** With the secret gone the job
   falls through to OIDC automatically — no workflow edit — and the log then says
   `auth: OIDC trusted publishing`. From then on there is no long-lived npm credential
   anywhere in this repository.

If you would rather never put a token in CI at all, the alternative is to publish 0.1.0
once by hand from a clean checkout (`cd js && npm ci && npm run build && npm publish`),
then do step 4 and never set the secret. Same end state.

### 1c. Nothing to configure for the installer

The desktop installer is attached with the automatic `GITHUB_TOKEN`. No secret, no
signing certificate, no account setup.

The installer is **not code-signed**, which is a real user-facing consequence, not an
omission to hide: Windows SmartScreen will warn on download and on first run until the
binary builds reputation. Fixing that needs an OV or EV code-signing certificate (an
annual paid purchase, EV on hardware or an HSM) plus a `signCommand`/`certificateThumbprint`
in `tauri.conf.json`. Out of scope for 0.1.0 — decide it separately.

---

## Part 2 — releasing

### Step 1: bump the version, in all five places

The version is declared five times and nothing in the build couples them.
`tests/test_release_version_sync.py` fails if they disagree, so you cannot forget one
silently — but you do have to edit all five:

| file | what it versions |
|---|---|
| `pyproject.toml` (`[project] version`) | the PyPI wheel and sdist |
| `llm_anthology/__init__.py` (`__version__`) | what the engine *inside* that wheel reports as `engine_version` |
| `js/package.json` (`version`) | the npm package |
| `cockpit/src-tauri/tauri.conf.json` (`version`) | the installer, and the version the app reports |
| `cockpit/src-tauri/Cargo.toml` (`[package] version`) | `CARGO_PKG_VERSION` in the shipped binary |

`__version__` is the easiest of the five to miss, because missing it does not break
anything visibly: the build reads the version from `pyproject.toml`, so you still get a
correctly-named `0.1.1` wheel — one whose engine answers `health.ping` with `0.1.0`.

`cockpit/package.json` is intentionally **not** in that list — it is a private dev-only
package (`"name": "cockpit"`, never published) and its version is not a release version.
Neither is the `engine_version: "mock-0.1.0"` in `cockpit/src/ipc/mock.ts`, which is a
browser-mock fixture that is *supposed* to be identifiable as not-a-real-engine.

If you add a sixth place, add it to `RELEASE_VERSION_FILES` in the test too; a test in
that file asserts the tuple and this table have not drifted apart.

### Step 2: update `CHANGELOG.md`

Move the `[Unreleased]` items into a new `## [x.y.z] — <date>` section and update the
two link definitions at the bottom. Write it for a user — what they can now do and what
was broken — not as a commit dump.

### Step 3: verify locally before tagging

```bash
python -m pytest                      # must end "Required test coverage of 100% reached"
cd js && npm ci && npm run typecheck && npm test && npm run build && cd ..
cd cockpit && npm ci && npx tsc --noEmit && npm test && cd ..
cd cockpit/src-tauri && cargo test --locked && cargo clippy --locked --all-targets -- -D warnings
```

Do **not** pass `-q` to pytest — `pyproject.toml`'s `addopts` already contains it and a
second one suppresses the pass/fail summary line.

### Step 3b: the provider-drift check — the half no CI job can do

Everything in Step 3, and the weekly `format-drift canary`
(`.github/workflows/format-drift-canary.yml`), runs against **synthetic, frozen** inputs. So
none of it can see the one drift that actually breaks this product: a **provider changing its
export format**. The motivating case — an early-2026 ChatGPT export change that broke
third-party parsers with no version marker in the file — is **UNVERIFIED here**: it is the
premise this drill was written on, taken as briefed, and nothing in this repository measures
it (a check that would is a network check, which this project does not make). Treat it as the
shape of the risk rather than a finding. The structural point stands on its own: a runner has
no exports, so this check only exists if a human with real exports runs it.

Do it **before** a release, on a machine that has the corpus:

```bash
# 1. Re-download the exports. This is the only step that can surface provider drift at all --
#    an old export cannot tell you the format moved.

# 2. Render each export provider and read the report, not the exit code.
llm-anthology claude   <fresh-claude-export>   /tmp/drift/claude/
llm-anthology chatgpt  <fresh-chatgpt-export>  /tmp/drift/chatgpt/
llm-anthology gemini   <fresh-gemini-export>   /tmp/drift/gemini/

# 3. Read the report. It is a DICT, so print the fields that carry the verdict:
python -c "import json;r=json.load(open('/tmp/drift/claude/_fidelity-report.json'));print({k:r[k] for k in ('conversations','rendered','turns','empty_conversations','fidelity_passed')}, 'failed:', len(r['failed']), 'errors:', len(r['errors']))"
```

What to look at, in order of how loudly it means "the format moved":

| signal | reading |
|---|---|
| conversations parsed drops to 0, or an adapter raises | a **structural** change — the shape the adapter walks is gone |
| `empty_conversations` > 0 | the sharpest signal there is: it parsed, reported no errors, and rendered **nothing**. `build.py` counts turns for exactly this reason — feeding a Codex-shaped export to the ChatGPT loader measured 1 conversation / 0 turns / 0 errors, a clean-looking load with the content gone |
| the count parsed is far below the export's own size | a shard or a message shape is being skipped silently |
| `_fidelity-report.json` failures jump | content is arriving in a container the renderer does not know |
| new `unknown` blocks appear | a new content type — additive, so usually harmless, but it is the early warning |

Compare the pass counts against the provider table in `README.md` and **update that table** if
they moved. A number nobody re-measured is the same liability as a stale claim.

Then the real-data cross-rail check:

```bash
python tools/gen-adapter-parity.py             # samples the REAL corpora into a local fixture
cd js && npx vitest run test/adapter-parity.test.ts
```

Two things to know before you run that, both measured:

- The fixture it writes (`js/test/fixtures/adapter-parity.json`) **embeds real conversation
  content** — on the current corpus, 27,249,819 bytes. It is gitignored and never packaged.
  Do not commit it, do not attach it to a release, and delete it when you are done. The
  canary asserts weekly that it is still untracked, and refuses to run the tool at all on a
  machine that has a corpus.
- It only covers **Claude**. On the current corpus the tool prints `claude 44 | chatgpt 1 |
  gemini 1`; two of those claude cases are its own synthetic ones, so 42 are real and the
  chatgpt/gemini cases are synthetic only — it samples the Claude corpus directory and
  nothing else. (The same 42 / zero / zero count is recorded independently in
  `js/test/adapter-parity.test.ts`.) So a green run here is evidence about one rail, not
  three, and the ChatGPT rail in particular stays `UNVALIDATED at scale` until someone runs a
  large real export through it deliberately.

### Step 4: commit, tag, push

```bash
git commit -am "release: 0.1.0"
git tag -a v0.1.0 -m "0.1.0"
git push origin main
git push origin v0.1.0
```

The tag must be `v<version>`. `installer-attach` strips a leading `v` and fails the
release if the tag and the built installer disagree on the version.

### Step 5: publish the GitHub Release

```bash
gh release create v0.1.0 --title "0.1.0" --notes-file CHANGELOG.md --verify-tag
```

Publishing the Release is what triggers `release.yml` — creating a *draft* does not. If
you want to review the notes first, `--draft` and then publish from the web UI; the
workflow fires on publish.

### Step 6: watch it

```bash
gh run watch "$(gh run list --workflow=release.yml --limit=1 --json databaseId -q '.[0].databaseId')"
```

Nine jobs, three independent chains. A failure in one chain does **not** stop the others
— so a red run can still mean PyPI succeeded and npm did not.

---

## Part 3 — verify afterwards

Do not trust green jobs alone; check the registries.

```bash
# PyPI: the version exists, and a clean install of it works
curl -s https://pypi.org/pypi/llm-anthology/json | python -c "import json,sys;print(sorted(json.load(sys.stdin)['releases']))"
pipx run --spec llm-anthology==0.1.0 llm-anthology demo /tmp/demo.html && head -c 15 /tmp/demo.html

# npm: the version exists, and provenance was attached
npm view llm-anthology version
npm view llm-anthology dist.attestations

# the installer is on the release, with its digest
gh release view v0.1.0 --json assets -q '.assets[].name'
```

Then, on a machine that does **not** have Python or this package installed, download the
installer, install it, and open a corpus. That is the only check that proves the bundled
engine actually works for a user, and no CI job can do it for you.

---

## Things that will bite you

**`workflow_dispatch` is not a dry run.** Dispatching `release.yml` manually runs the
PyPI and npm publishes for real. Only the installer upload is skipped (there is no
Release to attach it to). It exists as a retry path after a partial failure, not as a
rehearsal. PyPI and npm both reject a re-upload of an existing version, so a retry after
a *successful* publish fails on that job — which is the safe direction, but read the log
rather than assuming the failure is new.

**Version numbers are immutable on both registries.** A bad 0.1.0 cannot be replaced,
only yanked/deprecated and superseded by 0.1.1. There is no undo. This is the reason the
smoke jobs exist.

**A cold Windows release build is slow.** `installer-build` deliberately uses no Rust
cache — CI caches to keep iterations cheap, but a release should not be able to link a
stale cached artifact into a shipped binary. Expect 15-25 minutes.

**The installer's engine is staged, not committed.** `cockpit/src-tauri/resources/` is
gitignored, and `installer-build` runs `cockpit/scripts/stage-engine.ps1` to populate it
before `tauri build`. CI's `cockpit` job only creates an *empty* directory (enough to
compile, and it refuses to upload what it built) — the release job is the only place the
real engine is staged. If that step is ever removed or skipped, the installer still
builds and still installs, but the app falls back to `python` on the user's PATH, which
is exactly the requirement the bundled engine exists to remove. `installer-smoke` guards
this two ways: a size floor (an engine-bearing installer is ~14 MB; one without is a few
MB) and a silent install followed by running `engine\python.exe -m llm_anthology.sidecar`
from the installed tree.

**Windows only, for now, and on purpose.** `tauri.conf.json` sets `bundle.targets` to
`["nsis"]`, and `stage-engine.ps1` stages a *Windows* CPython via a PowerShell-only
script. macOS and Linux are therefore not a matrix line — they need a POSIX port of the
staging script, `dmg`/`deb`/`appimage` added to `bundle.targets`, and for macOS an Apple
Developer ID plus notarization credentials, since an unsigned `.dmg` is Gatekeeper-blocked
on arrival. Shipping a bundle for a platform whose engine cannot be staged would produce
an installer with no engine in it. The engine and CLI are already cross-platform via PyPI
and npm, so non-Windows users are not shut out — they just do not get the desktop app yet.
