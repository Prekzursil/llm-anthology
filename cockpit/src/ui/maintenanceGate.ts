/**
 * The switch that admits the maintenance/execute plane (DECISION G-11), and the only thing that
 * makes its button reachable.
 *
 * OFF BY DEFAULT. The plane archives, moves and quarantines files under the owner's live Codex
 * session store: the highest data-loss surface in the app, and the lowest core value to someone
 * whose first session is import -> search -> read -> export. Nothing on that floor touches it —
 * with this flag off, `#btn-maintenance` is `hidden`, so the pane cannot be revealed and
 * `app.ts` never even constructs `MaintenanceShell`.
 *
 * `hidden`, NOT `disabled`, and that is the whole mechanism rather than a detail. A disabled
 * button is still painted, still announced, and still advertises a plane the user cannot use;
 * `hidden` takes it out of the accessibility tree AND out of the Tab walk, which is the property
 * being claimed. (This repo already ships `tools/probe_keyboard_reach.mjs` because Tab traversal
 * was broken once, so "out of the Tab order" is a checkable claim here, not a hope.)
 *
 * WHY A VISIBLE CONTROL AT ALL, rather than a storage key only the author knows. A flag with no
 * control is not a setting, it is a secret: no user can enable it, none can tell the capability
 * exists, and the plane's entire safety model — typed confirmation, protected targets, dry-run
 * plans, restorable checkpoints — becomes code no user path ever exercises. That is the same
 * "shipped but unreachable" failure this codebase has now hit eight times. So the switch is on
 * screen, deliberately unexciting, immediately beside the button it governs, and it says on the
 * control itself why it is off.
 *
 * IT REPORTS THE STORED TRUTH, NEVER THE CHECKBOX. A hardened webview can refuse to persist; in
 * that case the box springs back to off, because a switch that claims to hold and does not is
 * worse than one that admits it cannot.
 *
 * The look is applied through CSSOM (`el.style.setProperty`) with longhand declarations only —
 * see `ui/graphStrip.ts` for both reasons: the shipped CSP is `style-src 'self'` with no
 * `'unsafe-inline'`, and a shorthand carrying a `var()` is destroyed by happy-dom's CSSOM.
 */

import { localFeatureFlag, type FeatureFlag } from "./featureFlags";

/** The switch's visible name. Exported so a test never retypes what the user reads. */
export const MAINTENANCE_GATE_LABEL = "Maintenance tools";

/**
 * Why it is off, shown on the control.
 *
 * Names the consequence rather than the category: "advanced feature" tells a user nothing they
 * can weigh, "moves and deletes files" tells them exactly what they are switching on.
 */
export const MAINTENANCE_GATE_HINT =
  "Off by default. Turns on tools that move, archive and delete files in your session stores. "
  + "Every action still plans first, needs a typed confirmation and writes an undo checkpoint.";

/** The gate's own declarations, longhand, from theme tokens. */
const GATE_STYLE: ReadonlyArray<readonly [string, string]> = [
  ["display", "inline-flex"],
  ["align-items", "center"],
  ["column-gap", "var(--space-1)"],
  ["color", "var(--muted)"],
  ["font-size", "12px"],
  ["cursor", "pointer"],
  ["user-select", "none"],
];

export interface MaintenanceGateOptions {
  /** Where the switch is appended — the top bar's workspace nav in the shipped shell. */
  container: HTMLElement;
  /** The button that reveals the plane. Hidden whenever the flag is off. */
  button: HTMLButtonElement;
  /**
   * Called ONLY when the user changes the switch, with the state that was actually stored.
   *
   * Never on construction: `app.ts` builds this in its own constructor, so a callback fired
   * there would run against half-initialised fields and would be indistinguishable, to any
   * listener, from the user having asked for the plane.
   */
  onChange?: (enabled: boolean) => void;
  /** Defaults to the real `localStorage`-backed flag. */
  flag?: FeatureFlag;
}

export class MaintenanceGate {
  private readonly flag: FeatureFlag;
  private readonly button: HTMLButtonElement;
  private readonly checkbox: HTMLInputElement;
  private readonly onChange: (enabled: boolean) => void;
  private readonly onToggle: () => void;

  constructor(options: MaintenanceGateOptions) {
    this.flag = options.flag ?? localFeatureFlag();
    this.button = options.button;
    this.onChange = options.onChange ?? ((): void => {});

    const label = document.createElement("label");
    label.className = "feature-toggle";
    // On the label, so it covers the box and its text — a tooltip only the 13px checkbox
    // reveals is a tooltip nobody finds.
    label.setAttribute("title", MAINTENANCE_GATE_HINT);
    for (const [prop, value] of GATE_STYLE) label.style.setProperty(prop, value);

    this.checkbox = document.createElement("input");
    this.checkbox.type = "checkbox";
    this.checkbox.className = "feature-toggle-input";
    // A real <label> ancestor rather than `aria-label`: it makes the TEXT a click target too,
    // which is the affordance that stops this reading as a stray checkbox.
    label.append(this.checkbox, document.createTextNode(MAINTENANCE_GATE_LABEL));

    this.onToggle = () => this.toggled();
    this.checkbox.addEventListener("change", this.onToggle);
    options.container.append(label);

    // Paint from the STORED state, and announce nothing: this is not a change.
    this.sync();
  }

  /** Whether the plane is admitted right now, per the store rather than the checkbox. */
  get enabled(): boolean {
    return this.flag.enabled();
  }

  /**
   * Handle a user flip: try to store it, then repaint from whatever was actually stored and
   * report THAT. The asymmetry is deliberate — the caller closes an open maintenance pane on a
   * `false`, and it must do so on a refused write too, because the plane is genuinely not
   * admitted in that case.
   */
  private toggled(): void {
    this.flag.set(this.checkbox.checked);
    const enabled = this.sync();
    this.onChange(enabled);
  }

  /** Reflect the stored state onto the checkbox and the button. Returns that state. */
  private sync(): boolean {
    const enabled = this.enabled;
    this.checkbox.checked = enabled;
    this.button.hidden = !enabled;
    return enabled;
  }

  /**
   * Stop governing the button.
   *
   * Drops the listener and leaves the DOM alone, exactly as `CorpusBar.destroy` does: a
   * teardown that also ripped out its own markup would make the order of teardown observable to
   * anything still holding a reference to it.
   */
  destroy(): void {
    this.checkbox.removeEventListener("change", this.onToggle);
  }
}
