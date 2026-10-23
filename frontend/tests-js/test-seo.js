/* tests-js/test-seo.js */
/* Search-engine configuration contract for the public site.
   Run from /frontend: node --test tests-js/test-seo.js

   These tests exist because every defect they cover was live in production:
   no canonical URLs at all, a sitemap listing redirecting .html URLs, a
   robots.txt that disallowed URLs the site no longer serves, and two pages
   describing the hotel as being in the wrong city. They fail the build/CI
   rather than waiting for an indexing problem to be noticed weeks later.

   Nothing here asserts a ranking. It asserts that the site tells search
   engines the truth about itself. */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { FRONTEND } = require("./harness");

const ORIGIN = "https://jonehotels.com";

const read = (rel) => fs.readFileSync(path.join(FRONTEND, rel), "utf8");

/* ---------- the public surface, discovered rather than hard-coded -------- */

const ERROR_PAGES = ["403.html", "404.html", "500.html"];

// Pages whose head is owned by the generated SEO block (frontend/seo_build.py).
// Discovered from the markup, then checked against the expected list below so a
// new page cannot be published without SEO, and a page cannot be silently
// dropped from the layer either.
const ALL_ROOT_PAGES = fs.readdirSync(FRONTEND).filter((f) => f.endsWith(".html")).sort();
const PUBLIC_PAGES = ALL_ROOT_PAGES.filter((f) => read(f).includes("<!-- SEO:BEGIN"));
const UNMANAGED_PAGES = ALL_ROOT_PAGES.filter((f) => !PUBLIC_PAGES.includes(f));

const EXPECTED_MANAGED = [
  "about.html", "booking-confirmation.html", "booking-review.html", "booking.html",
  "cancellation-policy.html", "cancellation-result.html", "contact.html",
  "facilities.html", "gallery.html", "index.html", "login.html", "my-bookings.html",
  "offers.html", "offline.html", "payment-verify.html", "policies.html",
  "privacy.html", "refund-policy.html", "register.html", "review.html",
  "room-details.html", "rooms.html", "terms.html",
];

// Pages that must be indexable (the site's landing pages + legal pages).
const INDEXABLE_PAGES = [
  "index.html",
  "rooms.html",
  "facilities.html",
  "gallery.html",
  "offers.html",
  "about.html",
  "contact.html",
  "policies.html",
  "privacy.html",
  "terms.html",
  "cancellation-policy.html",
  "refund-policy.html",
  "review.html",
];

// Pages that must never be indexed.
const PRIVATE_PAGES = [
  "booking.html",
  "booking-review.html",
  "booking-confirmation.html",
  "cancellation-result.html",
  "payment-verify.html",
  "my-bookings.html",
  "login.html",
  "register.html",
  "room-details.html",
];

function head(rel) {
  const html = read(rel);
  const start = html.indexOf("<head>");
  const end = html.indexOf("</head>");
  assert.ok(start !== -1 && end !== -1, `${rel} has no <head>`);
  return html.slice(start, end);
}

function one(html, re, what, rel) {
  const matches = [...html.matchAll(re)];
  assert.equal(matches.length, 1, `${rel} must contain exactly one ${what} (found ${matches.length})`);
  return matches[0];
}

function meta(pg, name) {
  const m = head(pg).match(new RegExp(`<meta\\s+name="${name}"\\s+content="([^"]*)"`, "i"));
  return m ? m[1] : null;
}

function og(pg, prop) {
  const m = head(pg).match(new RegExp(`<meta\\s+property="${prop}"\\s+content="([^"]*)"`, "i"));
  return m ? m[1] : null;
}

function canonical(pg) {
  const m = head(pg).match(/<link\s+rel="canonical"\s+href="([^"]*)"/i);
  return m ? m[1] : null;
}

