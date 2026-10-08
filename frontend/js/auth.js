/* js/auth.js */
/* ==========================================================================
   Auth — login, registration, session persistence, role-aware UI, guards.
   Frontend role checks are UX-only; the backend is authoritative.

   VERIFIED contract (live backend):
   - POST /api/auth/login/    {email, password}            → {user, tokens}
   - POST /api/auth/register/ {email, names, phone, pwd…}  → {user, tokens}
   - POST /api/auth/token/refresh/ {refresh}               → {tokens}
   - POST /api/auth/logout/   {refresh}                    → blacklists token
   - GET  /api/auth/profile/                              → user
   - user.role ∈ GUEST | RECEPTIONIST | MANAGER | ADMIN (uppercase on the wire)
   ========================================================================== */

(function () {
  "use strict";

  const KEY = (window.APP_CONFIG && window.APP_CONFIG.STORAGE.AUTH) || "jone.auth";
  const SESSION_KEY = (window.APP_CONFIG && window.APP_CONFIG.STORAGE.SESSION) || "jone.session";

  const state = {
    user: null,
    role: null,
    permissions: []
  };

  const ROLES = {
    ADMIN: "admin", MANAGER: "manager", RECEPTIONIST: "receptionist", CASHIER: "cashier",
    HOUSEKEEPING: "housekeeping", MAINTENANCE: "maintenance", INVENTORY_CLERK: "inventory_clerk",
    GUEST: "guest", STAFF: "staff"
  };

  /* Backend sends UPPERCASE roles; normalize once at the boundary. */
  function normRole(role) {
    return role ? String(role).toLowerCase() : null;
  }

  function isStaffRole(role) {
    const r = normRole(role || state.role);
    return ["admin", "manager", "receptionist", "cashier", "housekeeping", "maintenance", "inventory_clerk"].includes(r);
  }

  /* Store a session from either the login/register shape
     {user, tokens:{access,refresh}} or a bare user. */
  function storeSession(data) {
    const payload = data || {};
    const tokens = payload.tokens || {};
    const user = payload.user || (payload.access || payload.token ? payload : null);
    const cred = {
      access: tokens.access || payload.access || payload.token || null,
      refresh: tokens.refresh || payload.refresh || null
    };
    try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(cred)); } catch (_) {}
    const profile = user && (user.email !== undefined) ? user : payload;
    JONE.storage.set(KEY, profile);
    state.user = profile;
    state.role = normRole(profile.role);
    state.permissions = profile.permissions || [];
    return state.user;
  }

  function clearSession() {
    try { sessionStorage.removeItem(SESSION_KEY); } catch (_) {}
    JONE.storage.remove(KEY);
    state.user = null; state.role = null; state.permissions = [];
  }

  function getSessionCred() {
    try { return JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch (_) { return null; }
  }

  function isAuthenticated() {
    try { return !!(sessionStorage.getItem(SESSION_KEY) && getSessionCred() && getSessionCred().access); }
    catch (_) { return !!JONE.storage.get(KEY, null); }
  }

  function hasRole(minRole) {
    // guest < receptionist < manager < admin
    if (!state.role) return false;
    const order = { guest: 0, receptionist: 1, staff: 1, manager: 2, admin: 3 };
    return (order[state.role] != null ? order[state.role] : -1) >= (order[minRole] != null ? order[minRole] : 99);
  }

  // Exact operational-role visibility guard. It is deliberately separate from
  // hasRole(): CASHIER / HOUSEKEEPING / MAINTENANCE / INVENTORY_CLERK do not
  // form a privilege hierarchy, and server-side capabilities remain decisive.
  function hasAnyRole(roles) {
    if (!state.role || !Array.isArray(roles)) return false;
    return roles.map(normRole).includes(state.role);
  }

  function can(perm) {
    if (!perm) return true;
    if (state.permissions.includes("*")) return true;
    return state.permissions.includes(perm);
  }

  /* Wire API token provider + one-time refresh provider. */
  function setAPITokenProvider() {
    window.API.setTokenProvider(() => {
      const cred = getSessionCred();
      return cred ? (cred.access || null) : null;
    });
    if (window.API.setRefreshProvider) window.API.setRefreshProvider(refreshAccess);
  }
  setAPITokenProvider();

  /* One-time refresh: called by the API layer on a 401. Returns true when a new
     access token was obtained so the original request is retried once. Never
     loops (the API layer only retries a single time). */
  let refreshing = false;
  async function refreshAccess() {
    if (refreshing) return false;      // guard against concurrent refresh storms
    refreshing = true;
    try {
      const cred = getSessionCred();
      if (!cred || !cred.refresh) return false;
      const res = await window.API.refreshTokenCall(cred.refresh);
      const tokens = (res.data && (res.data.tokens || res.data)) || {};
      const access = tokens.access || null;
      if (!access) return false;
      try {
        const cur = getSessionCred() || {};
        cur.access = access;
        if (tokens.refresh) cur.refresh = tokens.refresh;   // rotation
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(cur));
      } catch (_) {}
      return true;
    } catch (_) {
      return false;
    } finally {
      refreshing = false;
    }
  }

  function onUnauthorized() {
    // Called by the API layer only AFTER a refresh attempt failed (or when no
    // refresh provider is configured). Clear session and redirect to login,
    // preserving the original destination for post-login return.
    clearSession();
    const here = location.pathname + location.search;
    // Already on the login page (e.g. a failed sign-in must never count as
    // "session expired"): just show the message, never redirect in a loop.
    if (location.pathname.indexOf("/login") === 0) return;
    const next = encodeURIComponent(here);
    try { JONE.ui.toast("Your session has expired. Please sign in again.", "warning"); } catch (_) {}
    if (location.pathname.indexOf("/dashboard/") === 0) {
      location.replace("/login.html?next=" + next);
    } else if (location.pathname.indexOf("booking") === 0 || location.pathname.indexOf("/booking") !== -1) {
      // Guest mid-booking: return them to the same step after signing back in.
      location.replace("/login.html?next=" + next);
    }
    // Other public pages: stay put (the page shows its own error state).
  }

  /* Restore profile on load so header can render user state without a round-trip. */
  function restore() {
    setAPITokenProvider();
    if (isAuthenticated()) {
      const profile = JONE.storage.get(KEY, null);
      if (profile) {
        state.user = profile.user || profile;
        state.role = normRole(state.user.role || profile.role);
        state.permissions = state.user.permissions || profile.permissions || [];
      }
    }
  }

  async function login(email, password) {
    const res = await window.API.login({ email: email, password: password });
    storeSession(res.data);
    setAPITokenProvider();
    return res.data;
  }

  async function logout() {
    const cred = getSessionCred();
    try {
      // API.logout attaches the stored access token when present (and the API
      // layer transparently refreshes it if expired), so a normal logout does
      // not produce a spurious 401. An already-dead session is fine too: the
      // backend logout is idempotent and local state is cleared regardless.
      await window.API.logout(cred ? cred.refresh : null);
    } catch (_) { /* session already gone — still clean up locally */ }
    clearSession();
    const here = location.pathname + location.search;
    const staffArea = here.indexOf("/dashboard/") !== -1 || here.indexOf("/login") === 0;
    location.href = staffArea ? "/login.html" : "index.html";
  }

  async function refreshProfile() {
    const res = await window.API.me();
    storeSession({ user: res.data, tokens: getSessionCred() || {} });
    return state.user;
  }

  /* Guards for dashboard pages. Call Auth.guard() on page load. */
  function guard(minRole) {
    restore();
    if (!isAuthenticated()) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.replace("/login.html?next=" + next);
      return false;
    }
    const allowed = Array.isArray(minRole) ? hasAnyRole(minRole) : hasRole(minRole);
    if (minRole && !allowed) {
      // Not enough permission (frontend only; backend enforces too). Show 403.
      location.replace("/403.html");
      return false;
    }
    return true;
  }

  function bindLoginForm(formSel, opts = {}) {
    const form = document.querySelector(formSel);
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("[type=submit]");
      if (!JONE.guardSubmit(btn)) return;
      const fd = new FormData(form);
      const email = String(fd.get("email") != null ? fd.get("email") : (fd.get("username") || "")).trim();
      const password = fd.get("password") || "";
      try {
        const session = await login(email, password);
        JONE.ui.toast("Welcome back" + (session && session.user && session.user.first_name ? ", " + session.user.first_name : "") + ".", "success");
        // Staff land in the dashboard; guests return whence they came (or home).
        const params = new URLSearchParams(location.search);
        let next = params.get("next");
        if (!next || /login/i.test(next)) next = null;   // never bounce back to the login page
        if (!next) next = isStaffRole(session && session.user && session.user.role) ? "dashboard/index.html" : "index.html";
        location.href = next;
      } catch (err) {
        JONE.releaseGuard(btn);
        const msg = err.status === 401 ? "Incorrect email or password." : (err.message || "Sign-in failed. Please try again.");
        JONE.ui.toast(msg, "error");
        const errBox = form.querySelector("[data-form-error]");
        if (errBox) { errBox.textContent = msg; errBox.style.display = "block"; }
      }
    });
  }

  window.Auth = {
    ROLES, state, login, logout, guard, hasRole, hasAnyRole, can,
    isAuthenticated, restore, refreshProfile, onUnauthorized,
    bindLoginForm, clearSession, refreshAccess,
    isStaffRole
  };
  window.JONE = window.JONE || {};
})();
