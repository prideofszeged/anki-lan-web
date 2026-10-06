(function () {
  "use strict";

  var lastFocus = null;

  function nightOn() {
    if (location.hash.includes("night")) return true;
    var saved = localStorage.getItem("ankiweb-night");
    if (saved !== null) return saved === "1";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function applyNight(enabled) {
    document.documentElement.classList.toggle("night-mode", enabled);
    document.documentElement.style.colorScheme = enabled ? "dark" : "light";
    localStorage.setItem("ankiweb-night", enabled ? "1" : "0");
    var hash = location.hash.replace(/#?night/g, "");
    if (enabled) hash = "#night";
    history.replaceState(null, "", location.pathname + location.search + hash);
  }

  function toggleMore(force) {
    var sheet = document.getElementById("ankiweb-more-sheet");
    var button = document.getElementById("ankiweb-more-btn");
    if (!sheet || !button) return;
    var open = typeof force === "boolean" ? force : sheet.hidden;
    sheet.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.classList.toggle("ankiweb-sheet-open", open);
    if (open) {
      lastFocus = document.activeElement;
      var close = sheet.querySelector(".close");
      if (close) close.focus();
    } else if (lastFocus && typeof lastFocus.focus === "function") {
      lastFocus.focus();
    }
  }

  function bindNavigation() {
    if (!document.getElementById("ankiweb-bottomnav")) {
      var back = document.createElement("a");
      back.id = "ankiweb-spa-back";
      back.href = nightOn() ? "/deckbrowser#night" : "/deckbrowser";
      back.setAttribute("aria-label", "Back to Decks");
      back.textContent = "← Decks";
      document.body.appendChild(back);
      return;
    }
    var more = document.getElementById("ankiweb-more-btn");
    var sheet = document.getElementById("ankiweb-more-sheet");
    if (more) more.addEventListener("click", function (event) {
      event.preventDefault();
      toggleMore();
    });
    if (sheet) {
      var backdrop = sheet.querySelector(".backdrop");
      var close = sheet.querySelector(".close");
      if (backdrop) backdrop.addEventListener("click", function () { toggleMore(false); });
      if (close) close.addEventListener("click", function () { toggleMore(false); });
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && sheet && !sheet.hidden) toggleMore(false);
    });

    document.querySelectorAll("#ankiweb-toolbar .nm, #ankiweb-more-sheet button[title='Toggle night mode']")
      .forEach(function (button) {
        button.addEventListener("click", function (event) {
          event.preventDefault();
          applyNight(!nightOn());
        });
      });
  }

  window.ankiwebToggleMore = toggleMore;
  window.ankiwebToggleNight = function () { applyNight(!nightOn()); };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindNavigation, { once: true });
  } else {
    bindNavigation();
  }
  document.documentElement.classList.toggle("night-mode", nightOn());
})();
