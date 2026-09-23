# Phase 1.4 research and strategy layer

Issue #6 adds a deterministic research layer that is independent of brokers,
databases, HTTP clients, and network transports.

## Execution contract

The backtester receives canonical `Candle` objects whose bars are already
closed. A strategy sees only the prefix ending at the current closed bar. A
signal formed on bar `n` executes at bar `n+1` open, with configured spread,
slippage, taker fee, and partial-fill assumptions. Any remaining position is
liquidated at the final bar close. This means a strategy cannot observe the
price at which its next order will execute while producing that signal.

The `enforce_closed_bar_guard=False` option exists only to support a negative
look-ahead canary test. It is not a production default and should not be used
for research conclusions.

## Reproducibility report

`BacktestReport` records the data source and window, symbols, granularity, all
cost assumptions, trade count, exposure, return-distribution statistics,
maximum drawdown and duration, calendar-year results, simple up/down/flat
regime results, the strategy version and SHA-256 version hash, final equity,
and completed trades.

`run_walk_forward` passes each training slice to a fresh strategy factory and
executes the following test slice. The final `holdout_bars` are excluded from
all training slices; the factory receives an empty tuple for the sealed
holdout strategy, making accidental holdout training observable.

## Reference strategy

`AlwaysBuyStrategy` is deliberately simple and is a machine-validation
instrument, not a profitability claim. The known-answer test compares its
buy-and-hold result with both-side fees to the cent. The look-ahead canary
shows that removing the guard produces an absurd result, so the test detects a
regression in the temporal boundary.
