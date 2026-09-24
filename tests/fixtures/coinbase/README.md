# Coinbase Advanced Trade response fixtures

The files in this folder follow the response shapes in the Coinbase Advanced
Trade REST API reference as read on 2026-09-24 (Create Order, Cancel Orders, Edit
Order, Get Order, Get API Key Permissions, and the common error body). They were
written from the documentation, not captured from the sandbox. The error
scenarios match the sandbox's documented `X-Sandbox` variants:

| File | Sandbox header |
| --- | --- |
| `create_order_insufficient_fund.json` | `X-Sandbox: PostOrder_insufficient_fund` |
| `cancel_orders_failure.json` | `X-Sandbox: CancelOrders_failure` |
| `edit_order_failure.json` | `X-Sandbox: EditOrder_failure` |

`{client_order_id}` is replaced by the test with the request's identifier.

## Captured sandbox responses

`sandbox/` holds the sandbox's own responses:
- **When and how:** captured on 2026-09-24 by `python -m probes.coinbase_sandbox_capture`, from `https://api-sandbox.coinbase.com/api/v3/brokerage`, which needs no authentication. See `sandbox/manifest.json`.
- **File format:** each file records the request that produced it, the HTTP status, and the response, unedited. The sandbox's data is static and fictional, so nothing needed redacting.
- **Tests:** `tests/test_coinbase_sandbox_capture.py` replays them through the adapter.

The sandbox echoes fixed identifiers rather than the request's, and Get Order
answers only for its own sample orders. So the documented fixtures above stay
in use for the tests that follow one order across several requests. To refresh
the capture, run the probe again; it overwrites `sandbox/`.
