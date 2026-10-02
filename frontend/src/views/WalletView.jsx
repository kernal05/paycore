import { useEffect, useState } from "react";
import { api } from "../api.js";
import { money, statusInfo } from "../format.js";

export default function WalletView({ onOpenLedger }) {
  const [accounts, setAccounts] = useState([]);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState("150.00");
  const [step, setStep] = useState("form");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const [copied, setCopied] = useState(false);

  async function loadAccounts() {
    try {
      const d = await api.listAccounts();
      setAccounts(d.accounts);
      setFrom((f) => f || d.accounts[0]?.id || "");
      setTo((t) => t || d.accounts[1]?.id || "");
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => {
    loadAccounts();
  }, []);

  const src = accounts.find((a) => a.id === from);
  const dst = accounts.find((a) => a.id === to);
  const minor = Math.round(parseFloat(amount) * 100);
  const valid = src && dst && from !== to && minor > 0 && src.currency === dst.currency;
  const insufficient = src && minor > src.balance_minor;

  async function send() {
    setBusy(true);
    setError(null);
    try {
      const result = await api.createPayment({
        idempotency_key: `console-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        source_account_id: from,
        dest_account_id: to,
        amount_minor: minor,
        currency: src.currency,
      });
      setReceipt({ ...result, from: src.name, to: dst.name, amount_minor: minor, currency: src.currency, ts: new Date() });
      setStep("receipt");
      loadAccounts();
    } catch (e) {
      setError(e.message);
      setStep("form");
    } finally {
      setBusy(false);
    }
  }

  function copyId() {
    navigator.clipboard?.writeText(receipt.transaction_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div>
      <div className="view-header">
        <h1>Wallet</h1>
        <p>Send money between accounts and see balances update in real time.</p>
      </div>

      <div className="acct-grid">
        {accounts.map((a) => (
          <div className="acct-card" key={a.id}>
            <div className="acct-name">{a.name}</div>
            <div className="acct-balance">{money(a.balance_minor, a.currency)}</div>
          </div>
        ))}
      </div>

      {error && <div className="error-box">{error}</div>}

      {step === "form" && (
        <div className="panel">
          <div className="panel-title">New payment</div>
          <div className="field-row">
            <div className="field">
              <label>From</label>
              <select value={from} onChange={(e) => setFrom(e.target.value)}>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
            <div className="field">
              <label>To</label>
              <select value={to} onChange={(e) => setTo(e.target.value)}>
                {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </div>
            <div className="field">
              <label>Amount ({src?.currency || "INR"})</label>
              <input type="number" step="0.01" min="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} />
            </div>
          </div>
          {from === to && from && <div className="hint">Choose two different accounts.</div>}
          {valid && insufficient && (
            <div className="hint">Heads up: this is more than {src.name}'s balance, so the bank is likely to decline it.</div>
          )}
          <button className="btn" disabled={!valid} onClick={() => setStep("confirm")}>Review payment</button>
        </div>
      )}

      {step === "confirm" && (
        <div className="panel">
          <div className="panel-title">Confirm payment</div>
          <div className="summary">
            <div className="summary-amount">{money(minor, src.currency)}</div>
            <div className="summary-route">{src.name} → {dst.name}</div>
          </div>
          <button className="btn" disabled={busy} onClick={send}>{busy ? "Processing…" : "Confirm and send"}</button>{" "}
          <button className="btn btn-ghost" disabled={busy} onClick={() => setStep("form")}>Back</button>
        </div>
      )}

      {step === "receipt" && receipt && (
        <div className="panel">
          <div className="panel-title">Receipt</div>
          <div className="summary">
            <div className="summary-amount">{money(receipt.amount_minor, receipt.currency)}</div>
            <div className="summary-route">{receipt.from} → {receipt.to}</div>
            <span className={`badge ${statusInfo(receipt.status, receipt.fraud_decision).cls}`}>
              {statusInfo(receipt.status, receipt.fraud_decision).label}
            </span>
          </div>
          <div className="hint">
            Reference <span className="mono">{receipt.transaction_id}</span>
          </div>
          <button className="btn" onClick={copyId}>{copied ? "Copied" : "Copy reference"}</button>{" "}
          <button className="btn btn-ghost" onClick={() => onOpenLedger?.(receipt.transaction_id)}>View timeline</button>{" "}
          <button className="btn btn-ghost" onClick={() => { setStep("form"); setReceipt(null); }}>New payment</button>
        </div>
      )}
    </div>
  );
}
