# Coinbase Advanced Trade response fixtures

These files follow the response shapes in the Coinbase Advanced Trade REST API
reference as read on 2026-09-24 (Create Order, Cancel Orders, Edit Order, Get
Order, Get API Key Permissions, and the common error body). They were written
from the documentation, not captured from the sandbox. The error scenarios
match the sandbox's documented `X-Sandbox` variants:

| File | Sandbox header |
| --- | --- |
| `create_order_insufficient_fund.json` | `X-Sandbox: PostOrder_insufficient_fund` |
| `cancel_orders_failure.json` | `X-Sandbox: CancelOrders_failure` |
| `edit_order_failure.json` | `X-Sandbox: EditOrder_failure` |

`{client_order_id}` is replaced by the test with the request's identifier.
Replace these with redacted owner-run captures from
`https://api-sandbox.coinbase.com` when they are taken; the tests should pass
unchanged.
