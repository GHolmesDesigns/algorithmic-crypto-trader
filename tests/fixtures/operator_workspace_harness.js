const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");

function boot(storage, pathname = "/operator") {
  const classes = new Set();
  const listeners = {};
  const attributes = { "aria-expanded": "true" };
  const button = {
    hidden: true,
    textContent: "Collapse navigation",
    getAttribute(name) { return attributes[name]; },
    setAttribute(name, value) { attributes[name] = value; },
    addEventListener(name, handler) { listeners[name] = handler; },
  };
  const navigation = {};
  const modules = ["overview", "watch-coins", "activity", "research", "system"];
  const links = modules.map((name) => {
    const linkAttributes = {
      "data-module": name,
      ...(name === "overview" ? { "aria-current": "page" } : {}),
    };
    return {
      getAttribute(key) { return linkAttributes[key] ?? null; },
      setAttribute(key, value) { linkAttributes[key] = value; },
      removeAttribute(key) { delete linkAttributes[key]; },
      current() { return "aria-current" in linkAttributes; },
      name,
    };
  });
  const context = {
    document: {
      documentElement: {
        classList: {
          toggle(name, enabled) {
            if (enabled) classes.add(name);
            else classes.delete(name);
          },
        },
      },
      getElementById(id) {
        return id === "workspace-nav-toggle" ? button : navigation;
      },
      querySelectorAll() { return links; },
    },
    localStorage: storage,
    location: { pathname, hash: "" },
    addEventListener() {},
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return {
    button,
    classes,
    click() { listeners.click(); },
    current() { return links.filter((link) => link.current()).map((link) => link.name); },
    state() {
      return {
        expanded: button.getAttribute("aria-expanded"),
        hidden: button.hidden,
        label: button.textContent,
        collapsedClass: classes.has("nav-collapsed"),
      };
    },
  };
}

const values = new Map();
const storage = {
  getItem(key) { return values.get(key) ?? null; },
  setItem(key, value) { values.set(key, value); },
};
const initial = boot(storage);
const expandedByDefault = initial.state();
initial.click();
const collapsed = initial.state();
const storedValue = values.get("act.operator.sidebar-collapsed.v1");
const reloaded = boot(storage).state();
const denied = boot({
  getItem() { throw new Error("storage denied"); },
  setItem() { throw new Error("storage denied"); },
}).state();
const deniedWrite = boot({
  getItem() { return null; },
  setItem() { throw new Error("storage denied"); },
});
deniedWrite.click();

const current = {};
for (const path of [
  "/operator",
  "/operator/system",
  "/operator/rearm",
  "/operator/diagnostics",
  "/operator/watchlist",
  "/operator/history/orders",
  "/operator/research",
]) {
  current[path] = boot({ getItem() { return null; }, setItem() {} }, path).current();
}

console.log(JSON.stringify({
  current,
  initial: expandedByDefault,
  collapsed,
  storedValue,
  reloaded,
  denied,
  deniedWrite: deniedWrite.state(),
}));
