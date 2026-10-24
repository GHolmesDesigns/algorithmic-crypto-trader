/* Appearance preference only. No network, trading or server-setting side effects. */
(function (root) {
  "use strict";

  var KEY = "act.operator.appearance.v1";
  var CHOICES = ["system", "light", "dark"];
  var DOCUMENT = root.document;
  if (!DOCUMENT || !DOCUMENT.documentElement) return;
  var page = DOCUMENT.documentElement;
  var device = typeof root.matchMedia === "function"
    ? root.matchMedia("(prefers-color-scheme: dark)")
    : null;

  /* A missing, unreadable or unrecognised value means Follow device. */
  function clean(value) {
    return CHOICES.indexOf(value) === -1 ? "system" : value;
  }

  function readStored() {
    try {
      return { choice: clean(root.localStorage.getItem(KEY)), storage: true };
    } catch (_error) {
      return { choice: "system", storage: false };
    }
  }

  /* Follow device sets no override, so the stylesheet's device query decides. */
  function apply(choice) {
    if (choice === "light" || choice === "dark") {
      page.setAttribute("data-theme", choice);
    } else {
      page.removeAttribute("data-theme");
    }
  }

  var initial = readStored();
  var choice = initial.choice;
  var storage = initial.storage;
  apply(choice);

  /* Another tab changed the preference: follow it. */
  if (typeof root.addEventListener === "function") {
    root.addEventListener("storage", function (event) {
      if (event && event.key !== null && event.key !== KEY) return;
      choice = readStored().choice;
      apply(choice);
      sync();
    });
  }

  var group = null;
  var status = null;

  function deviceName() {
    return device && device.matches ? "dark" : "light";
  }

  function sync() {
    if (!group) return;
    var radios = group.querySelectorAll("input[name='appearance']");
    for (var index = 0; index < radios.length; index += 1) {
      radios[index].checked = radios[index].value === choice;
    }
    if (!status) return;
    var text = choice === "system"
      ? "Following this device, which is currently " + deviceName() + "."
      : (choice === "light" ? "Light" : "Dark") + " mode is on for every operator page.";
    if (!storage) {
      text += " This browser blocks storage, so the choice is not saved and lasts only until you leave this page.";
    }
    status.textContent = text;
  }

  function connect() {
    group = DOCUMENT.querySelector("[data-appearance-group]");
    if (!group) return;
    status = DOCUMENT.querySelector("[data-appearance-status]");
    var fallback = DOCUMENT.querySelector("[data-appearance-fallback]");
    group.hidden = false;
    if (fallback) fallback.hidden = true;
    group.addEventListener("change", function (event) {
      var target = event && event.target;
      if (!target || target.name !== "appearance") return;
      choice = clean(target.value);
      apply(choice);
      if (storage) {
        try {
          root.localStorage.setItem(KEY, choice);
        } catch (_error) {
          storage = false;
        }
      }
      sync();
    });
    if (device && typeof device.addEventListener === "function") {
      device.addEventListener("change", sync);
    }
    sync();
  }

  if (DOCUMENT.readyState === "loading") {
    DOCUMENT.addEventListener("DOMContentLoaded", connect);
  } else {
    connect();
  }
})(typeof window !== "undefined" ? window : this);
