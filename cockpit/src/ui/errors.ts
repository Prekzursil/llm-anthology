/**
 * Turning engine errors into something a person can act on.
 *
 * The engine's "no corpus attached" error is not a failure the user caused or can debug — it
 * is simply the app's initial state. Interpolating it raw put the literal string
 * `no corpus attached: call open_corpus first` into three separate places in the UI (the
 * engine status, the stats line, and the time scrubber), telling the user to call an internal
 * JSON-RPC method. Measured in the installed build: that text was the first thing visible on
 * first launch.
 *
 * So this module does two things: recognise that ONE condition and replace it with a real
 * instruction, and otherwise leave genuine errors intact — a corrupt index or a crashed engine
 * must still surface its actual message, because suppressing those would trade a confusing
 * string for an invisible failure, which is worse.
 */

/** What the user should do when nothing is attached yet. Names the actual affordance. */
export const NO_CORPUS_MESSAGE = "No corpus open — use “Open corpus…” in the top bar.";

/**
 * The engine's not-attached error, from `cockpit/src-tauri/src/lib.rs` (`forward`, the `None`
 * arm). Matched as a SUBSTRING because the string arrives wrapped in varying prefixes
 * depending on which command forwarded it, and case-insensitively so a future rewording of
 * the Rust side does not silently reopen the leak.
 */
const NO_CORPUS_NEEDLE = "no corpus attached";

/** True when `err` is the engine reporting that no corpus is attached yet. */
export function isNoCorpusError(err: unknown): boolean {
  return String(err).toLowerCase().includes(NO_CORPUS_NEEDLE);
}

/**
 * The text to show for `err` under `label` (e.g. "stats unavailable").
 *
 * For the not-attached state the label is DROPPED entirely: "stats unavailable: no corpus
 * open" reads as two problems when there are none. Everything else keeps its label and its
 * real message, so a genuine fault is still legible and still attributable to the operation
 * that produced it.
 */
export function engineErrorText(err: unknown, label: string): string {
  if (isNoCorpusError(err)) {
    return NO_CORPUS_MESSAGE;
  }
  return `${label}: ${String(err)}`;
}
