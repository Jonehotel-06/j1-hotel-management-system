/* Profile-photo upload honesty in the dashboard.

   Reported (again): "profile images are not uploading and it doesn't display
   any error message why it fails". Two gaps made that possible and both are
   pinned here:

     * the save handler toasted "Profile updated." without ever checking that
       the photo could be displayed, so a photo that reached no reachable
       storage looked like a success; and
     * the API's answer about the photo (did it reach media storage?) was not
       used anywhere in the UI.

   The page must now say what actually happened, and never claim a photo is
   showing when it is not.

   Run from /frontend: node --test tests-js/test-profile-photo-ui.js */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const FRONTEND = path.resolve(__dirname, "..");
const PROFILE = fs.readFileSync(path.join(FRONTEND, "dashboard", "profile.html"), "utf8");
const STAFF = fs.readFileSync(path.join(FRONTEND, "dashboard", "staff.html"), "utf8");

function slice(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + 1);
  assert.ok(start > 0 && end > start, `${startMarker} not found in the shipped page`);
  return source.slice(start, end);
}

const SAVE = slice(PROFILE, '$("pf-form").addEventListener("submit"', '$("pw-form").addEventListener("submit"');
const PROBE = slice(PROFILE, "function probeImage(url){", "$(\"pf-form\").addEventListener");

test("the photo field says the upload is verified, so the outcome is never a guess", () => {
  assert.match(PROFILE, /id="pf-photo-note"/);
  assert.match(PROFILE, /role="alert"/);
  assert.match(PROFILE, /if your browser cannot display it, this page will tell you why/);
});

test("the browser itself checks that the stored photo really loads", () => {
  assert.match(PROBE, /new Image\(\)/);
  assert.match(PROBE, /img\.onload/);
  assert.match(PROBE, /img\.onerror/);
  // A hung request must not leave the desk waiting forever.
  assert.match(PROBE, /setTimeout\(function\(\)\{ finish\(false\); \}, 8000\)/);
  // The probe is cache-busted so a replaced photo is re-fetched, not remembered.
  assert.match(PROBE, /photo-check=/);
});

test("an unreachable photo is reported with the reason, not a success toast", () => {
  assert.match(SAVE, /me\.profile_image_stored === false/);
  assert.match(SAVE, /probeImage\(me\.profile_image_url\)/);
  assert.match(SAVE, /var why = me\.profile_image_warning \|\|/);
  assert.match(SAVE, /photoNote\(why \+ " Your name and phone number were saved\."\)/);
  assert.match(SAVE, /JONE\.ui\.toast\("Photo could not be displayed — " \+ why, "error"\)/);
  // The generic success line cannot be reached while a photo is unverified.
  assert.doesNotMatch(SAVE, /if\(file\)\{[\s\S]{0,200}JONE\.ui\.toast\("Profile updated\."/);
});

test("a photo that really loads is confirmed as showing", () => {
  assert.match(SAVE, /photoState === true/);
  assert.match(SAVE, /"Profile updated and your photo is showing\."/);
  // No photo in this save -> the ordinary confirmation, nothing about photos.
  assert.match(SAVE, /\} else \{\s*JONE\.ui\.toast\("Profile updated\.","success"\);/);
});

test("the note is cleared before each attempt so a stale problem cannot linger", () => {
  assert.match(SAVE, /photoNote\(""\);/);
});

test("the staff editor reports the same answer to the administrator", () => {
  const save = slice(STAFF, 'onSubmit: async function(values, formData){', "onSubmit: async function(vals){");
  assert.match(save, /saved\.profile_image_stored === false/);
  assert.match(save, /JONE\.ui\.toast\(saved\.profile_image_warning \|\|/);
  assert.match(save, /"error"\)/);
  // The success toast only happens when the API did not deny the photo.
  assert.match(save, /\} else \{\s*JONE\.ui\.toast\(id \? "Staff account updated\."/);
});
