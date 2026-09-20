"use strict";

(function () {
  var form = document.getElementById("loginForm");
  var input = document.getElementById("password");
  var button = document.getElementById("loginButton");
  var error = document.getElementById("loginError");

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    error.hidden = true;
    if (!input.value) {
      showError("Wpisz hasło.");
      input.focus();
      return;
    }
    button.disabled = true;
    button.textContent = "Logowanie…";
    try {
      var response = await fetch("/dash/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: input.value }),
      });
      if (response.ok) {
        location.replace("/dash" + location.hash);
        return;
      }
      var body = await response.json().catch(function () { return {}; });
      showError(body.detail || "Logowanie nie powiodło się (" + response.status + ").");
      input.select();
    } catch (err) {
      showError("Brak połączenia z serwerem. Sprawdź sieć (np. Tailscale) i spróbuj ponownie.");
    } finally {
      button.disabled = false;
      button.textContent = "Zaloguj";
    }
  });
})();
