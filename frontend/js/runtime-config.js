/* js/runtime-config.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — deployment-owned public runtime configuration.

   This deliberately contains no host-specific value, credential, payment key,
   or private setting. By default the browser uses the same origin for /api/.
   A deployment that hosts the static frontend separately may replace this
   served file (or set window.__APP_CONFIG__ earlier in the document) with
   public values such as:

       window.__APP_CONFIG__ = { API_BASE_URL: "https://api.example.com" };

   Keep a trailing slash out of API_BASE_URL. config.js normalizes it and
   merges supported overrides with the application's safe defaults.

   This file is intentionally network-only in sw.js: environment changes must
   take effect without a service-worker cache race. It is not a secret store.
   ========================================================================== */
(function (window) {
  "use strict";

  var existing = window.__APP_CONFIG__;
  if (!existing || typeof existing !== "object" || Array.isArray(existing)) {
    existing = {};
  }

  window.__APP_CONFIG__ = Object.assign(
    {
      // Empty means same-origin API requests: /api/... . This is the portable
      // default for reverse-proxy, single-host, and local dev_server setups.
      API_BASE_URL: "",
    },
    existing
  );
})(window);
