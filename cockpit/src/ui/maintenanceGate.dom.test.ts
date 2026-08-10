// @vitest-environment happy-dom
/**
 * The control that turns the maintenance plane on (DECISION G-11) — {@link MaintenanceGate}.
 *
 * A DOM file because the gate IS a DOM fact: the reason a new user cannot reach the plane is
 * that its button is not in the Tab order, and `hidden` is the only thing that makes that true.
 * The parsing and persistence half is `featureFlags.test.ts` under node.
 *
 * WHY A VISIBLE CHECKBOX AND NOT A HIDDEN STORAGE KEY. A flag with no control is not a setting,
 * it is a secret: nobody can turn it on, nobody can tell it exists, and the plane's whole safety
 * model becomes dead code that no user path exercises. So the switch is on screen, deliberately
 * unexciting, next to the button it governs — off by default, one click to enable, one to
 * revoke, and it survives a relaunch.
 */
import { beforeEach, describe, expect, it } from "vitest";

import { MAINTENANCE_FLAG_KEY, makeFeatureFlag, type FeatureFlag } from "./featureFlags";
import { MAINTENANCE_GATE_LABEL, MaintenanceGate } from "./maintenanceGate";

interface Shell {
  gate: MaintenanceGate;
  nav: HTMLElement;
  button: HTMLButtonElement;
  checkbox(): HTMLInputElement;
  /** Every value `onChange` was called with, in order. */
  changes: boolean[];
}

/**
 * Build the top bar's workspace nav as `index.html` declares it (`index.html:39-43`) and mount
 * a gate over the maintenance button. Omitting `flag` exercises the DEFAULT
 * (`localFeatureFlag()`), which under this environment is a real store over happy-dom's
 * `localStorage` — the same thing the app gets.
 */
function mount(flag?: FeatureFlag): Shell {
  const nav = document.createElement("div");
  nav.id = "workspace-nav";
  const dedup = document.createElement("button");
  dedup.id = "btn-dedup";
  const button = document.createElement("button");
  button.id = "btn-maintenance";
  button.textContent = "Maintenance";
  nav.append(dedup, button);
  document.body.append(nav);

  const changes: boolean[] = [];
  const gate = new MaintenanceGate({
    container: nav,
    button,
    onChange: (enabled) => void changes.push(enabled),
    ...(flag === undefined ? {} : { flag }),
  });
  return {
    gate,
    nav,
    button,
    checkbox: () => nav.querySelector("input[type=checkbox]") as HTMLInputElement,
    changes,
  };
}

/** Flip the checkbox the way a user does: set `checked`, then dispatch `change`. */
function toggle(shell: Shell, on: boolean): void {
  shell.checkbox().checked = on;
  shell.checkbox().dispatchEvent(new Event("change"));
}

beforeEach(() => {
  document.body.replaceChildren();
  localStorage.clear();
});

describe("MaintenanceGate on a machine that has never opted in", () => {
  it("hides the maintenance button, taking it out of the Tab order entirely", () => {
    // `hidden` rather than `disabled`: a disabled button is still announced and still visible
    // chrome advertising a plane the user cannot use. Hidden removes it from the accessibility
    // tree AND from the Tab walk, which is the property that matters here.
    const shell = mount();
    expect(shell.button.hidden).toBe(true);
    expect(shell.gate.enabled).toBe(false);
  });

  it("shows an unchecked switch that says what it is", () => {
    const shell = mount();
    expect(shell.checkbox().checked).toBe(false);
    expect(shell.nav.textContent).toContain(MAINTENANCE_GATE_LABEL);
  });

  it("calls back NOTHING on construction", () => {
    // Construction must be inert. `app.ts` builds this in its constructor, and a callback that
    // fired there would run against half-initialised fields — and would look, to any listener,
    // exactly like a user having asked for the plane.
    expect(mount().changes).toEqual([]);
  });

  it("writes nothing to storage until asked", () => {
    mount();
    expect(localStorage.getItem(MAINTENANCE_FLAG_KEY)).toBeNull();
  });

  it("labels the switch for a screen reader through a real <label> association", () => {
    const shell = mount();
    const label = shell.checkbox().closest("label");
    expect(label).not.toBeNull();
    expect(label?.textContent).toContain(MAINTENANCE_GATE_LABEL);
  });

  it("says WHY it is off, on the control rather than in a doc nobody opens", () => {
    const shell = mount();
    const title = shell.checkbox().closest("label")?.getAttribute("title") ?? "";
    expect(title).not.toBe("");
    // The reason has to be the actual reason: this plane moves and deletes real files.
    expect(title.toLowerCase()).toMatch(/delete|move|file/);
  });
});

