"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const os=require("node:os");
const {execFileSync}=require("node:child_process");
const {FRONTEND}=require("./harness");
const page=fs.readFileSync(path.join(FRONTEND,"reset-password.html"),"utf8");
const login=fs.readFileSync(path.join(FRONTEND,"login.html"),"utf8");

test("reset page links to the existing public API without exposing a link to subresources",()=>{
  assert.match(login,/href="reset-password\.html"/);
  assert.match(page,/<meta name="robots" content="noindex, nofollow">/);
  assert.match(page,/<meta name="referrer" content="no-referrer">/);
  assert.ok(page.indexOf("history.replaceState") < page.indexOf("rel=\"stylesheet\""));
  assert.match(page,/\/api\/auth\/password\/reset\/confirm\//);
  assert.match(page,/\/api\/auth\/password\/reset\//);
  assert.match(page,/new_password: password, new_password_confirm: confirmation/);
  assert.equal((page.match(/\{auth: false\}/g)||[]).length,2);
  const credentialScript=page.match(/<script>\s*\/\/ Read the one-use link[\s\S]*?<\/script>/);
  const themeScript=page.match(/<script>\s*\/\* Pre-paint theme[\s\S]*?<\/script>/);
  assert.ok(credentialScript && themeScript);
  assert.doesNotMatch(credentialScript[0],/localStorage|sessionStorage|indexedDB|document\.cookie|console\./);
  assert.match(themeScript[0],/localStorage/);
  assert.doesNotMatch(themeScript[0],/setItem|token|uid/);
});

test("release builder stamps reset page without reload-capable scripts",()=>{
  const tmp=fs.mkdtempSync(path.join(os.tmpdir(),"jone-reset-build-"));
  try {
    fs.mkdirSync(path.join(tmp,"components"));
    for(const name of ["build.py","seo_build.py","reset-password.html"])
      fs.copyFileSync(path.join(FRONTEND,name),path.join(tmp,name));
    for(const name of ["header.html","footer.html","mobile-nav.html"])
      fs.copyFileSync(path.join(FRONTEND,"components",name),path.join(tmp,"components",name));
    const check=[
      "import build",
      "pages = list(build.PUBLIC_PAGES) + list(build.PORTAL_PAGES) + list(build.QR_SERVICE_PAGES) + list(getattr(build, 'RESET_PASSWORD_PAGES', []))",
      "print('included=' + str('reset-password.html' in pages))",
      "build.build('reset-password.html', '9.9.9')",
    ].join("; ");
    const output=execFileSync("python3",["-c",check],{cwd:tmp,encoding:"utf8"});
    const built=fs.readFileSync(path.join(tmp,"reset-password.html"),"utf8");
    assert.match(output,/included=True/,"the release build must include this private page");
    assert.match(built,/js\/runtime-config\.js\?v=9\.9\.9/);
    assert.match(built,/<meta name="robots" content="noindex, nofollow">/);
    assert.doesNotMatch(built,/js\/(?:update-checker|pwa)\.js/);
  } finally {fs.rmSync(tmp,{recursive:true,force:true});}
});
