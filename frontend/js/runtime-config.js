/* js/runtime-config.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — authoritative public runtime configuration.

   Contains ONLY public origin settings (never API keys or private values).
   In production on Cloudflare static hosting (jonehotels.com), browser calls
   go directly to https://api.jonehotels.com over HTTPS. On local development
   hosts (localhost / 127.0.0.1), this stays empty so js/config.js resolves
   requests to its local development backend.

   Served with Cache-Control: no-store and bypassed by sw.js so configuration
   updates take effect immediately without cache staleness.
   ========================================================================== */
(function (window) {
  "use strict";

  var existing = window.__APP_CONFIG__;
  if (!existing || typeof existing !== "object" || Array.isArray(existing)) {
    existing = {};
  }

  var hostname = (window.location && window.location.hostname) || "";
  var isLocalHost =
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "[::1]" ||
    hostname === "";

  window.__APP_CONFIG__ = Object.assign(
    {
      API_BASE_URL: isLocalHost ? "" : "https://api.jonehotels.com",
    },
    existing
  );
})(window);