function jsonLd(pg) {
  const blocks = [...read(pg).matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)];
  return blocks.map((b) => JSON.parse(b[1].replace(/<\\\//g, "</")));
}

// Canonical URL -> site-root-relative file that must serve it.
function fileForCanonical(url) {
  const pathname = new URL(url).pathname;
  if (pathname === "/") return "index.html";
  return pathname.replace(/^\//, "") + ".html";
}

/* ------------------------------- sitemap -------------------------------- */

test("sitemap.xml is valid XML on the canonical HTTPS origin", () => {
  const xml = read("sitemap.xml");
  assert.match(xml, /^<\?xml version="1\.0" encoding="UTF-8"\?>/);
  assert.match(xml, /<urlset xmlns="http:\/\/www\.sitemaps\.org\/schemas\/sitemap\/0\.9">/);
  assert.match(xml, /<\/urlset>\s*$/);
  const locs = [...xml.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);
  assert.ok(locs.length >= 10, `expected the public pages in the sitemap, found ${locs.length}`);
  for (const loc of locs) {
    assert.ok(loc.startsWith(ORIGIN + "/"), `sitemap URL is not on ${ORIGIN}: ${loc}`);
    assert.ok(new URL(loc).protocol === "https:", `sitemap URL is not HTTPS: ${loc}`);
    assert.ok(!loc.includes("www."), `sitemap URL uses the www host: ${loc}`);
    assert.ok(!loc.endsWith(".html"), `sitemap URL is a redirecting .html URL: ${loc}`);
    assert.ok(!loc.includes("?"), `sitemap URL carries a query string: ${loc}`);
    assert.ok(!loc.endsWith("/") || loc === ORIGIN + "/", `sitemap URL has a trailing slash variant: ${loc}`);
  }
  assert.equal(locs.length, new Set(locs).size, "sitemap contains duplicate <loc> entries");
  assert.ok(locs.includes(ORIGIN + "/"), "the homepage must be in the sitemap");
});

test("every sitemap URL resolves to a real, indexable page", () => {
  const locs = [...read("sitemap.xml").matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);
  for (const loc of locs) {
    const file = fileForCanonical(loc);
    assert.ok(fs.existsSync(path.join(FRONTEND, file)), `sitemap URL ${loc} has no page (${file})`);
    const directives = (meta(file, "robots") || "").toLowerCase();
    assert.ok(!directives.includes("noindex"), `sitemap lists a noindex page: ${file}`);
    assert.equal(canonical(file), loc, `${file} canonical (${canonical(file)}) disagrees with the sitemap (${loc})`);
  }
});

test("sitemap excludes private, transactional and error pages", () => {
  const xml = read("sitemap.xml");
  for (const page of [...PRIVATE_PAGES, ...ERROR_PAGES]) {
    const canonicalUrl = canonical(page);
    if (canonicalUrl) {
      assert.ok(!xml.includes(canonicalUrl), `sitemap lists a private page: ${page}`);
    }
    const slug = "/" + page.replace(/\.html$/, "");
    if (page !== "index.html") {
      assert.ok(!xml.includes(`<loc>${ORIGIN}${slug}</loc>`), `sitemap lists ${slug}`);
    }
  }
  for (const banned of ["/dashboard", "/portal", "/qr-service", "/api/", "/media/"]) {
    assert.ok(!xml.includes(banned), `sitemap must not list ${banned}`);
  }
});

test("sitemap and robots contain no personal data or tokens", () => {
  for (const file of ["sitemap.xml", "robots.txt"]) {
    const text = read(file);
    assert.doesNotMatch(text, /J1-\d{8}-[A-Z0-9]+/i, `${file} contains a booking reference`);
    assert.doesNotMatch(text, /(token|session|access_key|secret)=/i, `${file} contains a token parameter`);
    assert.doesNotMatch(text, /[\w.+-]+@[\w-]+\.[\w.]+/g, `${file} contains what looks like an email address`);
  }
});

/* -------------------------------- robots -------------------------------- */

test("robots.txt allows public content and declares the sitemap", () => {
  const robots = read("robots.txt");
  assert.match(robots, /User-agent: \*/);
  assert.match(robots, /^Allow: \/$/m);
  assert.ok(
    robots.includes(`Sitemap: ${ORIGIN}/sitemap.xml`),
    "robots.txt must declare the absolute sitemap URL"
  );
  assert.doesNotMatch(robots, /^Disallow: \/$/m, "robots.txt must not disallow the whole site");
  assert.doesNotMatch(robots, /^Disallow: \*$/m);
  for (const asset of ["/css", "/js", "/assets", "/favicon"]) {
    assert.doesNotMatch(robots, new RegExp(`^Disallow: ${asset}`, "m"), `robots.txt blocks ${asset}`);
  }
});

test("robots.txt keeps private areas out but never blocks the public pages", () => {
  const robots = read("robots.txt");
  const disallow = [...robots.matchAll(/^Disallow: (\S+)/gm)].map((m) => m[1]);
  for (const path_ of ["/dashboard", "/portal", "/qr-service"]) {
    assert.ok(disallow.includes(path_), `robots.txt must disallow ${path_}`);
  }
  for (const page of INDEXABLE_PAGES) {
    const url = new URL(canonical(page)).pathname;
    for (const rule of disallow) {
      assert.ok(
        !(url === rule || url.startsWith(rule.replace(/\/$/, "") + "/")),
        `robots.txt blocks the public page ${url} via "Disallow: ${rule}"`
      );
    }
  }
});

/* ------------------------------- metadata ------------------------------- */

test("every public page has one unique, descriptive title and description", () => {
  const titles = new Map();
  const descriptions = new Map();
  for (const page of PUBLIC_PAGES) {
    const h = head(page);
    const title = one(h, /<title>([\s\S]*?)<\/title>/g, "<title>", page)[1].trim();
    assert.ok(title.length > 0, `${page} has an empty title`);
    assert.ok(title.length <= 70, `${page} title is ${title.length} characters (keep <= 70)`);
    assert.ok(!titles.has(title), `${page} reuses the title from ${titles.get(title)}`);
    titles.set(title, page);

    const description = meta(page, "description");
    assert.ok(description && description.trim(), `${page} has no meta description`);
    assert.ok(!descriptions.has(description), `${page} reuses the description from ${descriptions.get(description)}`);
    descriptions.set(description, page);
  }
  for (const page of INDEXABLE_PAGES) {
    const description = meta(page, "description");
    assert.ok(
      description.length >= 50 && description.length <= 165,
      `${page} description is ${description.length} characters (want 50–165)`
    );
  }
});

test("every public page has a correct, self-referencing canonical URL", () => {
  for (const page of PUBLIC_PAGES) {
    const url = canonical(page);
    assert.ok(url, `${page} has no canonical URL`);
    assert.ok(url.startsWith(ORIGIN + "/"), `${page} canonical is off-origin: ${url}`);
    assert.ok(!url.includes("www."), `${page} canonical uses the www host: ${url}`);
    assert.ok(!url.endsWith(".html"), `${page} canonical points at a redirecting .html URL: ${url}`);
    assert.ok(!url.includes("?"), `${page} canonical carries a query string: ${url}`);
    assert.ok(!url.endsWith("/") || url === ORIGIN + "/", `${page} canonical is a trailing-slash variant: ${url}`);
    assert.equal(og(page, "og:url"), url, `${page} og:url disagrees with its canonical`);
    one(head(page), /rel="canonical"/g, "canonical link", page);
  }
});

test("indexable pages are indexable and private pages are not", () => {
  for (const page of INDEXABLE_PAGES) {
    const directives = (meta(page, "robots") || "").toLowerCase();
    assert.ok(directives.includes("index"), `${page} is not marked indexable`);
    assert.ok(!directives.includes("noindex"), `${page} is accidentally noindex`);
  }
  for (const page of PRIVATE_PAGES) {
    const directives = (meta(page, "robots") || "").toLowerCase();
    assert.ok(directives.includes("noindex"), `${page} must be marked noindex`);
  }
  const homepage = (meta("index.html", "robots") || "").toLowerCase();
  assert.ok(homepage.includes("index") && !homepage.includes("noindex"), "the homepage must be indexable");
});

test("social sharing metadata uses absolute, existing URLs", () => {
  for (const page of PUBLIC_PAGES) {
    const h = head(page);
    for (const prop of ["og:type", "og:title", "og:description", "og:url", "og:image", "og:site_name"]) {
      assert.ok(og(page, prop), `${page} is missing ${prop}`);
    }
    assert.ok(/name="twitter:card"\s+content="summary_large_image"/.test(h), `${page} is missing twitter:card`);
    assert.ok(/name="twitter:title"/.test(h), `${page} is missing twitter:title`);
    assert.ok(/name="twitter:image"/.test(h), `${page} is missing twitter:image`);

    const image = og(page, "og:image");
    assert.ok(image.startsWith(ORIGIN + "/"), `${page} og:image is not absolute: ${image}`);
    const onDisk = path.join(FRONTEND, new URL(image).pathname.replace(/^\//, ""));
    assert.ok(fs.existsSync(onDisk), `${page} og:image does not exist: ${image}`);
    const bytes = fs.statSync(onDisk).size;
    assert.ok(bytes > 20 * 1024, `${page} og:image is suspiciously small (${bytes} bytes)`);
    assert.ok(og(page, "og:image:width") && og(page, "og:image:height"), `${page} og:image lacks dimensions`);
    assert.equal(og(page, "og:image:url") || image, image);
  }
});

/* ---------------------------- structured data --------------------------- */

test("structured data parses, is schema.org, and carries no invented ratings", () => {
  for (const page of PUBLIC_PAGES) {
    const blocks = jsonLd(page);
    assert.equal(blocks.length, 1, `${page} must have exactly one JSON-LD block`);
    assert.equal(blocks[0]["@context"], "https://schema.org", `${page} JSON-LD @context is wrong`);
    const graph = blocks[0]["@graph"] || [blocks[0]];
    assert.ok(graph.length > 0, `${page} JSON-LD graph is empty`);
    const serialised = JSON.stringify(blocks[0]);
    assert.doesNotMatch(serialised, /aggregateRating/, `${page} publishes a fabricated aggregate rating`);
    assert.doesNotMatch(serialised, /"review"\s*:/, `${page} publishes review markup the hotel cannot verify`);
    assert.doesNotMatch(serialised, /"priceRange"/, `${page} publishes an unverified price range`);
    assert.doesNotMatch(serialised, /"latitude"|"longitude"|"geo"/, `${page} publishes unverified coordinates`);
    // No private data can ride along in structured data.
    assert.doesNotMatch(serialised, /J1-\d{8}-/i, `${page} JSON-LD contains a booking reference`);
    assert.doesNotMatch(serialised, /api\.jonehotels\.com/, `${page} JSON-LD exposes the API origin`);
  }
});

test("the hotel entity states the verified identity on every page that declares it", () => {
  let hotelPages = 0;
  for (const page of PUBLIC_PAGES) {
    for (const block of jsonLd(page)) {
      for (const node of block["@graph"] || [block]) {
        if (node["@type"] !== "Hotel") continue;
        hotelPages += 1;
        assert.equal(node.name, "J-ONE Hotel & Lodge", `${page}: Hotel.name`);
        assert.equal(node.telephone, "+2348032112874", `${page}: Hotel.telephone`);
        assert.equal(node.email, "jonathanonu76@gmail.com", `${page}: Hotel.email`);
        assert.equal(node.url, ORIGIN + "/", `${page}: Hotel.url`);
        assert.equal(node.address.addressLocality, "Enugu", `${page}: Hotel.address locality`);
        assert.equal(node.address.addressRegion, "Enugu State", `${page}: Hotel.address region`);
        assert.equal(node.address.addressCountry, "NG", `${page}: Hotel.address country`);
        assert.match(node.address.streetAddress, /Mgbowo Street/, `${page}: Hotel.address street`);
        assert.ok(node.logo.url.startsWith(ORIGIN + "/"), `${page}: Hotel.logo must be absolute`);
        for (const image of node.image) {
          assert.ok(image.startsWith(ORIGIN + "/"), `${page}: Hotel.image must be absolute: ${image}`);
          const onDisk = path.join(FRONTEND, new URL(image).pathname.replace(/^\//, ""));
          assert.ok(fs.existsSync(onDisk), `${page}: Hotel.image does not exist: ${image}`);
        }
        for (const feature of node.amenityFeature || []) {
          assert.equal(feature["@type"], "LocationFeatureSpecification", `${page}: amenity type`);
          assert.ok(feature.name && feature.value === true, `${page}: amenity entry is incomplete`);
        }
      }
    }
  }
  assert.ok(hotelPages >= 2, "the Hotel entity must appear on the homepage and the contact page");
});

test("the homepage and contact page declare the site and page graph", () => {
  const types = (page) => jsonLd(page).flatMap((b) => (b["@graph"] || [b]).map((n) => n["@type"]));
  assert.ok(types("index.html").includes("Hotel"));
  assert.ok(types("index.html").includes("WebSite"));
  assert.ok(types("index.html").includes("WebPage"));
  assert.ok(types("contact.html").includes("Hotel"));
  for (const page of ["rooms.html", "facilities.html", "gallery.html", "offers.html", "contact.html"]) {
    assert.ok(types(page).includes("WebPage"), `${page} is missing WebPage markup`);
    assert.ok(types(page).includes("BreadcrumbList"), `${page} is missing BreadcrumbList markup`);
  }
});

test("breadcrumb items point at canonical URLs", () => {
  const known = new Set();
  for (const page of PUBLIC_PAGES) known.add(canonical(page));
  for (const page of PUBLIC_PAGES) {
    for (const block of jsonLd(page)) {
      for (const node of block["@graph"] || [block]) {
        if (node["@type"] !== "BreadcrumbList") continue;
        assert.ok(node.itemListElement.length >= 2, `${page}: breadcrumb has one item`);
        for (const item of node.itemListElement) {
          assert.ok(known.has(item.item), `${page}: breadcrumb points at a non-canonical URL: ${item.item}`);
        }
        assert.equal(node.itemListElement[0].item, ORIGIN + "/", `${page}: breadcrumb does not start at home`);
      }
    }
  }
});

/* --------------------------- content and links --------------------------- */

test("each indexable page has exactly one H1", () => {
  for (const page of INDEXABLE_PAGES) {
    const h1s = [...read(page).matchAll(/<h1[\s>]/g)];
    assert.equal(h1s.length, 1, `${page} has ${h1s.length} <h1> elements`);
  }
  // Transactional pages may ship one <h1> per state panel; each still needs one.
  for (const page of PUBLIC_PAGES) {
    assert.ok(/<h1[\s>]/.test(read(page)), `${page} has no <h1>`);
  }
  assert.match(read("index.html"), /<h1[^>]*>[^<]*J-ONE/, "the homepage H1 must name the hotel");
});

test("the public pages never describe the hotel as being in another city", () => {
  // The homepage and About page both shipped "…, Abuja" while the hotel is in
  // Enugu. Local search treats that as two different businesses.
  for (const page of PUBLIC_PAGES) {
    assert.doesNotMatch(read(page), /Abuja/i, `${page} still describes the hotel as being in Abuja`);
  }
  assert.match(read("index.html"), /Enugu/);
  assert.match(read("contact.html"), /Mgbowo Street/);
});

test("internal links resolve to files that exist", () => {
  for (const page of PUBLIC_PAGES) {
    const html = read(page);
    for (const m of html.matchAll(/(?:href|src)="([^"]+)"/g)) {
      const target = m[1];
      if (/^(https?:|mailto:|tel:|data:|#|javascript:|\/\/)/i.test(target)) continue;
      const withoutQuery = target.split("#")[0].split("?")[0];
      if (!withoutQuery) continue;
      const resolved = withoutQuery.startsWith("/")
        ? path.join(FRONTEND, withoutQuery.replace(/^\//, ""))
        : path.join(FRONTEND, path.dirname(page === "index.html" ? "" : page), withoutQuery);
      if (/\.(html|css|js|png|jpg|jpeg|svg|webmanifest|json|ico)$/i.test(withoutQuery)) {
        assert.ok(fs.existsSync(resolved), `${page} links to a missing file: ${target}`);
      }
    }
  }
});

test("SEO metadata never references internal, staging or obsolete hosts", () => {
  const banned = /(localhost|127\.0\.0\.1|api\.jonehotels\.com|northflank|vercel\.app|onrender|railway\.app|herokuapp)/i;
  for (const page of PUBLIC_PAGES) {
    const h = head(page);
    for (const m of h.matchAll(/content="(https?:\/\/[^"]+)"/g)) {
      assert.doesNotMatch(m[1], banned, `${page} publishes an internal/staging URL in its metadata: ${m[1]}`);
    }
    const url = canonical(page);
    assert.doesNotMatch(url, banned, `${page} canonical is an internal/staging host`);
  }
});

/* ------------------------- private-data boundary ------------------------ */

test("no page ships guest, staff or payment data in its static head", () => {
  const patterns = [
    /J1-\d{8}-[A-Z0-9]{4,}/i,           // booking reference
    /sk_live_[A-Za-z0-9]+/,             // Paystack secret key
    /\bBearer\s+[A-Za-z0-9._-]{20,}/,   // access token
    /password"\s*:\s*"/i,               // credential in inline JSON
  ];
  for (const page of ALL_ROOT_PAGES) {
    const text = head(page);
    for (const re of patterns) {
      assert.doesNotMatch(text, re, `${page} contains private data matching ${re}`);
    }
  }
});

test("the SEO layer covers every page it should, and only those", () => {
  assert.deepEqual(PUBLIC_PAGES.slice().sort(), EXPECTED_MANAGED.slice().sort(),
    "the set of pages with a generated SEO block changed — update seo_build.PAGES and this list together");
  // Everything else served from the root must be noindex and out of the sitemap.
  const sitemap = read("sitemap.xml");
  for (const page of UNMANAGED_PAGES) {
    const directives = meta(page, "robots") || "";
    assert.match(directives, /noindex/, `${page} is outside the SEO layer but is not noindex`);
    assert.ok(!sitemap.includes(page.replace(/\.html$/, "")), `${page} is outside the SEO layer but in the sitemap`);
  }
});

test("the generated SEO head is owned in one place and marked", () => {
  for (const page of PUBLIC_PAGES) {
    const html = read(page);
    assert.equal(
      (html.match(/<!-- SEO:BEGIN/g) || []).length,
      1,
      `${page} is missing the generated SEO block (run python3 build.py)`
    );
    assert.equal((html.match(/<!-- SEO:END -->/g) || []).length, 1, `${page} has an unbalanced SEO block`);
  }
});
