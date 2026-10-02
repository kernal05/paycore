import { useState } from "react";
import WalletView from "./views/WalletView.jsx";
import HistoryView from "./views/HistoryView.jsx";
import LedgerView from "./views/LedgerView.jsx";
import OpsView from "./views/OpsView.jsx";

const VIEWS = [
  { id: "wallet", label: "Wallet" },
  { id: "history", label: "History" },
  { id: "ledger", label: "Ledger" },
  { id: "ops", label: "Ops Console" },
];

export default function App() {
  const [active, setActive] = useState("wallet");
  const [lastTxnId, setLastTxnId] = useState(null);

  const openLedger = (id) => {
    setLastTxnId(id);
    setActive("ledger");
  };

  return (
    <div className="shell">
      <nav className="rail">
        <div className="brand">
          pay<span>core</span>
        </div>
        {VIEWS.map((v) => (
          <button key={v.id} className={`nav-item ${active === v.id ? "active" : ""}`} onClick={() => setActive(v.id)}>
            {v.label}
          </button>
        ))}
        <div className="rail-footer">
          demo environment
          <br />
          local-only credentials
        </div>
      </nav>
      <main className="main">
        {active === "wallet" && <WalletView onOpenLedger={openLedger} />}
        {active === "history" && <HistoryView onOpen={openLedger} />}
        {active === "ledger" && <LedgerView initialTxnId={lastTxnId} />}
        {active === "ops" && <OpsView />}
      </main>
    </div>
  );
}
