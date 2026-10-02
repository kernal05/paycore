import { useEffect, useState } from "react";
import { api } from "../api.js";
import { money, when, statusInfo } from "../format.js";

const RANGES = { "24h": 24 * 3600 * 1000, "7d": 7 * 24 * 3600 * 1000, all: Infinity };
const PAGE = 50;

export default function HistoryView({ onOpen }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [range, setRange] = useState("24h");
  const [visible, setVisible] = useState(PAGE);
  const [auto, setAuto] = useState(true);

  async function load() {
    try {
      const d = await api.listTransactions(200);
      setRows(d.transactions);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!auto) return;
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [auto]);
  useEffect(() => { setVisible(PAGE); }, [q, filter, range]);

  const now = Date.now();
  const filtered = (rows || []).filter((r) => {
    if (now - new Date(r.created_at).getTime() > RANGES[range]) return false;
    const info = statusInfo(r.status, r.fraud_decision, r.failure_reason);
    if (filter === "DONE" && info.label !== "Completed") return false;
    if (filter === "FAILED" && info.cls !== "badge-fail") return false;
    if (filter === "HELD" && info.cls !== "badge-pending") return false;
    const hay = `${r.id} ${r.source_name || ""} ${r.dest_name || ""} ${info.label}`.toLowerCase();
    return hay.includes(q.toLowerCase());
  });
  const shown = filtered.slice(0, visible);

  return (
    <div>
      <div className="view-header">
        <h1>History</h1>
        <p>Every payment, newest first. Click a row to see its full timeline.</p>
      </div>
      {error && <div className="error-box">{error}</div>}
      <div className="panel">
        <div className="toolbar">
          <input placeholder="Search by name, status or reference" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1, minWidth: 220 }} />
          <select value={range} onChange={(e) => setRange(e.target.value)}>
            <option value="24h">Last 24 hours</option>
            <option value="7d">Last 7 days</option>
            <option value="all">All time</option>
          </select>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="ALL">All statuses</option>
            <option value="DONE">Completed</option>
            <option value="HELD">In progress / held</option>
            <option value="FAILED">Failed / declined</option>
          </select>
          <label className="hint"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto-refresh</label>
          <button className="btn btn-ghost" onClick={load}>Refresh</button>
        </div>
        {!rows ? (
          <div className="empty-state">Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="empty-state">No payments match. Try a wider time range.</div>
        ) : (
          <>
            <div className="hint" style={{ marginBottom: 8 }}>Showing {shown.length} of {filtered.length}</div>
            <table>
              <thead>
                <tr><th>When</th><th>From → To</th><th>Amount</th><th>Status</th><th>Ref</th></tr>
              </thead>
              <tbody>
                {shown.map((r) => {
                  const info = statusInfo(r.status, r.fraud_decision, r.failure_reason);
                  return (
                    <tr key={r.id} className="row-click" onClick={() => onOpen?.(r.id)}>
                      <td>{when(r.created_at)}</td>
                      <td>{r.source_name || "—"} → {r.dest_name || "—"}{r.transaction_type === "REFUND" ? " (refund)" : ""}</td>
                      <td>{money(r.amount_minor, r.currency)}</td>
                      <td><span className={`badge ${info.cls}`}>{info.label}</span></td>
                      <td className="mono-small">{r.id.slice(0, 8)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {filtered.length > visible && (
              <div style={{ marginTop: 12 }}>
                <button className="btn btn-ghost" onClick={() => setVisible((v) => v + PAGE)}>Show more</button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
