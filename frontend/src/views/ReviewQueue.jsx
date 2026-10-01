import { useEffect, useState } from "react";
import { api } from "../api.js";

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

  useEffect(() => {
    load();
  }, []);

  async function decide(id, action) {
    setBusy(id);
    setMessage(null);
    try {
      const body = { reviewer: reviewer.trim(), note: note.trim() };
      const res =
        action === "approve" ? await api.reviewApprove(id, body) : await api.reviewReject(id, body);
      setMessage(`${id.slice(0, 8)} -> ${res.status}`);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  const ready = reviewer.trim() !== "" && note.trim() !== "";

  return (
    <div style={{ marginBottom: 32 }}>
      <h2>Fraud review queue</h2>
      <p>Payments held by the fraud engine. Approving posts the payment; rejecting fails it. Both are audited.</p>
      <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        <input placeholder="Your name (required)" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
        <input placeholder="Note (required)" value={note} onChange={(e) => setNote(e.target.value)} style={{ minWidth: 260 }} />
        <button onClick={load}>Refresh</button>
      </div>
      {error && <p>Error: {error}</p>}
      {message && <p>Done: {message}</p>}
      {items && items.length === 0 && <p>Nothing waiting for review.</p>}
      {items && items.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Transaction</th>
              <th>Amount</th>
              <th>Fraud score</th>
              <th>Reasons</th>
              <th>Created</th>
              <th>Decision</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr key={t.id}>
                <td title={t.id}>{t.id.slice(0, 8)}</td>
                <td>{(t.amount_minor / 100).toFixed(2)} {t.currency}</td>
                <td>{t.fraud_score}</td>
                <td>{Array.isArray(t.fraud_reasons) ? t.fraud_reasons.join(", ") : ""}</td>
                <td>{new Date(t.created_at).toLocaleTimeString()}</td>
                <td>
                  <button disabled={!ready || busy === t.id} onClick={() => decide(t.id, "approve")}>Approve</button>{" "}
                  <button disabled={!ready || busy === t.id} onClick={() => decide(t.id, "reject")}>Reject</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
