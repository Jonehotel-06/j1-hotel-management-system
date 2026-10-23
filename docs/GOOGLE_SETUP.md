# Owner guide — Google Search Console and Google Business Profile

Written for the hotel owner/manager, not for a developer. Nothing here has been
done automatically: these steps need access to the hotel's own Google account.
The website side is already prepared for all of them.

---

## 1. Verify jonehotels.com in Google Search Console

1. Open <https://search.google.com/search-console> and sign in with the Google
   account that owns the Business Profile (or an account you will keep long-term).
2. Choose **Add property → Domain** and enter `jonehotels.com`
   (Domain properties cover `www`, `http` and every subdomain path in one go).
3. Google shows a **DNS TXT record**. Add it at your DNS provider (this is
   normally the same place `jonehotels.com`'s nameservers are managed — if the
   domain is on Cloudflare DNS, use Cloudflare → DNS → Add record → TXT, name
   `@`, content = the value Google gave you).
4. Come back to Search Console and click **Verify**. DNS can take a few minutes
   to a few hours.
5. Keep that TXT record forever. Removing it un-verifies the property.

*Alternative (URL-prefix property):* if you cannot edit DNS, add a **URL prefix**
property for `https://jonehotels.com/` instead. Google offers several methods;
the **HTML file** and **HTML tag** methods need a developer to add the file/tag
to `frontend/` and redeploy. The DNS method needs nobody but you.

> No verification token has been invented or committed. When the owner has one,
> a developer can add `<meta name="google-site-verification" content="…">` — but
> do **not** hand-edit the page head; add it to `seo_build.py` so the build keeps
> it (see [SEO.md](SEO.md) §7).

---

## 2. Submit the sitemap

1. In Search Console open **Indexing → Sitemaps**.
2. Enter `sitemap.xml` (it resolves to `https://jonehotels.com/sitemap.xml`) and
   submit.
3. Status should become **Success** with 12 discovered URLs. If it says
   "Couldn't fetch", open the URL in a browser first — it must show the XML, not
   the homepage.

The sitemap lists only public, canonical, indexable pages: the homepage, rooms,
facilities, gallery, offers, about, contact, policies, privacy, terms,
cancellation policy and refund policy. It deliberately excludes the booking and
payment flow, staff and guest portals, and the QR guest-request page.

---

## 3. Inspect the important pages and request indexing

For each URL below: paste it into the **URL Inspection** bar at the top of Search
Console, wait for the report, and click **Request indexing** (a few per day is
enough; Google decides when to crawl).

```
https://jonehotels.com/
https://jonehotels.com/rooms
https://jonehotels.com/facilities
https://jonehotels.com/contact
https://jonehotels.com/gallery
https://jonehotels.com/offers
https://jonehotels.com/about
```

Check the report says **"URL is on Google"** (or will be after crawling) and that
the **canonical** Google chose equals the URL you inspected. If Google reports a
different canonical, do not panic — re-inspect after a week.

**Do not request indexing** for `/booking`, `/booking-confirmation`,
`/payment-verify`, `/my-bookings`, `/login`, `/dashboard/` or `/portal/`. Those
are deliberately marked `noindex`.

---

## 4. Watch the reports that matter

| Report | What to look for |
|---|---|
| **Indexing → Pages** | "Not indexed" reasons. `Excluded by 'noindex' tag` for the booking/portal pages is **correct**. `Crawled – currently not indexed` on a marketing page after 2–3 weeks is worth investigating. |
| **Experience → Core Web Vitals** | Mobile first. LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 at the 75th percentile. Field data appears after roughly 28 days of traffic. |
| **Performance → Search results** | Queries, impressions, clicks, CTR. Filter by *Branded* queries (`J-ONE`, `J One Hotel`, `Jone Hotels`) versus *non-branded* (`hotel in Enugu`, `hotel near Mater Dei`, `short let Enugu`). |
| **Settings → Crawled pages / Sitemaps** | Any 5xx or 4xx on public URLs |

Two realistic expectations:

* **Branded searches** (`J-ONE Hotel & Lodge`) should be the first win — the
  site now states the exact name, address, phone and city in the title, in the
  page text and in the structured data, so Google can match the name to the site.
* **"hotel in Enugu"** is a competitive query. The site is now technically
  eligible; ranking still depends on relevance, reviews, proximity and
  competition. Nobody can guarantee a position.

---

## 5. Three technical actions only the owner can approve

1. **Redirect `www.jonehotels.com` → `jonehotels.com` (301).**
   Today both hosts return 200. Every canonical on the site points at the apex,
   so Google consolidates there anyway, but a permanent redirect is cleaner.
   In Cloudflare: **Rules → Redirect Rules → Create rule** — *If* hostname equals
   `www.jonehotels.com`, *then* static redirect to
   `https://jonehotels.com` + the same path/query, status **301**, preserve query
   string. Do not redirect `/api/*` (it is proxied separately, if at all).

