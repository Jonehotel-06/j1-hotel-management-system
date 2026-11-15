/* tests-js/test-image-upload-accept.js */
/* Browser-side half of the "JPG/JPEG photos will not upload" bug.

   Reported symptom: PNG uploads worked, JPG/JPEG uploads were refused. The
   uploaders compared `File.type` against the literal list
   ["image/jpeg", "image/png", "image/webp"]. A browser derives that string from
   the file NAME using the operating system's MIME table, so the same JPEG can
   arrive as "image/jpeg", "image/jpg", "image/pjpeg", "image/x-citrix-jpeg" or
   "" (unknown) — while a PNG is essentially always "image/png". Real photos
   were therefore blocked in the browser before anything was sent.

   The shipped production function JONE.dashboard.imageFileIssue() is exercised
   unmodified inside the vm harness.

   Run: node --test tests-js/test-image-upload-accept.js   (from frontend/) */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { makeContext, loadScript, disposeAll } = require("./harness");

test.after(() => disposeAll());

function boot() {
  const ctx = makeContext();
  loadScript(ctx, "js/config.js");
  loadScript(ctx, "js/utils.js");
  loadScript(ctx, "js/dashboard.js");
  return ctx.window.JONE.dashboard;
}

/* A File-like object: the uploader only reads name, type and size. */
function file(name, type, size) {
  return { name: name, type: type, size: size === undefined ? 2048 : size };
}

/* ------------------------- the reported failure ------------------------- */

test("a JPEG reported as image/jpeg is accepted", () => {
  assert.equal(boot().imageFileIssue(file("lobby.jpg", "image/jpeg"), 5), null);
});

test("a JPEG reported as image/jpg (Windows mapping) is accepted", () => {
  assert.equal(boot().imageFileIssue(file("lobby.JPG", "image/jpg"), 5), null);
});

test("a JPEG reported as image/pjpeg (legacy IE mapping) is accepted", () => {
  assert.equal(boot().imageFileIssue(file("lobby.jpeg", "image/pjpeg"), 5), null);
});

test("a JPEG with an empty MIME type is accepted", () => {
  /* Several mobile browsers hand over an empty type for camera files. */
  assert.equal(boot().imageFileIssue(file("IMG_2041.JPG", ""), 5), null);
});

test("a JPEG whose name carries .jfif/.jpe is accepted", () => {
  const dashboard = boot();
  ["photo.jfif", "photo.jpe", "photo.JFIF", "photo.pjpeg"].forEach((name) => {
    assert.equal(dashboard.imageFileIssue(file(name, ""), 5), null, name);
  });
});

test("a JPEG with no extension at all is accepted", () => {
  assert.equal(boot().imageFileIssue(file("blob", ""), 5), null);
});

test("a JPEG whose MIME disagrees with its extension is still offered", () => {
  const dashboard = boot();
  assert.equal(dashboard.imageFileIssue(file("photo.jpg", "image/jpg"), 5), null);
  assert.equal(dashboard.imageFileIssue(file("photo.jpeg", "application/octet-stream"), 5), null);
});

/* --------------------------- unchanged behaviour ------------------------- */

test("PNG and WebP keep working", () => {
  const dashboard = boot();
  assert.equal(dashboard.imageFileIssue(file("shot.png", "image/png"), 5), null);
  assert.equal(dashboard.imageFileIssue(file("shot.webp", "image/webp"), 5), null);
});

test("a genuinely unsupported file is refused with a readable message", () => {
  const dashboard = boot();
  ["clip.mp4", "notes.pdf", "sheet.xlsx", "archive.zip"].forEach((name) => {
    const issue = dashboard.imageFileIssue(file(name, "video/mp4"), 5);
    assert.ok(issue && /JPG, PNG or WebP/.test(issue), name + " -> " + issue);
  });
});

test("an empty file is named as empty", () => {
  const issue = boot().imageFileIssue(file("photo.jpg", "image/jpeg", 0), 5);
  assert.match(issue, /empty/i);
});

