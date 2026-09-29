/* Progressive enhancement for the Markets page. The SVG and table remain the source of truth. */
(function () {
  "use strict";

  var tiles = document.querySelectorAll("[data-market-tile]");
  if (!tiles.length || !window.LightweightCharts) return;

  var charts = window.LightweightCharts;
  var darkMode = window.matchMedia("(prefers-color-scheme: dark)");

  function palette() {
    return darkMode.matches
      ? {
          background: "#18201d",
          text: "#f0f2ee",
          grid: "#29332f",
          border: "#3a4640",
          ink: "#b2bcb5",
        }
      : {
          background: "#f8f9f6",
          text: "#1b231f",
          grid: "#dee3d7",
          border: "#ccd2c6",
          ink: "#4b5650",
        };
  }

  function number(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function time(value) {
    var parsed = Date.parse(value);
    return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : null;
  }

  function records(series, kind) {
    var result = [];
    (series.bars || []).forEach(function (bar) {
      var timestamp = time(bar.opened_at);
      if (timestamp === null) return;
      if (kind === "line") {
        var close = number(bar.close);
        if (close !== null) result.push({ time: timestamp, value: close });
        return;
      }
      var open = number(bar.open);
      var high = number(bar.high);
      var low = number(bar.low);
      var candleClose = number(bar.close);
      if (open !== null && high !== null && low !== null && candleClose !== null) {
        result.push({ time: timestamp, open: open, high: high, low: low, close: candleClose });
      }
    });
    return result;
  }

  function volumeRecords(series) {
    return (series.bars || []).reduce(function (result, bar) {
      var timestamp = time(bar.opened_at);
      var volume = number(bar.volume);
      if (timestamp !== null && volume !== null) result.push({ time: timestamp, value: volume });
      return result;
    }, []);
  }

  function activityRecords(activity, color) {
    return ((activity && activity.rows) || []).reduce(function (result, row) {
      var timestamp = time(row.at);
      if (timestamp === null) return result;
      var sell = String(row.side || "").toLowerCase() === "sell";
      result.push({
        time: timestamp,
        position: sell ? "aboveBar" : "belowBar",
        shape: sell ? "arrowDown" : "arrowUp",
        color: color,
        text: String(row.label || row.kind || "A").slice(0, 1),
      });
      return result;
    }, []);
  }

  function enhance(tile) {
    var mount = tile.querySelector("[data-chart-mount]");
    var enhancement = tile.querySelector("[data-market-enhancement]");
    var fallback = tile.querySelector(".market-chart");
    var type = tile.querySelector("[data-chart-type]");
    var volume = tile.querySelector("[data-volume]");
    var reset = tile.querySelector("[data-reset-chart]");
    var url = tile.dataset.candlesUrl;
    if (!mount || !enhancement || !url) return;

    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("chart data unavailable");
        return response.json();
      })
      .then(function (payload) {
        var symbol = tile.dataset.symbol;
        var series = (payload.symbols || []).find(function (item) { return item.symbol === symbol; });
        if (!series || !records(series, "line").length) throw new Error("chart data empty");
        var activity = (payload.activity && payload.activity.symbols || []).find(function (item) { return item.symbol === symbol; });

        var colors = palette();
        enhancement.hidden = false;
        try {
          var chart = charts.createChart(mount, {
            width: mount.clientWidth,
            autoSize: true,
            height: 240,
            layout: { background: { type: charts.ColorType.Solid, color: colors.background }, textColor: colors.text },
            grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
            rightPriceScale: { borderColor: colors.border },
            timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: false },
            crosshair: { mode: charts.CrosshairMode.Normal },
            attributionLogo: true,
            handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
            handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
          });
          var activeSeries = null;
          var volumeSeries = null;

          function render() {
            if (activeSeries) chart.removeSeries(activeSeries);
            if (volumeSeries) chart.removeSeries(volumeSeries);
            if (type.value === "candles") {
              activeSeries = chart.addSeries(charts.CandlestickSeries, {
                upColor: colors.background,
                downColor: colors.ink,
                borderUpColor: colors.ink,
                borderDownColor: colors.ink,
                wickUpColor: colors.ink,
                wickDownColor: colors.ink,
              });
              activeSeries.setData(records(series, "candles"));
            } else {
              activeSeries = chart.addSeries(charts.LineSeries, {
                color: colors.ink,
                lineWidth: 2,
              });
              activeSeries.setData(records(series, "line"));
            }
            if (activeSeries && charts.createSeriesMarkers) {
              charts.createSeriesMarkers(activeSeries, activityRecords(activity, colors.ink));
            }
            if (volume.checked) {
              volumeSeries = chart.addSeries(charts.HistogramSeries, {
                color: colors.ink,
                priceFormat: { type: "volume" },
                priceScaleId: "volume",
                scaleMargins: { top: 0.8, bottom: 0 },
              });
              volumeSeries.setData(volumeRecords(series));
            }
            chart.timeScale().fitContent();
          }

          type.addEventListener("change", render);
          volume.addEventListener("change", render);
          reset.addEventListener("click", function () { chart.timeScale().fitContent(); });
          render();
          if (fallback) fallback.remove();
        } catch (error) {
          enhancement.hidden = true;
          throw error;
        }
      })
      .catch(function () {
        /* A blocked library, failed request, or malformed response keeps the SVG. */
      });
  }

  tiles.forEach(enhance);
}());
