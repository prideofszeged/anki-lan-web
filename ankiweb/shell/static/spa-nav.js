(function() {
  function isNight() {
    return location.hash.includes("night") || localStorage.getItem("ankiweb-night") === "1";
  }

  function updateNight() {
    var a = document.getElementById("ankiweb-spa-back");
    if (!a) return;
    if (isNight()) {
      a.classList.add("night");
      if (!a.href.includes("#night")) a.href = "/deckbrowser#night";
    } else {
      a.classList.remove("night");
      a.href = "/deckbrowser";
    }
  }

  function createAffordance() {
    if (document.getElementById("ankiweb-spa-back")) return;
    var a = document.createElement("a");
    a.id = "ankiweb-spa-back";
    a.className = "ankiweb-spa-back";
    a.href = isNight() ? "/deckbrowser#night" : "/deckbrowser";
    a.setAttribute("aria-label", "Back to Decks");
    a.textContent = "← Decks";
    if (isNight()) {
      a.classList.add("night");
    }
    a.addEventListener("click", function(e) {
      if (isNight() && !a.href.includes("#night")) {
        a.href = "/deckbrowser#night";
      }
    });
    if (document.body) {
      document.body.appendChild(a);
    } else {
      document.addEventListener("DOMContentLoaded", function() {
        document.body.appendChild(a);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", createAffordance);
  } else {
    createAffordance();
  }

  window.addEventListener("hashchange", updateNight);
})();
