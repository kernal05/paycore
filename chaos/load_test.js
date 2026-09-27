// Load test for POST /payments, ramping 10 -> 50 -> 100 virtual users.
// Run with: k6 run chaos/load_test.js
import http from 'k6/http';
import { check, sleep } from 'k6';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

export const options = {
  stages: [
    { duration: '30s', target: 10 },
    { duration: '1m', target: 50 },
    { duration: '1m', target: 100 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<1000', 'p(99)<2000'],  // starting targets — tighten once you have a real baseline
    http_req_failed: ['rate<0.05'],
  },
};

const BASE_URL = __ENV.PAYMENT_API_URL || 'http://localhost:8000';
const API_KEY = __ENV.API_KEY || 'demo-key-local-only';
const CUSTOMER_ACCOUNT = '11111111-1111-1111-1111-111111111111';
const MERCHANT_ACCOUNT = '22222222-2222-2222-2222-222222222222';

export default function () {
  const payload = JSON.stringify({
    idempotency_key: `load-${uuidv4()}`,
    source_account_id: CUSTOMER_ACCOUNT,
    dest_account_id: MERCHANT_ACCOUNT,
    amount_minor: 100,
    currency: 'INR',
    device_id: `load-test-device-${__VU}`,
    ip_address: '127.0.0.1',
  });

  const res = http.post(`${BASE_URL}/payments`, payload, {
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
  });

  check(res, {
    'status is 200 or 429 (rate-limited, not a server error)': (r) => r.status === 200 || r.status === 429,
  });

  sleep(0.1);
}
