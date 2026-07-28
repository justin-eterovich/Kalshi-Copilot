/**
 * The emergency stop.
 *
 * Until this existed the kill switch was config-only: editing `config.yaml`
 * and restarting containers, over SSH, while whatever prompted the panic was
 * still happening. It is documented in the README's safety table as an
 * operable control, and the dashboard showed it in three places — all of them
 * read-only.
 *
 * Four things this has to get right:
 *
 * 1. **Reachable from anywhere.** It lives in the topbar, which is sticky, so
 *    it is one click from every page rather than three tabs away.
 * 2. **Not firable by accident.** Opening the panel is one click; engaging is
 *    a second, deliberate one against a button that names the consequence.
 * 3. **It never claims a state it has not been told.** The pill renders the
 *    *server's* `kill_switch`, never an optimistic local guess. A stop that
 *    looks engaged and is not is worse than no control at all, so a failed
 *    request leaves the old state on screen with the error beside it.
 * 4. **It says what it did, and how to undo it.** "Engaged" alone leaves the
 *    operator wondering whether the resting orders went with it.
 */

import { useState } from "react";
import { api, type KillSwitchResult } from "./api";

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

export default function KillSwitch({
  engaged,
  configFloor,
  onChanged,
}: {
  /** The server's view. This component never overrides it locally. */
  engaged: boolean;
  /** `config.yaml` pins it on — a runtime release cannot clear it. */
  configFloor?: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<KillSwitchResult | null>(null);

  const send = async (next: boolean) => {
    setBusy(true);
    setError(null);
    try {
      setOutcome(await api.killSwitch(next));
      setOpen(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
      // Re-read the server's state either way: after a failure the truth
      // matters more than after a success.
      onChanged();
    }
  };

  return (
    <div className="killswitch">
      <button
        className={engaged ? "btn kill-btn engaged" : "btn kill-btn"}
        aria-expanded={open}
        onClick={() => {
          setOutcome(null);
          setError(null);
          setOpen(!open);
        }}
        title="Halt all new proposals and cancel resting orders"
      >
        {engaged ? "■ kill switch: ENGAGED" : "■ kill switch"}
      </button>

      {open && (
        <div className="kill-panel">
          {engaged ? (
            <>
              <p className="kill-copy">
                <strong>The kill switch is engaged.</strong> No proposal is
                being accepted and resting orders were cancelled. Releasing it
                lets proposals be created and approved again; it does not put
                any cancelled order back.
              </p>
              {configFloor && (
                <p className="kill-copy wire-missing">
                  <code>risk.kill_switch</code> is <code>true</code> in{" "}
                  <code>config.yaml</code>, so this cannot be released from
                  here — the file is the floor. Change it there and restart.
                </p>
              )}
              <div className="approval-actions">
                <button
                  className="btn"
                  disabled={busy || configFloor}
                  onClick={() => send(false)}
                >
                  {busy ? "releasing…" : "release the kill switch"}
                </button>
                <button className="btn" onClick={() => setOpen(false)}>
                  keep it engaged
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="kill-copy">
                <strong>Engaging halts trading immediately.</strong> Every new
                proposal is refused and every resting order is cancelled.
                Positions you already hold are <em>not</em> closed — this stops
                the system from acting, it does not flatten your book. You can
                release it from here afterwards.
              </p>
              <div className="approval-actions">
                <button
                  className="btn kill-confirm"
                  disabled={busy}
                  onClick={() => send(true)}
                >
                  {busy ? "engaging…" : "engage — halt & cancel resting orders"}
                </button>
                <button className="btn" onClick={() => setOpen(false)}>
                  cancel
                </button>
              </div>
            </>
          )}
          {error && <p className="error">{error}</p>}
        </div>
      )}

      {/* What it did, in the server's words. "Engaged" on its own leaves the
          operator wondering whether the resting orders went with it — and an
          order the cancel could not reach is still live, which is the most
          urgent thing that can be on this screen. */}
      {outcome && (
        <div className="kill-panel">
          <p className="kill-copy">
            {outcome.kill_switch ? (
              <>
                <strong>Engaged.</strong> New proposals and approvals are
                halted. {outcome.canceled_orders}{" "}
                {plural(outcome.canceled_orders, "resting order was", "resting orders were")}{" "}
                cancelled. Positions you already hold are untouched.
              </>
            ) : (
              <>
                <strong>Released.</strong> Proposals are accepted again.
                Cancelled orders are not restored — re-propose anything you
                still want.
              </>
            )}
          </p>

          {outcome.failed_cancels.length > 0 && (
            <p className="kill-copy error">
              <strong>
                {outcome.failed_cancels.length}{" "}
                {plural(outcome.failed_cancels.length, "order", "orders")} could
                not be cancelled and{" "}
                {plural(outcome.failed_cancels.length, "is", "are")} still live.
              </strong>{" "}
              {outcome.failed_cancels
                .map((f) => `#${f.order_id}: ${f.error}`)
                .join(" · ")}{" "}
              Cancel {plural(outcome.failed_cancels.length, "it", "them")} by
              hand from Working orders, or on Kalshi directly.
            </p>
          )}

          <button className="btn" onClick={() => setOutcome(null)}>
            dismiss
          </button>
        </div>
      )}
    </div>
  );
}
