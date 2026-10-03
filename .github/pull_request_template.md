## What and why

## How it was tested
- [ ] `ruff check common services tests`
- [ ] `pytest tests/unit`
- [ ] Integration tests / financial-health PASS (if ledger, payments or reconciliation changed)

## Risk
- [ ] Touches money movement or the state machine (needs extra care)
- [ ] Schema or config change; rollback plan:
