import { useState } from "react";
import { api, DEMO_ACCOUNTS } from "../api.js";

function formatMinor(amountMinor, currency) {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format(
    amountMinor / 100
  );
}

function StatusBadge({ status }) {
  const cls =
    status === "SETTLED" || status === "APPROVED_AND_SETTLED"
      ? "badge-pass"
      : status === "FAILED" || status === "DECLINED"
      ? "badge-fail"
      : "badge-pending";
  return <span className={`badge ${cls}`}>{status}</span>;
}

export default function WalletView({ onPaymentCreated }) {
  const [amount, setAmount] = useState("150.00");
  const [currency, setCurrency] = useState("INR");
  const [direction, setDirection] = useState("wallet-to-merchant");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [activity, setActivity] = useState([]);

  const submit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    const [sourceId, destId] =
      direction === "wallet-to-merchant"
        ? [DEMO_ACCOUNTS.wallet.id, DEMO_ACCOUNTS.merchant.id]
        : [DEMO_ACCOUNTS.merchant.id, DEMO_ACCOUNTS.wallet.id];

    try {
      const amountMinor = Math.round(parseFloat(amount) * 100);
      const idempotencyKey = `console-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const result = await api.createPayment({
        idempotency_key: idempotencyKey,
        source_account_id: sourceId,
        dest_account_id: destId,
        amount_minor: amountMinor,
        currency,
      });
      setActivity((prev) => [{ ...result, amount_minor: amountMinor, currency, ts: new Date() }, ...prev]);
      onPaymentCreated?.(result.transaction_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="view-header">
        <h1>Wallet</h1>
        <p>Move money between the demo customer wallet and merchant settlement account.</p>
      </div>

      {error && <div className="error-box">{error}</div>}

      <form className="panel" onSubmit={submit}>
        <div className="panel-title">New payment</div>
        <div className="field-row">
          <div className="field">
            <label>Direction</label>
            <select value={direction} onChange={(e) => setDirection(e.target.value)}>
              <option value="wallet-to-merchant">Wallet → Merchant</option>
              <option value="merchant-to-wallet">Merchant → Wallet</option>
            </select>
          </div>
          <div className="field">
            <label>Amount</label>
            <input
              type="number"
              step="0.01"
              min="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label>Currency</label>
            <select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              <option value="INR">INR</option>
              <option value="USD">USD</option>
            </select>
          </div>
        </div>
        <button className="btn" type="submit" disabled={loading}>
          {loading ? "Processing…" : "Send payment"}
        </button>
        <div className="hint">
          wallet: <span className="mono">{DEMO_ACCOUNTS.wallet.id}</span>
          <br />
          merchant: <span className="mono">{DEMO_ACCOUNTS.merchant.id}</span>
        </div>
      </form>

      <div className="panel">
        <div className="panel-title">Session activity</div>
        {activity.length === 0 ? (
          <div className="empty-state">No payments sent yet in this session.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Transaction</th>
                <th>Amount</th>
                <th>Status</th>
                <th>Fraud score</th>
              </tr>
            </thead>
            <tbody>
              {activity.map((a) => (
                <tr key={a.transaction_id + a.ts.toISOString()}>
                  <td>{a.ts.toLocaleTimeString()}</td>
                  <td className="mono-small">{a.transaction_id?.slice(0, 8)}…</td>
                  <td>{formatMinor(a.amount_minor, a.currency)}</td>
                  <td>
                    <StatusBadge status={a.status} />
                  </td>
                  <td>{a.fraud_score}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
