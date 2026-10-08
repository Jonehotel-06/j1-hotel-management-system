#!/usr/bin/env python3
# dashboard/shell.py
"""Dashboard shell builders. Dashboard pages share a dense ops shell that is
distinct from the public marketing site. Assets live one level up (../)."""

# Theme boot: sets data-theme immediately and removes the preload guard so the
# page can never be left hidden. Kept as a plain string (NOT inside an f-string)
# to avoid brace interpolation issues.
THEME_BOOT = (
    '<script>(function(){var t;try{t=JSON.parse(localStorage.getItem("jone.theme"))}'
    'catch(e){}if(t!=="dark"&&t!=="light"){t=(window.matchMedia&&'
    'window.matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light"}'
    'document.documentElement.setAttribute("data-theme",t);})();</script>'
)


def head(title, subtitle=""):
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en" data-theme="light">\n'
        '<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        '<title>' + title + ' | J-ONE Staff</title>\n'
        '<link rel="icon" href="../favicon/favicon-32.png" sizes="32x32">\n'
        '<link rel="apple-touch-icon" href="../favicon/apple-touch-icon.png">\n'
        '<link rel="stylesheet" href="../css/main.css">\n'
        + THEME_BOOT + '\n'
        '</head>\n'
        '<body>\n'
        '<div class="dash">\n'
        '  <aside class="dash-sidebar" aria-label="Staff navigation">\n'
        '    <div class="dash-sidebar-brand">\n'
        '      <img src="../assets/icons/logo-official.svg" alt="" width="30" height="42">\n'
        '      <span>\n'
        '        <span class="brand-word">J-ONE</span><br>\n'
        '        <span class="brand-sub">Staff Console</span>\n'
        '      </span>\n'
        # Desktop collapse control lives at the top-right of the brand header
        # (js/dashboard.js also injects it here when absent — setupSidebarCollapse
        # owns the behaviour; hidden on mobile by CSS so the hamburger drawer is
        # never affected).
        '      <button class="dash-sidebar-collapse" type="button" data-sidebar-collapse aria-expanded="true" aria-label="Collapse sidebar">\n'
        '        <span data-icon="chevronLeft" data-size="18"></span>\n'
        '      </button>\n'
        '    </div>\n'
        '    <nav class="dash-sidebar-nav" data-dash-nav aria-label="Staff"></nav>\n'
        '    <div class="dash-sidebar-footer">\n'
        '      <div data-dash-user></div>\n'
        '      <button class="btn btn-sm btn-outline" data-logout style="width:100%;justify-content:center;color:#D6D5D3;border-color:rgba(255,255,255,0.2);" aria-label="Sign out">\n'
        '        <span data-icon="logOut" data-size="16"></span> <span class="dash-logout-label">Sign out</span>\n'
        '      </button>\n'
        '    </div>\n'
        '  </aside>\n'
        '  <div class="dash-main">\n'
        '    <div class="dash-topbar">\n'
        '      <div class="dash-topbar-left">\n'
        '        <button class="btn-icon btn-outline dash-menu-toggle" aria-label="Open menu" data-icon="menu" data-size="22"></button>\n'
        '        <div class="dash-title" data-dash-title>\n'
        '          <h1>' + title + '</h1>\n'
        '        </div>\n'
        '      </div>\n'
        '      <div class="dash-topbar-right">\n'
        '        <div class="dash-topbar-actions" data-topbar-actions></div>\n'
        '        <button class="theme-toggle" type="button" aria-label="Toggle theme" aria-pressed="false">\n'
        '          <span class="icon-moon" data-icon="moon" data-size="18"></span>\n'
        '          <span class="icon-sun" data-icon="sun" data-size="18"></span>\n'
        '        </button>\n'
        '      </div>\n'
        '    </div>\n'
        '    <div class="dash-content">\n'
    )


def foot(extra_scripts=""):
    return (
        '    </div>\n'
        '  </div>\n'
        '</div>\n'
        '<script src="../js/runtime-config.js"></script>\n'
        '<script src="../js/config.js"></script>\n'
        '<script src="../js/utils.js"></script>\n'
        '<script src="../js/icons.js"></script>\n'
        '<script src="../js/api.js"></script>\n'
        '<script src="../js/theme.js"></script>\n'
        '<script src="../js/ui.js"></script>\n'
        '<script src="../js/auth.js"></script>\n'
        '<script src="../js/navigation.js"></script>\n'
        '<script src="../js/dashboard.js"></script>\n'
        '<script>\n'
        '  JONE.ui.initChrome();\n'
        # boot() handles sidebar rendering, the mobile drawer toggle (setupSidebar),
        # and nav init — do NOT duplicate initDashboardNav here.
        '  JONE.dashboard.boot("receptionist");\n'
        '  document.querySelector("[data-logout]") && document.querySelector("[data-logout]").addEventListener("click", function(){ window.Auth.logout(); });\n'
        + extra_scripts + '\n'
        '</script>\n'
        '</body>\n'
        '</html>\n'
    )
