// Company page: "Copy" next to a new invitation link. Without this script the link can be selected by hand.
(function () {
  var input = document.getElementById("colink");
  var button = document.getElementById("cocopy");
  if (!input || !button || !navigator.clipboard) return;
  button.hidden = false;
  input.addEventListener("focus", function () { input.select(); });
  button.addEventListener("click", function () {
    navigator.clipboard.writeText(input.value).then(function () {
      button.textContent = button.getAttribute("data-done");
    });
  });
})();
