# Phase 4.4: locally served interactive Markets charts

Issue #90. This is a read-only presentation enhancement on top of the bounded
JSON source from [Phase 4.2](phase-4.2-candle-read-model.md) and the
server-rendered Markets page from Phase 4.3. It does not reach a broker, change
the watchlist, submit an order, or affect strategy, risk, execution, portfolio,
reconciliation, the kill switch, or alerts.

## Local library

The repository vendors the standalone production build of TradingView
Lightweight Charts `5.2.1` in `api/static/markets/`. The release, build URL,
SHA-256, Apache license, and attribution notice are recorded beside the asset.
The service has no npm or Node build step and serves the file from the app's own
origin with a year-long immutable cache header. The checksum test must pass.

Only a reviewed pull request may update the library. Such a pull request must
pin the new release, replace the license and notice files, record the new source
URL and SHA-256, and update the checksum test.

## Progressive enhancement

`/operator/markets` first renders the existing SVG lines and accessible data
tables. An external, same-origin module fetches the existing
`GET /operator/markets/candles` endpoint. It replaces a tile's SVG only after
the library is available and the response contains usable data. The enhanced
tile offers a crosshair, mouse/pinch zoom, drag pan, line or candle view, and an
optional volume series. Rising and falling candles use neutral ink with hollow
and filled treatments; no health colour is used for price direction.

The JSON response remains decimal strings on the server. The browser converts
the presentation values to chart numbers only after authentication and the
same-origin request succeeds. No token or secret is placed in the page, script,
or response.

## Security and fallback

The Markets HTML response sets a page-specific Content-Security-Policy with
`script-src 'self'`, `connect-src 'self'`, no inline script, and
`frame-ancestors 'none'`. The local asset route returns JavaScript content type,
`nosniff`, and immutable caching. The page includes a visible TradingView
attribution link required by the library's Apache notice. Nothing loads from
TradingView at runtime.

If JavaScript is disabled, the library is blocked, the data request fails, or
the payload is malformed, the server SVG and table remain available. The other
operator pages have no script tags, and safety controls never depend on this
enhancement.

## Verification boundary

- Local validation: checksum, route headers, CSP, fallback, no-external-host,
  accessibility/source checks, focused tests, and the full repository gates.
- Remote CI: required checks for the exact finalized pull-request head.
- Owner-run verification: none; this feature reads the app's own database only
  and makes no provider request.