test("an oversize image reports its size, the limit and what to do", () => {
  const issue = boot().imageFileIssue(file("huge.jpg", "image/jpeg", 6 * 1024 * 1024), 5);
  assert.match(issue, /6\.0 MB/);
  assert.match(issue, /maximum is 5 MB/);
  assert.match(issue, /resize/i);
});

test("the size ceiling follows the configured limit", () => {
  const dashboard = boot();
  const just = 2 * 1024 * 1024;
  assert.equal(dashboard.imageFileIssue(file("a.jpg", "image/jpeg", just), 2), null);
  assert.match(dashboard.imageFileIssue(file("a.jpg", "image/jpeg", just + 1), 2), /maximum is 2 MB/);
});

test("no file selected produces a message, not a crash", () => {
  assert.match(boot().imageFileIssue(null, 5), /Select an image/);
});

/* ------------------ the inputs on the pages offer the formats ------------- */

const fs = require("node:fs");
const path = require("node:path");
const PAGE_DIR = path.resolve(__dirname, "..");

function readPage(rel) {
  return fs.readFileSync(path.join(PAGE_DIR, rel), "utf8");
}

test("every dashboard file input accepts .jpg and .jpeg (not just image/jpeg)", () => {
  ["dashboard/gallery.html", "dashboard/rooms.html"].forEach((page) => {
    const html = readPage(page);
    const accepts = html.match(/accept="[^"]*"/g) || [];
    const imageAccepts = accepts.filter((a) => /image\//.test(a));
    assert.ok(imageAccepts.length > 0, page + " has no image input");
    imageAccepts.forEach((attr) => {
      assert.match(attr, /\.jpg/, page + " must offer .jpg: " + attr);
      assert.match(attr, /\.jpeg/, page + " must offer .jpeg: " + attr);
      assert.match(attr, /image\/jpeg/, page + " must offer image/jpeg: " + attr);
    });
  });
});

test("the shared helper is exported for every dashboard uploader", () => {
  const source = readPage("js/dashboard.js");
  assert.match(source, /imageFileIssue,/);
  assert.match(source, /IMAGE_ACCEPT,/);
});

test("the strict literal whitelist no longer gates uploads", () => {
  const dashboard = readPage("js/dashboard.js");
  assert.doesNotMatch(
    dashboard,
    /!\["image\/jpeg", "image\/png", "image\/webp"\]\.includes\(/,
    "the old exact-MIME whitelist must not remain"
  );
});

test("the gallery and rooms pages delegate to the shared rule", () => {
  assert.match(readPage("dashboard/gallery.html"), /imageFileIssue/);
  assert.match(readPage("dashboard/rooms.html"), /imageFileIssue/);
});

/* -------------------------- static contract ----------------------------- */

test("the client list and the server validator agree on accepted formats", () => {
  const backend = fs.readFileSync(
    path.resolve(PAGE_DIR, "..", "backend", "apps", "core", "validators.py"),
    "utf8"
  );
  const client = boot();
  /* Every MIME the backend recognises must be accepted by the client. */
  const backendMimes = (backend.match(/RECOGNISED_IMAGE_MIME_TYPES = \{[\s\S]*?\}/) || [""])[0]
    .match(/"[^"]+"/g) || [];
  assert.ok(backendMimes.length >= 6, "could not read the backend MIME list");
  backendMimes.forEach((quoted) => {
    const mime = quoted.replace(/"/g, "");
    assert.ok(
      client.IMAGE_TYPES.indexOf(mime) !== -1,
      "client rejects a MIME the backend recognises: " + mime
    );
  });
  /* And every extension the backend allows must be in the client's list. */
  const backendExts = (backend.match(/ALLOWED_IMAGE_EXTENSIONS = \{[\s\S]*?\}/) || [""])[0]
    .match(/"\.[a-z]+"/g) || [];
  assert.ok(backendExts.length >= 6, "could not read the backend extension list");
  backendExts.forEach((quoted) => {
    const ext = quoted.replace(/["\.]/g, "");
    assert.ok(
      client.IMAGE_EXTENSIONS.indexOf(ext) !== -1,
      "client rejects an extension the backend allows: " + ext
    );
  });
});
