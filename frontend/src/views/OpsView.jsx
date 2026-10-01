import { useEffect, useState } from "react";
import { api } from "../api.js";

function CheckRow({ check }) {
  return (
    <tr>
      <td>{check.name}</td>
      <td>
        <span className={`badge ${check.status === "PASS" ? "badge-pass" : "badge-fail"}`}>
          {check.status}
        </span>
      </td>
      <td>{check.violation_count}</td>
    </tr>
  );
}

export default function OpsView() {
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const [pending, setPending] = useState(null);
  const [sweepResult, setSweepResult] = useState(null);
  const [reconReport, setReconReport] = useState(null);
  const [exceptions, setExceptions] = useState(null);
  const [busy, setBusy] = useState(null);
  const [authError, setAuthError] = useState(null);

  async function refreshHealth() {
    try {
      const data = await api.financialHealth();
      setHealth(data);
      setHealthError(null);
    } catch (err) {
      setHealthError(err.message);
    }
  }

  async function loadAdminData() {
    try {
      const [p, r, e] = await Promise.all([
        api.recoveryPending(),
        api.reconciliationReport(),
        api.reconciliationExceptions(),
      ]);
      setPending(p);
      setReconReport(r);
      setExceptions(e);
      setAuthError(null);
    } catch (err) {
      setAuthError(err.message);
    }
  }

  useEffect(() => {
    refreshHealth();
    loadAdminData();
  }, []);

  async function runSweep() {
    setBusy("sweep");
    try {
      const res = await api.runRecoverySweep();
      setSweepResult(res);
      await loadAdminData();
      await refreshHealth();
    } catch (err) {
      setAuthError(err.message);
    } finally {
      setBusy(null);
    }
  }

  async function runReconBatch() {
    setBusy("recon");
    try {
      await api.runReconciliationBatch();
      await loadAdminData();
    } catch (err) {
      setAuthError(err.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="view-header">
        <h1>Ops Console</h1>
        <p>Financial invariant monitoring, payment recovery, and reconciliation — the tools an on-call engineer would use.</p>
      </div>

      {authError && (
        <div className="error-box">
          Admin endpoint call failed: {authError}
          <br />
          <span style={{ opacity: 0.8 }}>
            (These endpoints require a signed service token — see the note in src/api.js about why
            that's a known gap in this build.)
          </span>
        </div>
      )}

      <div className="panel">
        <div className="panel-title">
          Financial health —{" "}
          {health && (
            <span className={`badge ${health.overall_status === "PASS" ? "badge-pass" : "badge-fail"}`}>
              {health.overall_status} · {health.total_violations} violation(s)
            </span>
          )}
        </div>
        {healthError && <div className="error-box">{healthError}</div>}
        {health && (
          <table>
            <thead>
              <tr>
                <th>Invariant</th>
                <th>Status</th>
                <th>Violations</th>
              </tr>
            </thead>
            <tbody>
              {health.checks.map((c) => (
                <CheckRow key={c.name} check={c} />
              ))}
            </tbody>
          </table>
        )}
        <button className="btn btn-ghost" style={{ marginTop: 12 }} onClick={refreshHealth}>
          Refresh
        </button>
      </div>

      <div className="panel">
        <div className="panel-title">Payment recovery engine</div>
        <p style={{ marginBottom: 12 }}>
          {pending ? `${pending.pending?.length ?? 0} transaction(s) parked in UNKNOWN, waiting on recovery.` : "—"}
        </p>
        <button className="btn" onClick={runSweep} disabled={busy === "sweep"}>
          {busy === "sweep" ? "Running sweep…" : "Run recovery sweep"}
        </button>
        {sweepResult && (
          <div className="mono-small" style={{ marginTop: 10 }}>
            checked: {sweepResult.checked} · results: {JSON.stringify(sweepResult.results)}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="panel-title">Reconciliation</div>
        {reconReport && (
          <p style={{ marginBottom: 12 }}>
            {reconReport.total_checked} checked · {reconReport.total_mismatches} mismatch(es) ·{" "}
            match rate {(reconReport.match_rate * 100).toFixed(1)}%
          </p>
        )}
        <button className="btn" onClick={runReconBatch} disabled={busy === "recon"}>
          {busy === "recon" ? "Running batch…" : "Run reconciliation batch"}
        </button>

        {exceptions?.exceptions?.length > 0 && (
          <table style={{ marginTop: 16 }}>
            <thead>
              <tr>
                <th>Transaction</th>
                <th>Category</th>
                <th>Internal</th>
                <th>Processor</th>
              </tr>
            </thead>
            <tbody>
              {exceptions.exceptions.map((ex) => (
                <tr key={ex.id}>
                  <td className="mono-small">{(ex.transaction_id || "").slice(0, 8)}…</td>
                  <td>{ex.category}</td>
                  <td>{ex.internal_status}</td>
                  <td>{ex.processor_status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
