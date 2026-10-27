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
    permissions: [],
    capabilities: [],
    capabilitiesLoaded: false
  };

  const ROLES = {
    ADMIN: "admin", MANAGER: "manager", RECEPTIONIST: "receptionist",
    HOUSEKEEPING: "housekeeping", MAINTENANCE: "maintenance", INVENTORY_CLERK: "inventory_clerk",
    FRONT_DESK_SUPERVISOR: "front_desk_supervisor", GENERAL_MANAGER: "general_manager",
    RESTAURANT_MANAGER: "restaurant_manager", WAITER: "waiter", BAR_MANAGER: "bar_manager",
    BARTENDER: "bartender", KITCHEN_MANAGER: "kitchen_manager", CHEF: "chef",
    HOUSEKEEPING_MANAGER: "housekeeping_manager", HOUSEKEEPER: "housekeeper",
    MAINTENANCE_TECHNICIAN: "maintenance_technician", ACCOUNTS_MANAGER: "accounts_manager",
    ACCOUNTANT: "accountant", HR_MANAGER: "hr_manager", HR_OFFICER: "hr_officer", SECURITY: "security",
    PROCUREMENT_OFFICER: "procurement_officer", STOREKEEPER: "storekeeper",
    PORTER: "porter", KITCHEN_ASSISTANT: "kitchen_assistant",
    GUEST: "guest", STAFF: "staff"
  };

  /* Backend sends UPPERCASE roles; normalize once at the boundary. CASHIER is
     a retired stored role: stale sessions/JWT claims resolve to the existing
     Restaurants WAITER role rather than recreating a second active identity. */
  const RETIRED_ROLE_ALIASES = Object.freeze({ cashier: "waiter" });
  function normRole(role) {
    const value = role ? String(role).toLowerCase() : null;
    return value && RETIRED_ROLE_ALIASES[value] ? RETIRED_ROLE_ALIASES[value] : value;
  }

  /* Every operational role code. The department roles do NOT form a privilege
     hierarchy (a chef is not "below" a receptionist — they do different jobs),
     so a personal surface that belongs to all staff members is expressed as an
     exact-role list rather than a minimum role. Guests are never included. */
  const OPERATIONAL_ROLES = Object.freeze(
    Object.values(ROLES).filter((value) => value !== ROLES.GUEST)
  );

  function isStaffRole(role) {
    const r = normRole(role || state.role);
    return Object.values(ROLES).filter((value) => value !== "guest").includes(r);
  }

  function getSubdomainContext(hostnameOverride, searchOverride) {
    const map = (window.APP_CONFIG && window.APP_CONFIG.DEPARTMENT_SUBDOMAINS) || {};
    const host = String(hostnameOverride != null ? hostnameOverride : (window.location && window.location.hostname) || "").toLowerCase();
    const parts = host.split(".");
    if (parts.length >= 2) {
      const first = parts[0];
      if (first && first !== "www" && first !== "jonehotel" && map[first]) {
        return map[first];
      }
    }
    try {
      const searchStr = searchOverride != null ? searchOverride : (window.location && window.location.search) || "";
      const params = new URLSearchParams(searchStr);
      const deptParam = String(params.get("department") || params.get("portal") || "").toLowerCase().trim();
      if (deptParam && map[deptParam]) return map[deptParam];
    } catch (_) {}
    return null;
  }

  function isAuthorizedForSubdomain(roleOrUser, subdomainCtx) {
    const ctx = subdomainCtx || getSubdomainContext();
    if (!ctx) return true;
    const rawRole = typeof roleOrUser === "object" && roleOrUser !== null ? roleOrUser.role : roleOrUser;
    const r = normRole(rawRole || state.role);
    if (!r) return false;
    if (r === "admin") return true;
    if (Array.isArray(ctx.allowedRoles) && ctx.allowedRoles.includes(r)) return true;
    return false;
  }

  function defaultStaffLanding(role) {
    const r = normRole(role);
    const subCtx = getSubdomainContext();
    if (subCtx && isAuthorizedForSubdomain(r, subCtx) && subCtx.landingPath) {
      return subCtx.landingPath.replace(/^\//, "");
    }
    if (["restaurant_manager", "waiter", "bar_manager", "bartender", "kitchen_manager", "chef", "kitchen_assistant"].includes(r)) {
      return "dashboard/pos.html";
    }
    if (["housekeeping", "housekeeping_manager", "housekeeper"].includes(r)) return "dashboard/housekeeping.html";
    if (["porter"].includes(r)) return "dashboard/service-requests.html";
    if (["maintenance", "maintenance_technician"].includes(r)) return "dashboard/maintenance.html";
    if (["inventory_clerk", "storekeeper"].includes(r)) return "dashboard/inventory.html";
    if (r === "procurement_officer") return "dashboard/procurement.html";
    /* Security officers clock in and pick up guest requests; they are not
       admitted to index.html, so landing them there bounced them straight to
       /403.html after a successful sign-in. */
    if (r === "security") return "dashboard/workforce.html";
    if (["accounts_manager", "accountant"].includes(r)) return "dashboard/payments.html";
    if (["hr_manager", "hr_officer"].includes(r)) return "dashboard/workforce.html";
    return "dashboard/index.html";
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
    state.capabilities = [];
    state.capabilitiesLoaded = false;
    return state.user;
  }

  function clearSession() {
    try { sessionStorage.removeItem(SESSION_KEY); } catch (_) {}
    JONE.storage.remove(KEY);
    state.user = null; state.role = null; state.permissions = [];
    state.capabilities = []; state.capabilitiesLoaded = false;
  }

  function getSessionCred() {
    try { return JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null"); } catch (_) { return null; }
  }

  function isAuthenticated() {
    try { return !!(sessionStorage.getItem(SESSION_KEY) && getSessionCred() && getSessionCred().access); }
    catch (_) { return !!JONE.storage.get(KEY, null); }
  }

  function hasRole(minRole) {
    // Legacy hierarchy for the existing front-desk/management shell. Department
    // roles stay outside it and must pass exact-role or capability checks.
    if (!state.role) return false;
    const order = {
      guest: 0, receptionist: 1, front_desk_supervisor: 1, staff: 1,
      manager: 2, general_manager: 2, admin: 3,
    };
    return (order[state.role] != null ? order[state.role] : -1) >= (order[minRole] != null ? order[minRole] : 99);
  }

  // Exact operational-role visibility guard. It is deliberately separate from
  // hasRole(): HOUSEKEEPING / MAINTENANCE / INVENTORY_CLERK do not
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

  let capabilityRequest = null;
  function loadCapabilities(options) {
    options = options || {};
    if (state.capabilitiesLoaded && !options.force) return Promise.resolve(state.capabilities.slice());
    if (capabilityRequest) return capabilityRequest;
    if (!window.API || typeof window.API.get !== "function") return Promise.resolve([]);
    /* One source of truth for the path. A missing key is a configuration
       defect and must surface as such, not be papered over with a hard-coded
       route that can drift from config/urls.py. */
    const endpoint = window.APP_CONFIG && window.APP_CONFIG.API_ENDPOINTS &&
      window.APP_CONFIG.API_ENDPOINTS.staffCapabilities;
    if (!endpoint) {
      return Promise.reject(new Error("The capabilities endpoint is not configured."));
    }
    capabilityRequest = window.API.get(endpoint).then((response) => {
      const payload = response && response.data || {};
      state.capabilities = Array.isArray(payload.capabilities) ? payload.capabilities.slice() : [];
      state.capabilitiesLoaded = true;
      return state.capabilities.slice();
    }).catch((error) => {
      // Role-based UX remains a fallback only when the capability read fails;
      // all protected resources continue to use server-side capability checks.
      state.capabilities = [];
      state.capabilitiesLoaded = false;
      throw error;
    }).finally(() => { capabilityRequest = null; });
    return capabilityRequest;
  }

  function hasCapability(code) {
    return !!(state.capabilitiesLoaded && state.capabilities.includes(String(code || "")));
  }

  function hasAnyCapability(codes) {
    return Array.isArray(codes) && codes.some(hasCapability);
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
     loops (the API layer only retries a single time).
     Single-flight promise deduplication: concurrent callers awaiting a 401
     refresh share the same in-flight refreshPromise so rotated refresh tokens
     are never submitted twice or prematurely rejected as false. */
  let refreshPromise = null;
  async function refreshAccess() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      try {
        const cred = getSessionCred();
        if (!cred || !cred.refresh) return false;
        const deskKey = receptionistDesktopKey();
        const res = await window.API.refreshTokenCall(cred.refresh, deskKey ? { headers: { "X-JONE-Desktop-Key": deskKey } } : {});
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
        refreshPromise = null;
      }
    })();
    return refreshPromise;
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

  /* Receptionist Desktop enrolment. The desk key is issued by an administrator
     and kept in this browser only; the server stores just its digest. Staff
     (other than management) can sign in only when this key is present. */
  const DESKTOP_KEY_STORAGE = "jone.receptionistDesktopKey";
  function receptionistDesktopKey() {
    try { return localStorage.getItem(DESKTOP_KEY_STORAGE) || ""; } catch (_) { return ""; }
  }
  function setReceptionistDesktopKey(value) {
    const key = String(value || "").trim();
    try {
      if (key) localStorage.setItem(DESKTOP_KEY_STORAGE, key);
      else localStorage.removeItem(DESKTOP_KEY_STORAGE);
    } catch (_) { return false; }
    return true;
  }

  async function login(email, password, extra) {
    const key = receptionistDesktopKey();
    const subCtx = getSubdomainContext();
    const headers = {};
    if (key) headers["X-JONE-Desktop-Key"] = key;
    if (subCtx && subCtx.subdomain) headers["X-JONE-Department"] = subCtx.subdomain;
    const opts = Object.keys(headers).length ? { headers: headers } : {};
    const body = Object.assign({ email: email, password: password }, extra || {});
    if (subCtx && subCtx.subdomain && !body.department) body.department = subCtx.subdomain;
    const res = await window.API.login(body, opts);
    storeSession(res.data);
    setAPITokenProvider();
    return res.data;
  }

  async function loginWithAccessCode(accessCode, extra) {
    const subCtx = getSubdomainContext();
    const headers = {};
    if (subCtx && subCtx.subdomain) headers["X-JONE-Department"] = subCtx.subdomain;
    const opts = Object.keys(headers).length ? { headers: headers } : {};
    const body = Object.assign({ access_code: String(accessCode || "").trim() }, extra || {});
    if (subCtx && subCtx.subdomain && !body.department) body.department = subCtx.subdomain;
    const res = await window.API.login(body, opts);
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
    const subCtx = getSubdomainContext();
    if (subCtx && !isAuthorizedForSubdomain(state.user || state.role, subCtx)) {
      location.replace("/403.html");
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

  /* Sign-in failures are NOT interchangeable, and reporting the wrong one sends
     the person to the wrong fix: "we couldn't reach our servers" makes a
     receptionist check their connection when the real answer was "this device
     is not the Front Desk", and an authorization problem must never be dressed
     up as a network problem. Each condition below gets its own sentence; the
     backend's own authored message wins whenever it named the reason, and none
     of these strings reveals credentials, tokens or server internals. */
  const SIGN_IN_MESSAGES = {
    credentials: "Incorrect email or password.",
    restricted: "Staff sign-in is available only from the Receptionist Desktop. Ask the front desk to sign you in, or contact an administrator.",
    permission: "Your account is not allowed to sign in here. Contact an administrator.",
    inactive: "This account is inactive or disabled. Contact an administrator.",
    csrf: "Your sign-in form has expired. Reload the page and try again.",
    rateLimited: "Too many sign-in attempts. Please wait a moment and try again.",
    server: "Our server could not complete the sign-in just now. Please try again in a moment.",
    timeout: "The sign-in request is taking longer than expected. Please try again.",
    offline: "You appear to be offline. Reconnect and try again.",
    network: "We couldn't reach our servers. Your connection or our service may be unavailable — please try again.",
    unexpected: "The server sent a response we couldn't read. Please try again.",
    fallback: "Sign-in failed. Please try again."
  };

  function signInErrorMessage(err) {
    const status = Number(err && err.status) || 0;
    const kind = String((err && err.kind) || "");
    const code = String((err && err.code) || "").toUpperCase();
    const serverMessage = typeof (err && err.message) === "string" ? err.message.trim() : "";
    // 1. Reasons only the server knows, in the order that matters.
    if (code === "STAFF_SIGN_IN_RESTRICTED") return serverMessage || SIGN_IN_MESSAGES.restricted;
    if (/INACTIVE|DISABLED|SUSPENDED/.test(code)) return serverMessage || SIGN_IN_MESSAGES.inactive;
    if (/CSRF/.test(code)) return SIGN_IN_MESSAGES.csrf;
    if (code === "RATE_LIMITED" || status === 429) return serverMessage || SIGN_IN_MESSAGES.rateLimited;
    // 2. HTTP answers. 401 is deliberately identical for a wrong password and an
    //    account that may not sign in, so this page never enumerates accounts.
    if (status === 401) return SIGN_IN_MESSAGES.credentials;
    if (status === 403) return serverMessage || SIGN_IN_MESSAGES.permission;
    if (status === 400 || status === 422) return serverMessage || SIGN_IN_MESSAGES.fallback;
    if (status >= 500) return serverMessage || SIGN_IN_MESSAGES.server;
    // 3. No HTTP answer at all — the only group that may say "network".
    if (kind === "offline") return SIGN_IN_MESSAGES.offline;
    if (kind === "timeout") return serverMessage || SIGN_IN_MESSAGES.timeout;
    if (kind === "network") return serverMessage || SIGN_IN_MESSAGES.network;
    if (kind === "parse") return SIGN_IN_MESSAGES.unexpected;
    if (!status) return serverMessage || SIGN_IN_MESSAGES.unexpected;
    return serverMessage || SIGN_IN_MESSAGES.fallback;
  }

  function bindLoginForm(formSel, opts = {}) {
    const form = document.querySelector(formSel);
    if (!form) return;
    /* Each attempt starts from a clean page state: a message left over from the
       previous failure must never be mistaken for this attempt's result, and a
       refusal's notice must be cleared the moment the person tries again. */
    function resetAttempt() {
      const errBox = form.querySelector("[data-form-error]");
      if (errBox) { errBox.textContent = ""; errBox.style.display = "none"; }
      const notice = form.querySelector("[data-desktop-notice]");
      if (notice) { notice.textContent = ""; notice.style.display = "none"; }
      if (typeof opts.onReset === "function") { try { opts.onReset(); } catch (_) {} }
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      resetAttempt();
      const btn = form.querySelector("[type=submit]");
      // One submission in flight at a time (the guard is released below for
      // EVERY outcome, so a failure never leaves the form unsubmittable).
      if (btn && !JONE.guardSubmit(btn)) return;
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
        if (!next) next = isStaffRole(session && session.user && session.user.role)
          ? defaultStaffLanding(session && session.user && session.user.role) : "index.html";
        location.href = next;
      } catch (err) {
        // The refusal carries the address the request came from (err.data.errors),
        // which is the only thing an administrator needs in order to register the
        // desk. Pass the error through so the page can show it — and never let a
        // page callback turn a backend answer into a silent, unreported failure.
        if (err && err.code === "STAFF_SIGN_IN_RESTRICTED" && typeof opts.onRestricted === "function") {
          try { opts.onRestricted(err); } catch (_) {}
        }
        const msg = signInErrorMessage(err);
        try { JONE.ui.toast(msg, "error"); } catch (_) {}
        const errBox = form.querySelector("[data-form-error]");
        if (errBox) { errBox.textContent = msg; errBox.style.display = "block"; }
      } finally {
        // Released whatever happened: a refused credential, a 500, a timeout, a
        // thrown callback or an unreadable response. This is what keeps the next
        // person able to sign in without reloading the page. On success the page
        // is already navigating, so releasing here is harmless.
        if (btn) JONE.releaseGuard(btn);
      }
    });
  }

  window.Auth = {
    ROLES, OPERATIONAL_ROLES, state, login, loginWithAccessCode, logout, guard, hasRole, hasAnyRole, can,
    isAuthenticated, restore, refreshProfile, onUnauthorized,
    bindLoginForm, clearSession, refreshAccess, loadCapabilities,
    receptionistDesktopKey, setReceptionistDesktopKey,
    hasCapability, hasAnyCapability,
    isStaffRole, defaultStaffLanding,
    getSubdomainContext, isAuthorizedForSubdomain
  };
  window.JONE = window.JONE || {};
})();
