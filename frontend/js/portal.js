/* js/portal.js
   Verified-email guest portal transport. The portal session is an HttpOnly,
   SameSite=Lax cookie set by the API. Page scripts never read or store it, so
   there is no session token in the URL, in memory, or in web storage. A refresh
   or return visit re-validates the cookie with the server (GET /api/portal/me/).
   Every portal call sends X-Portal-Request so the server can refuse
   cookie-authenticated writes that did not come from this page (CSRF). */
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

  // The session travels in the HttpOnly cookie. The session argument is accepted
  // for call-site compatibility and is deliberately not sent anywhere.
  function portalHeaders(extra) {
    return Object.assign({}, extra || {}, { "X-Portal-Request": "1" });
  }

  function sessionRequest(name, _sessionIgnored, method, body, opts) {
    opts = opts || {};
    return callPath(endpoint(name), Object.assign({}, opts, {
      method: method || "GET", body: body, headers: portalHeaders(opts.headers),
    }));
  }

  function sessionPathRequest(path, _sessionIgnored, method, body, opts) {
    opts = opts || {};
    return callPath(path, Object.assign({}, opts, {
      method: method || "GET", body: body, headers: portalHeaders(opts.headers),
    }));
  }

  function newIdempotencyKey(prefix) {
    var suffix = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    return (prefix || "portal") + "-ui-" + suffix;
  }

  // Shown only when the server says the stay has ended at checkout. Any other
  // 401 simply means "sign in again", so it returns an empty message.
  function sessionEndedMessage(err) {
    if (err && err.code === "PORTAL_ACCESS_ENDED") {
      return err.message || "Portal access ended because checkout is complete.";
    }
    return "";
  }

  portal.endpoint = endpoint;
  portal.pathFor = pathFor;
  portal.publicRequest = publicRequest;
  portal.sessionRequest = sessionRequest;
  portal.sessionPathRequest = sessionPathRequest;
  portal.requestAccess = function (email) { return publicRequest("portalAccessRequest", "POST", { email: email }); };
  portal.consumeAccess = function (token) { return publicRequest("portalAccessConsume", "POST", { token: token }); };
  portal.logout = function (sessionToken) { return sessionRequest("portalLogout", sessionToken, "POST", {}); };
  portal.sessionEndedMessage = sessionEndedMessage;
  portal.newIdempotencyKey = newIdempotencyKey;
})();
