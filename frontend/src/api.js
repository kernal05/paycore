// API client for the PayCore backend services.
//
// NOTE (v1, intentional): getServiceToken() below signs an HMAC bearer
// token client-side, using a secret baked into the frontend bundle at
// build time. That means anyone who opens devtools can read the secret
// out of the JS and mint their own admin tokens. This is a realistic
// anti-pattern, kept in on purpose for v1 so it can be found and fixed
// properly in the security review pass, the same way real bugs were
// found by running the backend. Do not copy this pattern into anything
// real. The correct fix is a backend-for-frontend that holds the secret
// server-side and the browser never sees it.

const PAYMENT_API = import.meta.env.VITE_PAYMENT_API_URL || "http://localhost:8000";
const LEDGER_API = import.meta.env.VITE_LEDGER_API_URL || "http://localhost:8001";
const RECON_API = import.meta.env.VITE_RECON_API_URL || "http://localhost:8003";

const CLIENT_API_KEY = import.meta.env.VITE_CLIENT_API_KEY || "demo-key-local-only";
const SERVICE_AUTH_SECRET =
  import.meta.env.VITE_SERVICE_AUTH_SECRET || "local-dev-only-secret-DO-NOT-USE-IN-PROD";

async function hmacSha256Hex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function getServiceToken() {
  const expiry = Math.floor(Date.now() / 1000) + 300;
  const payload = `console-ui.${expiry}`;
  const sig = await hmacSha256Hex(SERVICE_AUTH_SECRET, payload);
  return `${payload}.${sig}`;
}

async function request(url, { method = "GET", body, auth = "none" } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth === "api-key") {
    headers["X-API-Key"] = CLIENT_API_KEY;
  } else if (auth === "service") {
    headers["Authorization"] = `Bearer ${await getServiceToken()}`;
  }

  const res = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { raw: text };
  }

  if (!res.ok) {
    const message = data?.detail || data?.raw || `HTTP ${res.status}`;
    const err = new Error(typeof message === "string" ? message : JSON.stringify(message));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  // Wallet
  createPayment: (payload) => request(`${PAYMENT_API}/payments`, { method: "POST", body: payload, auth: "api-key" }),
  getPayment: (id) => request(`${PAYMENT_API}/payments/${id}`, { auth: "api-key" }),
  getBalance: (accountId) => request(`${LEDGER_API}/accounts/${accountId}/balance`, { auth: "service" }),

  // Ledger / forensic
  getTimeline: (id) => request(`${LEDGER_API}/transactions/${id}/timeline`, { auth: "service" }),
  getTransaction: (id) => request(`${LEDGER_API}/transactions/${id}`, { auth: "service" }),
  journalIntegrity: () => request(`${LEDGER_API}/journal/integrity-check`),
  ledgerConsistency: () => request(`${LEDGER_API}/ledger/consistency-check`),

  // Ops
  financialHealth: () => request(`${LEDGER_API}/financial-health`),
  recoveryPending: () => request(`${LEDGER_API}/recovery/pending`, { auth: "service" }),
  runRecoverySweep: () => request(`${LEDGER_API}/recovery/run-sweep`, { method: "POST", auth: "service" }),
  reconciliationReport: () => request(`${RECON_API}/reconcile/report`, { auth: "service" }),
  reconciliationExceptions: () => request(`${RECON_API}/reconciliation/exceptions`, { auth: "service" }),
  runReconciliationBatch: () => request(`${RECON_API}/reconcile/run-batch`, { method: "POST", auth: "service" }),
  listAccounts: () => request(`${LEDGER_API}/accounts`, { auth: "service" }),
  listTransactions: (limit = 100) => request(`${LEDGER_API}/transactions?limit=${limit}`, { auth: "service" }),
  reviewPending: () => request(`${LEDGER_API}/review/pending`, { auth: "service" }),
  reviewApprove: (id, body) => request(`${LEDGER_API}/transactions/${id}/review-approve`, { method: "POST", body, auth: "service" }),
  reviewReject: (id, body) => request(`${LEDGER_API}/transactions/${id}/review-reject`, { method: "POST", body, auth: "service" }),
};

export const DEMO_ACCOUNTS = {
  wallet: { id: "11111111-1111-1111-1111-111111111111", name: "Customer Wallet" },
  merchant: { id: "22222222-2222-2222-2222-222222222222", name: "Merchant Settlement" },
};
