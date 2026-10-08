/* js/portal.js
   Verified-email guest portal transport. Portal credentials are opaque,
   short-lived X-Portal-Session values and are intentionally kept only in the
   current page's JavaScript memory. A consumed link moves the token through a
   URL fragment once, then immediately removes it from browser history. */
(function () {
  "use strict";

  var J = window.JONE = window.JONE || {};
  var portal = J.portal = J.portal || {};

  function endpoint(name) {
    var endpoints = window.APP_CONFIG && window.APP_CONFIG.API_ENDPOINTS;
    var value = endpoints && endpoints[name];
    if (!value) throw new Error("Guest portal service is not configured. Please contact the hotel.");
    return value;
  }

  function pathFor(name, id, action) {
    var base = endpoint(name).replace(/\/$/, "");
    if (id == null || id === "") return base + "/";
    var path = base + "/" + encodeURIComponent(String(id));
    if (action) {
      String(action).split("/").filter(Boolean).forEach(function (segment) {
        path += "/" + encodeURIComponent(segment);
      });
    }
    return path + "/";
  }

  function callPath(path, opts) {
    opts = opts || {};
    if (!window.API || typeof window.API.request !== "function") {
      return Promise.reject(new Error("Guest portal service is unavailable. Refresh and try again."));
    }
    return window.API.request(path, Object.assign({ auth: false }, opts));
  }

  function publicRequest(name, method, body, opts) {
    opts = opts || {};
    return callPath(endpoint(name), Object.assign({}, opts, { method: method || "GET", body: body }));
  }

  function sessionRequest(name, sessionToken, method, body, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {}, { "X-Portal-Session": sessionToken || "" });
    return callPath(endpoint(name), Object.assign({}, opts, { method: method || "GET", body: body, headers: headers }));
  }

  function sessionPathRequest(path, sessionToken, method, body, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {}, { "X-Portal-Session": sessionToken || "" });
    return callPath(path, Object.assign({}, opts, { method: method || "GET", body: body, headers: headers }));
  }

  function newIdempotencyKey(prefix) {
    var suffix = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    return (prefix || "portal") + "-ui-" + suffix;
  }

  function consumeSessionFromHash() {
    var raw = String(window.location.hash || "").replace(/^#/, "");
    if (!raw) return "";
    var token = "";
    try { token = new URLSearchParams(raw).get("session") || ""; } catch (_) {}
    // A fragment is not sent to the server, but removing it immediately keeps
    // it out of copied URLs and reduces accidental local exposure.
    try { window.history.replaceState(null, "", window.location.pathname + window.location.search); } catch (_) {}
    return token;
  }

  portal.endpoint = endpoint;
  portal.pathFor = pathFor;
  portal.publicRequest = publicRequest;
  portal.sessionRequest = sessionRequest;
  portal.sessionPathRequest = sessionPathRequest;
  portal.requestAccess = function (email) { return publicRequest("portalAccessRequest", "POST", { email: email }); };
  portal.consumeAccess = function (token) { return publicRequest("portalAccessConsume", "POST", { token: token }); };
  portal.logout = function (sessionToken) { return sessionRequest("portalLogout", sessionToken, "POST", {}); };
  portal.sessionFromHash = consumeSessionFromHash;
  portal.sessionHash = function (sessionToken) { return "#session=" + encodeURIComponent(sessionToken || ""); };
  portal.newIdempotencyKey = newIdempotencyKey;
})();