2. **Turn on edge image conversion (optional, biggest byte win).**
   Cloudflare dashboard → your zone → **Speed → Optimization → Image
   Optimization**: enable *Polish* ("Convert to WebP") and *Mirage* if offered.
   This converts the existing JPEGs per-visitor without touching the site or the
   admin image overrides.

3. **Trim the homepage hero to three slides (optional).** Six full-screen photos
   are fetched on first load (~1 MB). Fewer slides = faster LCP. It is a visual
   change, so it is your call — ask your developer to remove the `hero-4`,
   `hero-5` and `hero-6` slides in `frontend/index.html` and rebuild.

---

## 6. Google Business Profile checklist (manual — do not automate)

The hotel is already on Google Maps. Work through this in the Business Profile
manager (<https://business.google.com>), not from the website.

1. **Confirm the listing is verified and owned.**
   Search your business name on Google Maps while signed in; if you see
   "Claim this business", claim it. If someone else manages it, request access
   through the profile rather than creating a second listing.
   **Never create a duplicate listing** — duplicates split reviews and confuse
   Google about which business the website belongs to.

2. **Primary category: `Hotel`.** Add secondary categories only for what you
   actually offer (e.g. `Lodge`, `Guest house`, `Event venue`). Categories decide
   which searches you can appear in; wrong categories attract the wrong traffic.

3. **Name, address, phone and website must match the site exactly.**
   * Name: `J-ONE Hotel & Lodge`
   * Address: `Plot 566 Mgbowo Street, off Ezike Street, Enugu, Enugu State, Nigeria`
     (the backend currently stores a longer form that adds "off NOWAS, near Mater
     Dei Catholic Church, Trans-Ekulu" — see §7 below)
   * Phone: `+234 803 211 2874`
   * Website: `https://jonehotels.com`
   Inconsistency between the profile and the site is the most common cause of a
   business failing to rank for its own name.

4. **Complete the attributes.** Wi-Fi, air conditioning, parking, restaurant,
   power supply/24-hour reception — tick only what is true. The website publishes
   exactly six in-room amenities (air conditioning, free Wi-Fi, television,
   refrigerator, water heater, 24/7 power); keep the profile consistent with them,
   and tell your developer before adding a new amenity so both stay in step.

5. **Upload genuine photographs.** Exterior (sign visible), reception, each room
   type, bathroom, dining and any event space. 10–20 real photos, updated
   seasonally, outperform stock imagery. Do not upload images that are not the
   hotel.

6. **Encourage real reviews.** Ask every departing guest — in person, or via the
   confirmation email's review link — for an honest review. Never buy reviews,
   never write them yourself, never offer anything in exchange. Review count and
   freshness are among the strongest local ranking signals.

7. **Reply to every review**, positive and negative, within a few days, in a
   professional voice that names the hotel. Replies are public and are read by
   future guests.

8. **Check performance monthly.** Business Profile → *Performance* (calls,
   direction requests, website clicks, searches) alongside Search Console
   *Performance*. A rise in "website clicks" without a rise in "searches" can
   mean the profile is doing the work — or that the site is losing branded
   traffic. Compare them.

9. **Post updates and offers** to the profile (Business Profile → *Posts*) when
   you run a genuine offer. Do not post expired promotions — that is exactly why
   the website's `/offers` page only renders live offers.

---

## 7. Known data inconsistencies to resolve (owner decision)

The website and the backend currently disagree in three places. Nothing was
changed automatically, because each is a business fact, not a technical one.

| Item | Where it says what | What to do |
|---|---|---|
| **Address wording** | Website / structured data: `Plot 566 Mgbowo Street, off Ezike Street, Enugu State, Nigeria`. Backend hotel settings: `Plot 566, Mgbowo Street, off NOWAS, near Mater Dei Catholic Church, Trans-Ekulu, Enugu State, Nigeria`. | Whichever wording matches the verified Business Profile is the correct one. Update the other two: the backend setting **and** `SITE["street"]` in `frontend/seo_build.py`, then rebuild. |
| **Check-in / check-out time** | Backend hotel settings: check-in **12:00**, check-out **12:00**. Contact page text: "Check-in from **2:00 PM**". | Confirm the real times. Until then the site does not publish them in structured data. Once confirmed, update the backend setting (which drives the site) and the contact-page default text. |
| **Google Maps URL** | Backend `google_maps_url` is empty, so the site falls back to a Maps **search** for the address (correct, but not the profile's place link). | In Hotel Settings, paste the share link from the verified Business Profile (and the "write a review" link into `google_review_url`). The site then uses both automatically on every page. |
