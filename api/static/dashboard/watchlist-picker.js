"use strict";

document.querySelectorAll("form[data-catalog-refresh]").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type='submit']");
    if (!(button instanceof HTMLButtonElement)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Loading catalog…";
  });
});
