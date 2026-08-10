// @vitest-environment happy-dom
/**
 * DECISION G-8: the DOM shell of the "Copy diagnostics" action.
 *
 * Separate file with its own environment docblock, per `vitest.config.ts` — the default here
 * is `node` and a global DOM flip is measured to take four unrelated test files down.
 *
 * What is worth asserting in a DOM at all: that the button EXISTS with an accessible name,
 * that pressing it is ANNOUNCED (the whole interaction is one invisible clipboard write, so a
 * silent success is indistinguishable from a dead button — the exact defect class the repo's
 * `probe_keyboard_reach` / dead-button audits exist for), and that `destroy` really unbinds.
 * The bundle's CONTENT is proven in `diagnostics.test.ts` under node.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  BUTTON_LABEL,
  COPIED_MESSAGE,
  DiagnosticsButton,
  mountDiagnosticsButton,
  type DiagnosticsDeps,
} from "./diagnostics";

function deps(copy: DiagnosticsDeps["copy"]): DiagnosticsDeps {
  return {
    appInfo: vi.fn(async () => ({ version: "0.1.0", diagnostics: { engine_stderr: "" } })),
    snapshot: () => ({ health: null, stats: null, indexPath: null }),
    copy,
  };
}

beforeEach(() => {
  document.body.replaceChildren();
});

describe("mountDiagnosticsButton", () => {
  it("creates a real button with an accessible name and a polite status region", () => {
    const host = document.createElement("div");
    document.body.append(host);

    mountDiagnosticsButton(host, deps(async () => undefined));

    const button = host.querySelector("button");
    expect(button).not.toBeNull();
    expect(button?.type).toBe("button");
    expect(button?.textContent).toBe(BUTTON_LABEL);
    // A live region, so the copy is announced rather than silently painted.
    expect(host.querySelector('[role="status"]')).not.toBeNull();
    // Nothing is claimed before the first press.
    expect(host.querySelector('[role="status"]')?.textContent).toBe("");
  });

  it("copies on click and announces it", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const copy = vi.fn(async () => undefined);
    mountDiagnosticsButton(host, deps(copy));

    const button = host.querySelector("button") as HTMLButtonElement;
    button.click();
    // The handler is async and fire-and-forget by design (a click handler may not block), so
    // wait for the microtask chain rather than assume it completed synchronously.
    await vi.waitFor(() => expect(copy).toHaveBeenCalledTimes(1));

    expect(host.querySelector('[role="status"]')?.textContent).toBe(COPIED_MESSAGE);
    expect(button.disabled).toBe(false);
  });

  it("disables the button while collecting, so a second click cannot double-fire", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    let release: () => void = () => undefined;
    const copy = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          release = resolve;
        }),
    );
    mountDiagnosticsButton(host, deps(copy));

    const button = host.querySelector("button") as HTMLButtonElement;
    button.click();
    await vi.waitFor(() => expect(button.disabled).toBe(true));
    button.click();
    expect(copy).toHaveBeenCalledTimes(1);

    release();
    await vi.waitFor(() => expect(button.disabled).toBe(false));
  });

  it("shows the clipboard failure in the status region rather than throwing out of the handler", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    mountDiagnosticsButton(
      host,
      deps(async () => {
        throw new Error("clipboard blocked by policy");
      }),
    );

    (host.querySelector("button") as HTMLButtonElement).click();
    await vi.waitFor(() =>
      expect(host.querySelector('[role="status"]')?.textContent).toContain(
        "clipboard blocked by policy",
      ),
    );
  });
});

describe("DiagnosticsButton.destroy", () => {
  it("unbinds the click listener", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const copy = vi.fn(async () => undefined);
    const panel = mountDiagnosticsButton(host, deps(copy));
    const button = host.querySelector("button") as HTMLButtonElement;

    panel.destroy();
    button.click();
    // Give any stray handler a chance to run before concluding it did not.
    await Promise.resolve();
    await Promise.resolve();
    expect(copy).not.toHaveBeenCalled();
  });

  it("can be constructed against elements the shell already owns", () => {
    // The path `app.ts` would take if the topbar button were declared in `index.html`
    // instead of created here — both spellings must work, since which one lands is a
    // decision for whoever owns those files.
    const button = document.createElement("button");
    const status = document.createElement("span");
    document.body.append(button, status);
    const panel = new DiagnosticsButton(button, status, deps(async () => undefined));
    expect(status.textContent).toBe("");
    panel.destroy();
  });
});
