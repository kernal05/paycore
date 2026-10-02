import { useEffect, useState } from "react";
import { api } from "../api.js";
import { money, when, statusInfo } from "../format.js";

const LABELS = {
  TRANSACTION_CREATED: "Payment received",
  PAYMENT_CREATED: "Payment request recorded",
  RISK_EVALUATED: "Security check",
  PAYMENT_SETTLED: "Money moved",
  REFUND_ISSUED: "Refund issued",
  RECONCILIATION_MATCH: "Matched with bank records",
  REVIEW_APPROVED: "Approved by reviewer",
  REVIEW_REJECTED: "Rejected by reviewer",
  REVIEW_APPROVAL_OUTCOME: "Result of approval",
  RECONCILIATION_MISMATCH: "Mismatch with bank records",
  WEBHOOK_RECEIVED: "Bank notification received",
  "OUTBOX_EVENT_CREATED:ledger.failed": "Payment failed",
  "OUTBOX_EVENT_CREATED:ledger.posted": "Settlement announced",
  "OUTBOX_EVENT_CREATED:ledger.refunded": "Refund announced",
  "OUTBOX_EVENT_CREATED:ledger.unknown": "Waiting on bank confirmation",
};
const REASONS = {
  processor_declined: "Declined by bank",
  manual_review_rejected: "Rejected by reviewer",
};

function summarize(ev) {
  const d = ev.detail || {};
  switch (ev.event) {
    case "RISK_EVALUATED":
      return `Risk score ${d.after?.fraud_score ?? "?"}, result: ${d.after?.status ?? "?"}`;
    case "PAYMENT_SETTLED":
      return `${d.before?.status ?? "?"} → ${d.after?.status ?? "?"}`;
    case "REFUND_ISSUED":
      return `Reason: ${(d.after?.reason || "").replace(/_/g, " ")}`;
    case "RECONCILIATION_MISMATCH":
      return d.detail || d.category || "";
    case "OUTBOX_EVENT_CREATED:ledger.failed":
      return [REASONS[d.reason] || d.reason, d.detail].filter(Boolean).join(" — ");
    case "TRANSACTION_CREATED":
      return d.amount_minor != null ? money(d.amount_minor, d.currency) : "";
    default:
      return "";
  }
}

export default function LedgerView({ initialTxnId }) {
  const [txnId, setTxnId] = useState(initialTxnId || "");
  const [timeline, setTimeline] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [integrity, setIntegrity] = useState(null);
  const [raw, setRaw] = useState(false);

  useEffect(() => {
    if (initialTxnId) {
      setTxnId(initialTxnId);
      lookUp(initialTxnId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTxnId]);

  useEffect(() => {
    api
      .journalIntegrity()
      .then((j) => api.ledgerConsistency().then((c) => setIntegrity({ journal: j, consistency: c })))
      .catch(() => setIntegrity(null));
  }, []);

  async function lookUp(id) {
    const target = id ?? txnId;
    if (!target) return;
    setLoading(true);
    setError(null);
    setTimeline(null);
    try {
      setTimeline(await api.getTimeline(target.trim()));
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const failedEv = timeline?.timeline.find((e) => e.event === "OUTBOX_EVENT_CREATED:ledger.failed");
  const info = timeline ? statusInfo(timeline.current_status, null, failedEv?.detail?.reason) : null;

  return (
    <div>
      <div className="view-header">
        <h1>Ledger</h1>
        <p>The full story of any payment, step by step.</p>
      </div>

      <div className="panel">
        <div className="panel-title">Find a payment</div>
        <div className="field-row">
          <div className="field" style={{ flex: 3 }}>
            <label>Reference</label>
            <input value={txnId} onChange={(e) => setTxnId(e.target.value)} placeholder="paste a reference, or click a row in History" />
          </div>
        </div>
        <button className="btn" onClick={() => lookUp()} disabled={loading || !txnId}>
          {loading ? "Looking up…" : "View timeline"}
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {timeline && (
        <div className="panel">
          <div className="panel-title">
            {money(timeline.amount_minor, timeline.currency)}{" "}
            <span className={`badge ${info.cls}`}>{info.label}</span>
          </div>
          <div className="hint" style={{ marginBottom: 10 }}>
            {timeline.event_count} events · reference <span className="mono">{timeline.transaction_id}</span>
          </div>
          <label className="hint"><input type="checkbox" checked={raw} onChange={(e) => setRaw(e.target.checked)} /> show raw technical detail</label>
          <table>
            <thead>
              <tr><th>When</th><th>What happened</th><th>Detail</th></tr>
            </thead>
            <tbody>
              {timeline.timeline.map((ev, i) => (
                <tr key={i}>
                  <td className="mono-small">{when(ev.ts)}</td>
                  <td>{LABELS[ev.event] || ev.event}</td>
                  <td>
                    {summarize(ev)}
                    {raw && <div className="mono-small">{JSON.stringify(ev.detail)}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {integrity && (
        <div className="panel">
          <div className="panel-title">Ledger-wide integrity</div>
          <div className="field-row">
            <div>
              Journal balance:{" "}
              <span className={`badge ${integrity.journal.healthy ? "badge-pass" : "badge-fail"}`}>
                {integrity.journal.healthy ? "HEALTHY" : "IMBALANCED"}
              </span>
            </div>
            <div>
              Account consistency:{" "}
              <span className={`badge ${integrity.consistency.healthy ? "badge-pass" : "badge-fail"}`}>
                {integrity.consistency.healthy ? "HEALTHY" : "INCONSISTENT"}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
