const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(process.argv[3]);
const source = fs.readFileSync(process.argv[2], "utf8");

// Pure functions, loaded as a module.
const api = require(process.argv[2]);
const pure = {
  format: api.formatUtc(input.serverMs),
  formatBad: [api.formatUtc(NaN), api.formatUtc(Infinity), api.formatUtc("x"), api.formatUtc(8.64e15 + 1)],
  offset: api.offsetFrom(input.serverMs, input.deviceMs),
  serverNow: api.serverNow(api.offsetFrom(input.serverMs, input.deviceMs), input.deviceMs + 1234),
  waits: [0, 1, 250, 999, 1000, 1750, -1, -250].map((value) => api.msUntilNextSecond(value)),
};

// The script in a page: a fake document and a device clock set wrong.
function run(anchorAttr) {
  let device = input.deviceMs;
  const timers = [];
  const attrs = {};
  const labels = { text: "At page load" };
  const time = {
    textContent: input.serverText,
    getAttribute: () => null,
    setAttribute: (name, value) => { attrs[name] = value; },
  };
  const label = { get textContent() { return labels.text; }, set textContent(v) { labels.text = v; } };
  const clock = {
    getAttribute: (name) => (name === "data-server-epoch-ms" ? anchorAttr : null),
    querySelector: (selector) => (selector === "[data-clock-label]" ? label : time),
  };
  const listeners = {};
  const sandbox = {
    document: {
      hidden: false,
      querySelector: () => clock,
      addEventListener: (name, fn) => { listeners["document:" + name] = fn; },
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    addEventListener: (name, fn) => { listeners[name] = fn; },
    Date: class extends Date { static now() { return device; } },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  const out = { atLoad: time.textContent, label: labels.text, name: attrs["aria-label"], delays: [] };
  for (let i = 0; i < 3 && timers.length; i++) {
    const next = timers.pop();
    out.delays.push(next.ms);
    device += next.ms;
    next.fn();
    out["after" + (i + 1)] = time.textContent;
  }
  device += 3_600_000;
  listeners.pageshow && listeners.pageshow();
  out.afterSleep = time.textContent;
  out.datetime = attrs.datetime;
  return out;
}

console.log(JSON.stringify({ pure, good: run(String(input.serverMs)), bad: run("not a number") }));
