/* Sidebar preference only. This script has no network, trading or storage side effects. */
(function (root) {
  "use strict";

  var KEY = "act.operator.sidebar-collapsed.v1";
  var button = root.document && root.document.getElementById("workspace-nav-toggle");
  var navigation = root.document && root.document.getElementById("workspace-nav");
  if (!button || !navigation) return;

  function apply(collapsed) {
    root.document.documentElement.classList.toggle("nav-collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
    button.textContent = collapsed ? "Expand navigation" : "Collapse navigation";
  }

  function markCurrentModule() {
    var path = root.location.pathname;
    var hash = root.location.hash;
    var active = path === "/operator" && hash === "#health"
      ? "system"
      : path === "/operator"
      ? "overview"
      : path.indexOf("/operator/watchlist") === 0 || path.indexOf("/operator/markets") === 0
      ? "watch-coins"
      : path.indexOf("/operator/history") === 0 || path.indexOf("/operator/soak") === 0
      ? "activity"
      : path.indexOf("/operator/research") === 0
      ? "research"
      : null;
    var links = root.document.querySelectorAll("#workspace-nav a[data-module]");
    for (var index = 0; index < links.length; index += 1) {
      if (links[index].getAttribute("data-module") === active) {
        links[index].setAttribute("aria-current", "page");
      } else {
        links[index].removeAttribute("aria-current");
      }
    }
  }

  var collapsed = false;
  var storageAvailable = false;
  try {
    var stored = root.localStorage.getItem(KEY);
    storageAvailable = true;
    collapsed = stored === "collapsed";
  } catch (_error) {
    collapsed = false;
  }
  apply(collapsed);
  button.hidden = false;
  markCurrentModule();
  root.addEventListener("hashchange", markCurrentModule);

  button.addEventListener("click", function () {
    collapsed = button.getAttribute("aria-expanded") === "true";
    apply(collapsed);
    if (!storageAvailable) return;
    try {
      root.localStorage.setItem(KEY, collapsed ? "collapsed" : "expanded");
    } catch (_error) {
      storageAvailable = false;
      apply(false);
    }
  });
})(typeof window !== "undefined" ? window : this);
