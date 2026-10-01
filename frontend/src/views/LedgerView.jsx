import { useEffect, useState } from "react";
import { api } from "../api.js";

export default function LedgerView({ initialTxnId }) {
  const [txnId, setTxnId] = useState(initialTxnId || "");
  const [timeline, setTimeline] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [integrity, setIntegrity] = useState(null);

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
      const data = await api.getTimeline(target);
      setTimeline(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <div className="view-header">
        <h1>Ledger</h1>
        <p>Look up any transaction's full forensic timeline — every event that touched it, in order.</p>
      </div>

      <div className="panel">
        <div className="panel-title">Transaction lookup</div>
        <div className="field-row">
          <div className="field" style={{ flex: 3 }}>
            <label>Transaction ID</label>
            <input
              value={txnId}
              onChange={(e) => setTxnId(e.target.value)}
              placeholder="paste a transaction id, e.g. from the Wallet tab"
            />
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
            Timeline — {timeline.event_count} events — current status {timeline.current_status}
          </div>
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Event</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {timeline.timeline.map((ev, i) => (
                <tr key={i}>
                  <td className="mono-small">{new Date(ev.ts).toLocaleTimeString()}</td>
                  <td>{ev.event}</td>
                  <td className="mono-small">{JSON.stringify(ev.detail)}</td>
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
