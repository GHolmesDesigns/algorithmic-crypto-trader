# Phase 1.2 market-data evidence

This card adds the provider-scoped Coinbase Advanced Trade market-data path. The
implementation is deliberately split between deterministic local behavior and
owner-run provider evidence:

- `data.coinbase.CoinbaseRESTClient` is public, bounded, rate-limited, and circuit-broken.
- `data.backfill.HistoricalCandleBackfiller` resumes after the latest persisted candle and
  uses the `(symbol, interval, opened_at)` unique key for idempotent writes.
- `data.stream.CoinbaseWebSocketIngestor` subscribes to candles, ticker, and heartbeats,
  records raw messages, fails closed on heartbeat timeout, and invokes REST gap fill after
  disconnect. The candles channel sends five-minute buckets updated every second, so a
  bucket is emitted only after it closes, and gap fill stops at the bucket in progress
  (corrected by the [Phase 1 gate](phase-1-gate-acceptance.md)).
- `data.validation.MarketDataValidator` rejects duplicate bars, gaps, bad durations,
  suspicious jumps, and stale quotes before downstream use.

The focused test suite proves the failure and recovery paths with deterministic fakes. A
real 72-hour continuous-ingest run is not performed in CI and remains an owner-run
verification item. It must use the public Coinbase stream, a bounded REST gap-fill budget,
an append-only replay destination, and a dated redacted result showing zero unexplained
gaps before the Phase 1 gate can claim that exit criterion.

No Coinbase credentials are accepted by this market-data adapter, and no order, transfer,
withdrawal, or other provider write is reachable from it.
