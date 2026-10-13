/* Ticks the dashboard's UTC clock from the server's time. Display only: it makes no request. */
(function (root) {
  "use strict";

  var LIVE_LABEL = "Now";
  var LIVE_NAME = "Current UTC time";

  function pad(value, width) {
    var text = String(value);
    while (text.length < width) text = "0" + text;
    return text;
  }

  // The page's stamp format, "YYYY-MM-DD HH:MM:SS UTC", or null when the time is not a real date.
  function formatUtc(ms) {
    if (typeof ms !== "number" || !isFinite(ms)) return null;
    var moment = new Date(ms);
    if (isNaN(moment.getTime())) return null;
    return (
      pad(moment.getUTCFullYear(), 4) + "-" + pad(moment.getUTCMonth() + 1, 2) + "-" +
      pad(moment.getUTCDate(), 2) + " " + pad(moment.getUTCHours(), 2) + ":" +
      pad(moment.getUTCMinutes(), 2) + ":" + pad(moment.getUTCSeconds(), 2) + " UTC"
    );
  }

  // Server time minus the device clock at load, so a wrong device clock or time zone cancels out.
  function offsetFrom(serverMs, deviceMs) {
    return serverMs - deviceMs;
  }

  function serverNow(offset, deviceMs) {
    return deviceMs + offset;
  }

  // Wait for the next whole second of server time, so the seconds never skip or repeat.
  function msUntilNextSecond(serverMs) {
    var into = ((serverMs % 1000) + 1000) % 1000;
    return 1000 - into;
  }

  var api = {
    formatUtc: formatUtc,
    offsetFrom: offsetFrom,
    serverNow: serverNow,
    msUntilNextSecond: msUntilNextSecond
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (!root.document) return;

  var clock = root.document.querySelector("[data-utc-clock]");
  if (!clock) return;
  var label = clock.querySelector("[data-clock-label]");
  var time = clock.querySelector("[data-clock-time]");
  var anchor = Number(clock.getAttribute("data-server-epoch-ms"));
  // Without a usable anchor the server-rendered "At page load" stamp stays, and still says so.
  if (!time || !isFinite(anchor)) return;

  var offset = offsetFrom(anchor, Date.now());
  var timer = null;
  var live = false;

  function show() {
    var now = serverNow(offset, Date.now());
    var text = formatUtc(now);
    if (text === null) return null;
    if (!live) {
      live = true;
      if (label) label.textContent = LIVE_LABEL;
      time.setAttribute("aria-label", LIVE_NAME);
    }
    if (time.textContent !== text) {
      time.textContent = text;
      time.setAttribute("datetime", new Date(now).toISOString());
    }
    return now;
  }

  function tick() {
    root.clearTimeout(timer);
    var now = show();
    if (now === null) return;
    timer = root.setTimeout(tick, msUntilNextSecond(now));
  }

  // A page restored from the back/forward cache or a sleeping device catches up at once.
  root.document.addEventListener("visibilitychange", function () {
    if (!root.document.hidden) tick();
  });
  root.addEventListener("pageshow", tick);
  tick();
})(typeof window !== "undefined" ? window : this);
