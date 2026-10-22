// J-ONE Hotel & Lodge — Cloudflare Worker in front of the static frontend.
//
// 1. /api/* and /media/* are proxied to the Django backend on Northflank so the
//    browser only ever talks to jonehotels.com (same origin: no CORS, and the
//    guest-portal HttpOnly cookie works).
// 2. "/", "/dashboard/" and "/portal/" are mapped to their index.html (the site
//    uses explicit .html URLs, so html_handling is "none").
//
// Everything else is served straight from the static assets.

const API_ORIGIN = "https://api.jonehotels.com";

async function proxyToBackend(request, url) {
  const target = new URL(url.pathname + url.search, API_ORIGIN);
  const headers = new Headers(request.headers);

  // Pass the real visitor address to Django. It reads true-client-ip first
  // (see backend/apps/core/client_ip.py), which the front-desk IP rule needs.
  const ip = request.headers.get("cf-connecting-ip");
  headers.delete("host");
  if (ip) {
    headers.set("true-client-ip", ip);
    headers.set("x-real-ip", ip);
    headers.set("x-forwarded-for", ip);
  }
  headers.set("x-forwarded-proto", "https");

  const init = { method: request.method, headers, redirect: "manual" };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
  }

  try {
    return await fetch(target, init);
  } catch (err) {
    return new Response(
      JSON.stringify({ success: false, message: "API temporarily unavailable." }),
      { status: 502, headers: { "content-type": "application/json" } }
    );
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path.startsWith("/api/") || path.startsWith("/media/")) {
      return proxyToBackend(request, url);
    }

    if ((request.method === "GET" || request.method === "HEAD") && path.endsWith("/")) {
      const indexUrl = new URL(url);
      indexUrl.pathname = path + "index.html";
      return env.ASSETS.fetch(new Request(indexUrl, request));
    }

    return env.ASSETS.fetch(request);
  },
};
