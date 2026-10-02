import { useEffect, useState } from "react";
import { api } from "../api.js";
import { money, when } from "../format.js";

export default function ReviewQueue() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [message, setMessage] = useState(null);
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(null);

  async function load() {
    try {
      const data = await api.reviewPending();
      setItems(data.pending);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  }
  useEffect(() => { load(); }, []);

  async function decide(id, action) {
    setBusy(id);
    setMessage(null);
    setError(null);
    try {
      const body = { reviewer: reviewer.trim(), note: note.trim() };
      const res = action === "approve" ? await api.reviewApprove(id, body) : await api.reviewReject(id, body);
      setMessage(`${id.slice(0, 8)} ${action === "approve" ? "approved" : "rejected"} - status now ${res.status}`);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  const ready = reviewer.trim() !== "" && note.trim() !== "";

  return (
    <div className="panel">
      <div className="panel-title">Fraud review queue</div>
      <p style={{ marginBottom: 12 }}>
        Payments held by the fraud engine. Approving sends the money; rejecting fails the payment. Both are audited.
      </p>
      <div className="field-row">
        <div className="field">
          <label>Your name (required)</label>
          <input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="e.g. Vishal" />
        </div>
        <div className="field" style={{ flex: 2 }}>
          <label>Reason / note (required)</label>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="e.g. verified with customer" />
        </div>
      </div>
      {!ready && <div className="hint" style={{ marginBottom: 10 }}>Fill in your name and a note to enable Approve / Reject.</div>}
      {error && <div className="error-box">{error}</div>}
      {message && <div className="hint" style={{ color: "#5fd1a8", marginBottom: 10 }}>{message}</div>}
      <button className="btn btn-ghost" onClick={load}>Refresh</button>

      {items && items.length === 0 && <div className="empty-state">Nothing waiting for review.</div>}
      {items && items.length > 0 && (
        <table>
          <thead>
            <tr><th>Ref</th><th>Amount</th><th>Fraud score</th><th>Reasons</th><th>Created</th><th>Decision</th></tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.id}>
                <td className="mono-small" title={t.id}>{t.id.slice(0, 8)}</td>
                <td>{money(t.amount_minor, t.currency)}</td>
                <td>{t.fraud_score}</td>
                <td>{Array.isArray(t.fraud_reasons) ? t.fraud_reasons.join(", ") : ""}</td>
                <td>{when(t.created_at)}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  <button className="btn btn-approve" disabled={!ready || busy === t.id} onClick={() => decide(t.id, "approve")}>Approve</button>{" "}
                  <button className="btn btn-reject" disabled={!ready || busy === t.id} onClick={() => decide(t.id, "reject")}>Reject</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
