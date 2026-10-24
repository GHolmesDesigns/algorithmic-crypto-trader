const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const KEY = "act.operator.appearance.v1";

function memoryStorage(initial) {
  const values = new Map(initial === undefined ? [] : [[KEY, initial]]);
  return {
    values,
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
  };
}

function boot(options) {
  const { storage, withPage = true, withMatchMedia = true, deviceDark = false } = options;
  const attributes = {};
  const radios = ["light", "dark", "system"].map((value) => ({
    name: "appearance",
    value,
    checked: value === "system",
  }));
  const groupListeners = {};
  const group = {
    hidden: true,
    querySelectorAll() { return radios; },
    addEventListener(name, handler) { groupListeners[name] = handler; },
  };
  const status = { textContent: "" };
  const fallback = { hidden: false };
  const documentListeners = {};
  const windowListeners = {};
  const media = {
    matches: deviceDark,
    listeners: [],
    addEventListener(_name, handler) { this.listeners.push(handler); },
  };
  const document = {
    readyState: options.readyState || "complete",
    documentElement: {
      setAttribute(name, value) { attributes[name] = value; },
      removeAttribute(name) { delete attributes[name]; },
    },
    addEventListener(name, handler) { documentListeners[name] = handler; },
    querySelector(selector) {
      if (!withPage) return null;
      if (selector === "[data-appearance-group]") return group;
      if (selector === "[data-appearance-status]") return status;
      if (selector === "[data-appearance-fallback]") return fallback;
      return null;
    },
  };
  const context = {
    document,
    localStorage: storage,
    addEventListener(name, handler) { windowListeners[name] = handler; },
  };
  if (withMatchMedia) context.matchMedia = () => media;
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);

  return {
    theme() { return attributes["data-theme"] === undefined ? null : attributes["data-theme"]; },
    checked() { return radios.filter((radio) => radio.checked).map((radio) => radio.value); },
    shown() { return { group: !group.hidden, fallback: !fallback.hidden }; },
    status() { return status.textContent; },
    finishLoading() { documentListeners.DOMContentLoaded(); },
    choose(value) { groupListeners.change({ target: { name: "appearance", value } }); },
    chooseOther() { groupListeners.change({ target: { name: "other", value: "dark" } }); },
    changeDevice(dark) {
      media.matches = dark;
      media.listeners.forEach((handler) => handler());
    },
    otherTab(key) { windowListeners.storage({ key }); },
    stored() { return storage.values ? storage.values.get(KEY) ?? null : null; },
  };
}

function snapshot(page) {
  return { theme: page.theme(), checked: page.checked(), shown: page.shown(), status: page.status() };
}

const result = {};

// Nothing stored: Follow device sets no override and the choices appear once JavaScript runs.
const fresh = boot({ storage: memoryStorage() });
result.fresh = snapshot(fresh);

// Each choice applies, is saved, and is still applied by the next page's first line of script.
const storage = memoryStorage();
const first = boot({ storage });
first.choose("dark");
result.dark = { ...snapshot(first), stored: first.stored() };
first.choose("light");
result.light = { ...snapshot(first), stored: first.stored() };
first.choose("dark");
const second = boot({ storage, readyState: "loading" });
result.reloadBeforeParsing = { theme: second.theme(), shown: second.shown() };
second.finishLoading();
result.reloadAfterParsing = snapshot(second);
second.choose("system");
result.system = { ...snapshot(second), stored: second.stored() };
result.afterSystem = snapshot(boot({ storage }));
first.choose("light");
first.chooseOther();
result.ignoredOtherControl = first.theme();

// Follow device tracks the device without touching the override; an explicit choice ignores it.
const following = boot({ storage: memoryStorage(), deviceDark: true });
result.followingDark = snapshot(following);
following.changeDevice(false);
result.followingLight = snapshot(following);
const explicit = boot({ storage: memoryStorage(), deviceDark: false });
explicit.choose("light");
explicit.changeDevice(true);
result.explicitLightOnDarkDevice = snapshot(explicit);

// Corrupt or foreign stored values fall back to Follow device.
result.corrupt = ["purple", "", "DARK", "dark ", "null", "{}", "[\"dark\"]"].map((value) =>
  boot({ storage: memoryStorage(value) }).theme());

// Storage that cannot be read or written never stops the page or the choice.
const denied = boot({
  storage: {
    getItem() { throw new Error("storage denied"); },
    setItem() { throw new Error("storage denied"); },
  },
});
result.deniedRead = snapshot(denied);
denied.choose("dark");
result.deniedChoice = snapshot(denied);
const missing = boot({ storage: undefined });
missing.choose("light");
result.missingStorage = snapshot(missing);
const readOnly = boot({
  storage: {
    getItem() { return null; },
    setItem() { throw new Error("quota"); },
  },
});
readOnly.choose("dark");
result.deniedWrite = snapshot(readOnly);

// A change made in another tab is followed; unrelated keys are ignored.
const sharedStorage = memoryStorage();
const tabA = boot({ storage: sharedStorage });
const tabB = boot({ storage: sharedStorage });
tabA.choose("dark");
tabB.otherTab("unrelated");
result.otherTabIgnored = snapshot(tabB);
tabB.otherTab(KEY);
result.otherTabFollowed = snapshot(tabB);

// Pages without the Appearance section, and browsers without matchMedia, still apply the choice.
const elsewhere = boot({ storage: memoryStorage("dark"), withPage: false });
result.noSection = elsewhere.theme();
const old = boot({ storage: memoryStorage("light"), withMatchMedia: false });
result.noMatchMedia = snapshot(old);
old.choose("system");
result.noMatchMediaSystem = snapshot(old);

console.log(JSON.stringify(result));
