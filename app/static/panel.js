// Restaurant Bot – Chatwoot Dashboard App panel.
// Context arrives via window "message" events from the Chatwoot dashboard:
//   event.data is a JSON string: {"event":"appContext","data":{"conversation":{...},"contact":{...},"currentAgent":{...}}}
// and can be requested with parent.postMessage('chatwoot-dashboard-app:fetch-info', origin).
// The parent's data is used only to know WHICH conversation is open; the server re-verifies
// access with the agent's own Chatwoot token. currentAgent is not trusted for identity.
(function () {
  "use strict";
  var root = document.getElementById("panel-root");
  var panel = document.getElementById("panel");
  var CW_ORIGIN = root.dataset.chatwootOrigin;
  var store = (function () {
    try { window.sessionStorage.setItem("rb_test", "1"); window.sessionStorage.removeItem("rb_test"); return window.sessionStorage; }
    catch (e) { var mem = {}; return { getItem: function (k) { return mem[k] || null; }, setItem: function (k, v) { mem[k] = String(v); }, removeItem: function (k) { delete mem[k]; } }; }
  })();
  var state = { token: store.getItem("rb_panel_token"), csrf: store.getItem("rb_panel_csrf"), conversationId: null, link: null };

  function saveAuth(token, csrf) {
    state.token = token; state.csrf = csrf;
    if (token) { store.setItem("rb_panel_token", token); store.setItem("rb_panel_csrf", csrf); }
    else { store.removeItem("rb_panel_token"); store.removeItem("rb_panel_csrf"); }
  }

  function api(path, body) {
    var headers = { "Content-Type": "application/json" };
    if (state.token) { headers["Authorization"] = "Bearer " + state.token; headers["X-CSRF-Token"] = state.csrf; }
    return fetch(path, { method: "POST", headers: headers, body: JSON.stringify(body || {}), credentials: "omit" });
  }

  // ------------------------------------------------------------ context
  window.addEventListener("message", function (event) {
    if (event.origin !== CW_ORIGIN) return; // ignore every other origin
    var data = event.data;
    if (typeof data === "string") { try { data = JSON.parse(data); } catch (e) { return; } }
    if (!data || data.event !== "appContext" || !data.data) return;
    var conv = data.data.conversation || {};
    var id = parseInt(conv.id, 10);
    if (!Number.isInteger(id) || id <= 0) return;
    if (id !== state.conversationId) { state.conversationId = id; loadView(); }
  });

  function requestContext() {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage("chatwoot-dashboard-app:fetch-info", CW_ORIGIN);
    }
  }

  // ------------------------------------------------------------ views
  function showLogin(message) {
    var tpl = document.getElementById("tpl-login").content.cloneNode(true);
    panel.innerHTML = "";
    panel.appendChild(tpl);
    if (message) panel.querySelector('[data-slot="status"]').textContent = message;
    startLink();
  }

  function startLink() {
    api("/app-panel/api/link/start").then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); }).then(function (res) {
      if (!res.ok) { setStatus(res.j.error || "Could not start login."); return; }
      state.link = res.j;
      var codeEl = panel.querySelector('[data-slot="code"]');
      var linkEl = panel.querySelector('[data-slot="login-link"]');
      if (codeEl) codeEl.textContent = res.j.code;
      if (linkEl) linkEl.setAttribute("href", res.j.login_url);
    }).catch(function () { setStatus("Network error."); });
  }

  function setStatus(text) {
    var el = panel.querySelector('[data-slot="status"]');
    if (el) el.textContent = text;
  }

  function claim() {
    if (!state.link) { startLink(); return; }
    fetch("/app-panel/api/link/claim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nonce: state.link.nonce }), credentials: "omit" })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.status === 200 && res.j.token) { saveAuth(res.j.token, res.j.csrf); state.link = null; loadView(); }
        else if (res.status === 202) { setStatus("Not confirmed yet. Finish logging in and click “Confirm and connect” in the other tab."); }
        else { setStatus("That login link expired. A new code has been created; click Log in again."); startLink(); }
      }).catch(function () { setStatus("Network error."); });
  }

  function loadView() {
    if (!state.token) { showLogin(); return; }
    if (!state.conversationId) { panel.innerHTML = '<p class="text-slate-500">Open a conversation in Chatwoot to see bot details.</p>'; requestContext(); return; }
    htmx.ajax("GET", "/app-panel/view?conversation_id=" + encodeURIComponent(state.conversationId), { target: "#panel", swap: "innerHTML" });
  }

  // ------------------------------------------------------------ htmx auth headers
  document.body.addEventListener("htmx:configRequest", function (evt) {
    if (state.token) {
      evt.detail.headers["Authorization"] = "Bearer " + state.token;
      evt.detail.headers["X-CSRF-Token"] = state.csrf;
    }
  });
  document.body.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail.xhr.status;
    if (status === 401) { saveAuth(null, null); showLogin("Your panel session expired. Please log in again."); }
    else if (status === 403) { panel.insertAdjacentHTML("afterbegin", '<p class="text-xs bg-red-50 text-red-700 p-2 rounded">Request refused (403). Reload the panel.</p>'); }
  });

  // ------------------------------------------------------------ actions (event delegation, no inline JS)
  function translate(text, lang, done) {
    api("/copilot/translate", { text: text, target: lang, conversation_id: state.conversationId })
      .then(function (r) { if (r.status === 401) { saveAuth(null, null); showLogin(); return null; } return r.json(); })
      .then(function (j) { if (j) done(j.translation || "", j.error); });
  }

  function copyText(el) {
    var text = el.value;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).catch(function () { el.select(); document.execCommand("copy"); });
    } else { el.select(); document.execCommand("copy"); }
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-action]");
    if (!btn) return;
    var action = btn.dataset.action;
    if (action === "claim") { claim(); }
    else if (action === "reload") { loadView(); }
    else if (action === "logout") { api("/app-panel/api/logout").finally(function () { saveAuth(null, null); showLogin(); }); }
    else if (action === "insert") {
      var d = document.getElementById("draft");
      if (d) { d.value = d.value ? d.value + "\n" + btn.dataset.text : btn.dataset.text; d.focus(); }
    }
    else if (action === "copy") { var t = document.querySelector(btn.dataset.target); if (t) copyText(t); btn.textContent = "Copied"; }
    else if (action === "translate-draft") {
      var draft = document.getElementById("draft");
      if (!draft || !draft.value.trim()) return;
      btn.disabled = true;
      translate(draft.value, btn.dataset.lang, function (out, err) { btn.disabled = false; if (out) draft.value = out; else if (err) alert(err); });
    }
    else if (action === "translate-last") {
      var msgs = document.querySelectorAll("[data-last-incoming]");
      var out = document.getElementById("translation-out");
      if (!msgs.length || !out) return;
      var text = msgs[msgs.length - 1].textContent.replace(/^\s*Customer:\s*/, "");
      out.classList.remove("hidden"); out.textContent = "Translating…";
      translate(text, btn.dataset.lang, function (res, err) { out.textContent = res || err || "No translation."; });
    }
  });

  loadView();
  requestContext();
})();
