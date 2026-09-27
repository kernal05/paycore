import uuid

import httpx
import pytest

PAYMENT_API = "http://localhost:8000"
LEDGER_SERVICE = "http://localhost:8001"
API_KEY = "demo-key-local-only"

CUSTOMER_ACCOUNT = "11111111-1111-1111-1111-111111111111"
MERCHANT_ACCOUNT = "22222222-2222-2222-2222-222222222222"


def _stack_is_up() -> bool:
    try:
        r = httpx.get(f"{PAYMENT_API}/healthz", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


requires_stack = pytest.mark.skipif(
    not _stack_is_up(),
    reason="docker-compose stack is not running — start it with `docker compose up --build` first",
)


@pytest.fixture
def api_client():
    with httpx.Client(base_url=PAYMENT_API, headers={"X-API-Key": API_KEY}, timeout=10.0) as c:
        yield c


@pytest.fixture
def ledger_client():
    from common.auth import issue_token
    headers = {"Authorization": f"Bearer {issue_token('test-suite')}"}
    with httpx.Client(base_url=LEDGER_SERVICE, headers=headers, timeout=10.0) as c:
        yield c


@pytest.fixture
def idem_key():
    return f"test-{uuid.uuid4()}"


def make_payment(client: httpx.Client, idempotency_key: str, amount_minor: int = 1000,
                  source: str = CUSTOMER_ACCOUNT, dest: str = MERCHANT_ACCOUNT):
    return client.post("/payments", json={
        "idempotency_key": idempotency_key,
        "source_account_id": source,
        "dest_account_id": dest,
        "amount_minor": amount_minor,
        "currency": "INR",
        "device_id": "test-device",
        "ip_address": "127.0.0.1",
    })