describe("MaintenanceGate when the user opts in", () => {
  it("reveals the button, persists the choice and announces it once", () => {
    const shell = mount(makeFeatureFlag(localStorage));
    toggle(shell, true);

    expect(shell.button.hidden).toBe(false);
    expect(shell.gate.enabled).toBe(true);
    expect(shell.changes).toEqual([true]);
    expect(localStorage.getItem(MAINTENANCE_FLAG_KEY)).toBe("1");
  });

  it("persists through the DEFAULT flag, with none injected", () => {
    // The constructor's `flag = localFeatureFlag()` default is what the app actually gets, so
    // the remembering it provides is only real if the default is exercised rather than replaced.
    const shell = mount();
    toggle(shell, true);
    expect(localStorage.getItem(MAINTENANCE_FLAG_KEY)).toBe("1");
  });

  it("comes up already ON, with the button reachable, on the next launch", () => {
    localStorage.setItem(MAINTENANCE_FLAG_KEY, "1");
    const shell = mount(makeFeatureFlag(localStorage));
    expect(shell.checkbox().checked).toBe(true);
    expect(shell.button.hidden).toBe(false);
    // Still no callback: nothing CHANGED, the flag was simply already set.
    expect(shell.changes).toEqual([]);
  });
});

describe("MaintenanceGate when the user revokes", () => {
  it("hides the button again, forgets the key and announces the revocation", () => {
    localStorage.setItem(MAINTENANCE_FLAG_KEY, "1");
    const shell = mount(makeFeatureFlag(localStorage));
    toggle(shell, false);

    expect(shell.button.hidden).toBe(true);
    expect(shell.gate.enabled).toBe(false);
    expect(shell.changes).toEqual([false]);
    expect(localStorage.getItem(MAINTENANCE_FLAG_KEY)).toBeNull();
  });

  it("reports the STORED truth, not the checkbox, when a write is refused", () => {
    // A hardened webview can refuse to persist. The checkbox would happily show `on`; the
    // gate must not, because on the next launch the plane will be off and a UI that claimed
    // otherwise taught the user to trust a switch that does not hold.
    const refusing: FeatureFlag = { enabled: () => false, set: () => {} };
    const shell = mount(refusing);
    toggle(shell, true);

    expect(shell.gate.enabled).toBe(false);
    expect(shell.button.hidden).toBe(true);
    expect(shell.checkbox().checked).toBe(false);
    // And it still tells the caller what the user asked for, so a pane can be closed.
    expect(shell.changes).toEqual([false]);
  });
});

describe("MaintenanceGate lifecycle", () => {
  it("mounts exactly one switch, inside the container it was given", () => {
    const shell = mount();
    expect(shell.nav.querySelectorAll("input[type=checkbox]").length).toBe(1);
    expect(shell.checkbox().closest("#workspace-nav")).toBe(shell.nav);
  });

  it("leaves the buttons it did not build alone", () => {
    const shell = mount();
    expect(shell.nav.querySelector<HTMLButtonElement>("#btn-dedup")?.hidden).toBe(false);
  });

  it("stops governing the button after destroy", () => {
    const shell = mount(makeFeatureFlag(localStorage));
    shell.gate.destroy();
    toggle(shell, true);
    expect(shell.button.hidden).toBe(true);
    expect(shell.changes).toEqual([]);
  });
});
