// Admin site helpers. No inline scripts (CSP), so behaviour is attached by data attributes.
(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
  document.addEventListener("focusin", function (e) {
    if (e.target.hasAttribute && e.target.hasAttribute("data-select-on-focus")) e.target.select();
  });
})();
