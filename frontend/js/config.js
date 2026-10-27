/* js/config.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — global app configuration.
   Centralized so API endpoints are never scattered across files.
   ========================================================================== */

(function () {
  // runtime-config.js loads before this file. Keep this defensive so direct
  // script users and test harnesses still receive portable same-origin defaults.
  var runtimeConfig = window.__APP_CONFIG__;
  if (!runtimeConfig || typeof runtimeConfig !== "object" || Array.isArray(runtimeConfig)) {
    runtimeConfig = {};
  }

  function normalizeApiBase(value) {
    return typeof value === "string" ? value.trim().replace(/\/+$/, "") : "";
  }

  // True when the frontend is opened on the dev machine (e.g. Live Server :5500).
  function isLocalHost() {
    return ["localhost", "127.0.0.1", "[::1]", ""].indexOf(window.location.hostname) !== -1;
  }

  // Django dev server used when running locally and nothing else is configured.
  var LOCAL_API_BASE_URL = "http://127.0.0.1:8000";
  var CONFIG_ERROR = "";

  function validateApiBase(rawValue) {
    if (rawValue === undefined || rawValue === null) return "";
    if (typeof rawValue !== "string") {
      CONFIG_ERROR = "[J-ONE CONFIG] Invalid API_BASE_URL type; expected a string origin.";
      if (typeof console !== "undefined" && console.error) console.error(CONFIG_ERROR);
      return "";
    }
    var trimmed = normalizeApiBase(rawValue);
    if (trimmed === "") return "";
    if (!/^https?:\/\/[^/\s]+$/i.test(trimmed)) {
      CONFIG_ERROR = "[J-ONE CONFIG] Invalid API_BASE_URL (" + trimmed + "): expected scheme + host without path.";
      if (typeof console !== "undefined" && console.error) console.error(CONFIG_ERROR);
      return "";
    }
    if (window.location && window.location.protocol === "https:" && /^http:\/\//i.test(trimmed)) {
      CONFIG_ERROR = "[J-ONE CONFIG] Insecure HTTP API_BASE_URL (" + trimmed + ") rejected on an HTTPS origin.";
      if (typeof console !== "undefined" && console.error) console.error(CONFIG_ERROR);
      return "";
    }
    return trimmed;
  }

  function resolveApiBase(value) {
    var validated = validateApiBase(value);
    if (validated) return validated;
    if (CONFIG_ERROR) return "";
    return isLocalHost() ? LOCAL_API_BASE_URL : "";
  }

  var API_BASE_URL = resolveApiBase(runtimeConfig.API_BASE_URL);

  window.APP_CONFIG = {
    API_BASE_URL: API_BASE_URL,
    CONFIG_ERROR: CONFIG_ERROR,
    HOTEL_SLUG: "j-one-hotel-lodge",
    PAGE_DEFAULT: 1,
    PAGE_SIZE: 10,

  /* Booking flow defaults (backed by hotel settings from the API at runtime) */
  DEFAULT_ADULTS: 2,
  DEFAULT_CHILDREN: 0,
  MIN_STAY_NIGHTS: 1,
  CURRENCY: "NGN",

  /* Storage keys */
  STORAGE: {
    THEME: "jone.theme",
    AUTH: "jone.auth",
    BOOKING: "jone.booking.draft",
    SESSION: "jone.session",
    TERMINAL: "jone.terminal.reference"
  },

  /* Fingerprint capture source for this deployment.

     "hardware" (default, required in production): probes come only from the
     department reader bridge exposed as window.JoneFingerprintReader.capture().
     Without a reader the biometric pages refuse and point the operator at the
     supervised fallback — the browser never invents a fingerprint.

     "simulated": lab/demo only. A deployment sets it through the
     deployment-owned runtime config (window.__APP_CONFIG__.BIOMETRIC_READER_MODE)
     so the value does not travel with the static bundle. It must never be
     enabled where real attendance is recorded, because a simulated template is
     indistinguishable from a captured one to the API. */
  BIOMETRIC_READER_MODE: (typeof runtimeConfig.BIOMETRIC_READER_MODE === "string"
    && runtimeConfig.BIOMETRIC_READER_MODE.trim())
    ? runtimeConfig.BIOMETRIC_READER_MODE.trim().toLowerCase()
    : "hardware",

  /* Department subdomain routing map (Priority 5 / Part F2) */
  DEPARTMENT_SUBDOMAINS: {
    manager: {
      subdomain: "manager",
      code: "MANAGEMENT",
      label: "Management",
      portalTitle: "Executive & General Management Portal",
      landingPath: "/dashboard/index.html",
      allowedRoles: ["admin", "general_manager", "manager", "hr_manager", "hr_officer"]
    },
    accountant: {
      subdomain: "accountant",
      code: "ACCOUNTS",
      label: "Accounts & Finance",
      portalTitle: "Accounts & Financial Control Portal",
      landingPath: "/dashboard/accounting.html",
      allowedRoles: ["admin", "accounts_manager", "accountant", "general_manager", "manager"]
    },
    admin: {
      subdomain: "admin",
      code: "ADMIN_PROCUREMENT",
      label: "Administration",
      portalTitle: "System Administration Portal",
      landingPath: "/dashboard/staff.html",
      allowedRoles: ["admin"]
    },
    procurement: {
      subdomain: "procurement",
      code: "ADMIN_PROCUREMENT",
      label: "Administration & Procurement",
      portalTitle: "Procurement & Vendor Operations Portal",
      landingPath: "/dashboard/procurement.html",
      allowedRoles: ["admin", "procurement_officer", "manager", "general_manager", "storekeeper"]
    },
    frontdesk: {
      subdomain: "frontdesk",
      code: "FRONT_DESK",
      label: "Front Desk / Reception",
      portalTitle: "Front Desk & Reception Operations Portal",
      landingPath: "/dashboard/bookings.html",
      allowedRoles: ["admin", "front_desk_supervisor", "receptionist", "general_manager", "manager"]
    },
    kitchen: {
      subdomain: "kitchen",
      code: "KITCHEN",
      label: "Kitchen",
      portalTitle: "Kitchen & Culinary Production Portal",
      landingPath: "/dashboard/pos.html",
      allowedRoles: ["admin", "kitchen_manager", "chef", "kitchen_assistant", "general_manager", "manager"]
    },
    restaurant: {
      subdomain: "restaurant",
      code: "RESTAURANT_BAR",
      label: "Restaurant Service",
      portalTitle: "Restaurant & Dining Service Portal",
      landingPath: "/dashboard/pos.html",
      allowedRoles: ["admin", "restaurant_manager", "waiter", "general_manager", "manager"]
    },
    bar: {
      subdomain: "bar",
      code: "RESTAURANT_BAR",
      label: "Bar Service",
      portalTitle: "Bar & Beverage Service Portal",
      landingPath: "/dashboard/pos.html",
      allowedRoles: ["admin", "bar_manager", "bartender", "waiter", "general_manager", "manager"]
    },
    housekeeping: {
      subdomain: "housekeeping",
      code: "HOUSEKEEPING",
      label: "Housekeeping",
      portalTitle: "Housekeeping & Room Readiness Portal",
      landingPath: "/dashboard/housekeeping.html",
      allowedRoles: ["admin", "housekeeping_manager", "housekeeper", "housekeeping", "general_manager", "manager"]
    },
    porter: {
      subdomain: "porter",
      code: "PORTERING",
      label: "Portering & Guest Assistance",
      portalTitle: "Porters & Guest Services Portal",
      landingPath: "/dashboard/service-requests.html",
      allowedRoles: ["admin", "porter", "maintenance", "maintenance_technician", "security", "general_manager", "manager"]
    },
    store: {
      subdomain: "store",
      code: "STORES_INVENTORY",
      label: "Stores & Inventory",
      portalTitle: "Central Stores & Inventory Control Portal",
      landingPath: "/dashboard/inventory.html",
      allowedRoles: ["admin", "storekeeper", "inventory_clerk", "general_manager", "manager"]
    }
  },

  /* ========================================================================
     BACKEND CONTRACT — staff/dashboard resource base paths.
     VERIFIED against the actual Django backend (live run + its test suite +
     docs/FRONTEND_CONTRACT.md). Keep in sync with config/urls.py on the
     backend. All lists are paginated with the shared envelope; detail paths
     are derived as {base}/{id}/ by js/api.js.

     Guest-facing endpoints (auth, rooms, availability, quote, bookings,
     payments, notifications, enquiries) are wired directly in js/api.js —
     they are part of the public contract, not deploy-time configuration.
     ======================================================================== */
  API_ENDPOINTS: {
    /* Hotel operations (staff, /api/admin/ namespace) */
    bookings:      "/api/admin/bookings/",        // list/create; detail {id|ref}; actions via API.checkInBooking() etc.
    booking:       "/api/admin/bookings/",        // detail view alias (booking-details page)
    guests:        "/api/admin/guests/",
    guestDiscounts:"/api/admin/guest-discounts/", // individual guest discounts (manager write, staff read)
    occupancy:     "/api/admin/bookings/calendar/", // month occupancy grid (per room, per date)
    missedBookings:"/api/admin/bookings/missed/",   // guests who never arrived
    lateArrivals:"/api/admin/bookings/late-arrivals/", // missed night 1, can still check in
    rooms:         "/api/admin/rooms/",           // physical rooms (status/housekeeping)
    roomTypes:     "/api/admin/room-types/",      // catalog CRUD + images upload
    roomImages:    "/api/admin/room-images/",      // room-type image detail (PATCH/DELETE)
    amenities:     "/api/admin/amenities/",
    payments:      "/api/admin/payments/",        // list; detail {id|ref}; record via API.recordPayment()
    refunds:       "/api/admin/payments/refunds/", // Paystack refund lifecycle rows (read-only list/detail)
    receipts:      "/api/admin/payments/",        // receipts view = payment records
    enquiries:     "/api/admin/enquiries/",       // includes cancellation/refund review actions
    auditLogs:     "/api/admin/audit-logs/",      // ADMIN, read-only
    auditLogActions: "/api/admin/audit-logs/actions/",  // ADMIN, filter choices
    reviews:       "/api/admin/reviews/",         // ADMIN-only guest reviews (private)
    users:         "/api/admin/users/",           // ADMIN
    workstations:  "/api/admin/users/terminals/", // capability-gated; attribution only
    staffCapabilities: "/api/auth/capabilities/", // current account's server-resolved UI capability hints
    clientAddress: "/api/auth/client-address/",   // address the server sees THIS browser from
    authProfile:   "/api/auth/profile/",          // read/update the signed-in account
    passwordChange:"/api/auth/password/change/",  // change the signed-in account's password
    settings:      "/api/admin/settings/",        // GET manager+, PATCH admin-only
    stats:         "/api/admin/dashboard/",       // dashboard KPIs
    facilities:    "/api/admin/facilities/",
    policies:      "/api/admin/policies/",
    offers:        "/api/admin/offers/",
    gallery:       "/api/admin/gallery/",

    /* POS & room service (capability-gated server-side) */
    posMenu:             "/api/admin/pos/menu/",
    posBarcodeLookup:    "/api/admin/pos/menu/barcode-lookup/",
    posMenuCategories:   "/api/admin/pos/menu/categories/",
    posMenuItems:        "/api/admin/pos/menu/items/",
    posMenuModifiers:    "/api/admin/pos/menu/modifiers/",
    posRestaurantTables: "/api/admin/pos/restaurant-tables/",
    posRestaurantTableSessions: "/api/admin/pos/restaurant-table-sessions/",
    posRoomServiceStays: "/api/admin/pos/room-service-stays/",
    posOrders:           "/api/admin/pos/orders/",
    // Bar/restaurant unopened-item returns (delivered order → stock back →
    // final charge → eligible refund). Literal segments must stay ahead of the
    // order-detail route on the server, which the URLconf already guarantees.
    posOrderReturns:     "/api/admin/pos/orders/returns/",
    posOrderReturnEligible: "/api/admin/pos/orders/returns/eligible/",
    posKitchenTickets:   "/api/admin/pos/kitchen-tickets/",
    /* Cash sessions have ONE model (apps.finance.models.CashSession) with two
       distinct route surfaces on the backend, so both keys are deliberate:
         posCashSessions      → the cash-session actions (open / {ref}/close/)
                                mounted under the POS namespace;
         financeCashSessions  → the canonical LISTING/reconciliation
                                collection mounted under the finance namespace.
       The bare `GET /api/admin/pos/cash-sessions/` collection does not exist
       and is intentionally retired (D12) — nothing may list from
       `posCashSessions`. Always read sessions through `financeCashSessions`. */
    posCashSessions:     "/api/admin/pos/cash-sessions/",
    financeCashSessions: "/api/admin/finance/cash-sessions/",

    /* Guest-service and operations queues (capability-gated server-side). */
    serviceRequests:       "/api/admin/service-requests/",
    serviceQrLinks:        "/api/admin/service-qr-links/",
    serviceQrContext:      "/api/service-qr/context/",
    serviceQrRequests:     "/api/service-qr/requests/",
    housekeepingTasks:     "/api/admin/housekeeping/",
    maintenanceWorkOrders: "/api/admin/maintenance/",
    staffDirectory:        "/api/admin/users/directory/",
    permissionsMatrix:     "/api/admin/users/permissions-matrix/",
    myStaffProfile:        "/api/admin/staff-operations/my-profile/",
    staffPerformanceReviews: "/api/admin/staff-operations/performance-reviews/",
    staffProfiles:         "/api/admin/staff-operations/profiles/",
    staffFullProfile:      "/api/admin/staff-operations/staff/",
    departments:           "/api/admin/staff-operations/departments/",
    departmentDashboard:   "/api/admin/staff-operations/department-dashboard/",
    staffDisciplinary:     "/api/admin/staff-operations/disciplinary/",
    staffDocuments:        "/api/admin/staff-operations/documents/",
    /* Biometric enrollment is workstation-scoped: the desktop terminal key is
       the credential, and the server refuses the request without it. The paths
       used to be hard-coded inside js/biometrics.js, which bypassed the single
       endpoint map (D1 sweep). */
    fingerprintEnrollment: "/api/admin/staff-operations/fingerprint/enrollment/",
    fingerprintEnrollments:"/api/admin/staff-operations/fingerprint/enrollments/",
    fingerprintStaff:      "/api/admin/staff-operations/fingerprint/staff/",
    faceEnrollment:        "/api/admin/staff-operations/face/enrollment/",
    faceEnrollments:       "/api/admin/staff-operations/face/enrollments/",
    temporaryCredential:   "/api/admin/staff-operations/biometrics/temporary-credential/",
    receptionTerminal:     "/api/admin/staff-operations/biometrics/reception-terminal/",
    biometricDevices:      "/api/admin/staff-operations/biometrics/devices/",
    attendanceClock:       "/api/admin/staff-operations/attendance/clock/",
    leaveRequests:         "/api/admin/staff-operations/leave-requests/",
    shiftTemplates:        "/api/admin/staff-operations/shift-templates/",
    shifts:                "/api/admin/staff-operations/shifts/",
    attendance:            "/api/admin/staff-operations/attendance/",
    attendanceClock:       "/api/admin/staff-operations/attendance/clock/",
    leaveRequests:         "/api/admin/staff-operations/leave-requests/",
    payrollCompensation:   "/api/admin/staff-operations/payroll/compensation/",
    payrollStockShortages: "/api/admin/staff-operations/payroll/stock-shortages/",
    payrollLatenessPolicy: "/api/admin/staff-operations/payroll/lateness-policy/",
    payrollLatenessDeductions: "/api/admin/staff-operations/payroll/lateness-deductions/",
    payrollPeriods:       "/api/admin/staff-operations/payroll/periods/",
    /* Payroll (client) — the signed-in user's OWN approved payslips. */
    payrollPayslips:      "/api/admin/staff-operations/payroll/payslips/",
    myPayslips:           "/api/admin/staff-operations/payroll/my-payslips/",

    /* Inventory and procurement (capability-gated server-side). */
    inventoryLocations:           "/api/admin/inventory/locations/",
    inventoryItems:               "/api/admin/inventory/items/",
    inventoryMenuItems:           "/api/admin/inventory/menu-items/",
    inventoryBalances:            "/api/admin/inventory/balances/",
    /* Read-only forward projection (§ "product profitability / valuation").
       Nothing here posts money; the server labels the payload as a PROJECTION. */
    inventoryProfitability:       "/api/admin/inventory/profitability/",
    inventoryProfitabilityFilters:"/api/admin/inventory/profitability/filters/",
    inventoryMovements:           "/api/admin/inventory/movements/",
    inventoryRecipes:             "/api/admin/inventory/recipes/",
    inventoryConsumptionRequests: "/api/admin/inventory/consumption-requests/",
    inventoryProcessPendingConsumption: "/api/admin/inventory/consumption-requests/process-pending/",
    stockCounts:                  "/api/admin/inventory/stock-counts/",
    inventoryIssues:              "/api/admin/inventory/issues/",
    inventoryOpeningStock:        "/api/admin/inventory/opening-stock/",
    inventoryReturns:             "/api/admin/inventory/returns/",
    inventoryTransfers:           "/api/admin/inventory/transfers/",
    suppliers:                    "/api/admin/inventory/suppliers/",
    purchaseOrders:               "/api/admin/inventory/purchase-orders/",

    /* Reports (MANAGER+) — financial ledger summary stays separate from
       legacy booking/occupancy compatibility projections.

       The Accounting page reads four independent finance collections plus the
       ledger summary. Every key below MUST resolve to a real backend route
       (apps/finance/urls.py) — a missing key used to produce requests ending
       in "/undefined" and permanently blank accounting panels (D1). */
    financeSummary:      "/api/admin/finance/reports/summary/",
    financeTransactions: "/api/admin/finance/transactions/",   // immutable posted ledger headers
    financeExpenses:     "/api/admin/finance/expenses/",       // controlled expense drafts/submissions
    financeNightAudit:   "/api/admin/finance/reports/night-audit/",
    financeBalanceSheet: "/api/admin/finance/reports/balance-sheet/",
    financeCashFlow:     "/api/admin/finance/reports/cash-flow/",
    guestLaundry:        "/api/admin/service-requests/laundry/",
    financeArAging:      "/api/admin/finance/reports/ar-aging/",
    financeJournals:     "/api/admin/finance/journals/",
    financeChartOfAccounts: "/api/admin/finance/chart-of-accounts/",
    financeApprovals:    "/api/admin/finance/approvals/",      // sensitive-money approval requests
    /* Per-record drill-down. API.getOne appends the escaped reference. */
    financeExpenseDetail:"/api/admin/finance/expenses/",
    reportsRevenue:      "/api/admin/reports/revenue/", // retained for legacy consumers
    reportsOccupancy:    "/api/admin/reports/occupancy/",
    reportsBookings:     "/api/admin/reports/bookings/",
    reportsApprovals:    "/api/admin/reports/approvals/",   // approvals inbox: everything awaiting a decision

    /* Authoritative availability search (public; used by staff too) */
    availability:  "/api/rooms/availability/",

    /* Verified-email guest portal (HttpOnly session cookie, never staff JWT). */
    portalAccessRequest: "/api/portal/auth/request/",
    portalAccessConsume: "/api/portal/auth/consume/",
    portalLogout:        "/api/portal/auth/logout/",
    portalOverview:      "/api/portal/me/",
    portalFolios:        "/api/portal/folios/",
    portalRequests:      "/api/portal/requests/",
    portalMenu:          "/api/portal/catalog/menu/",
    portalServices:      "/api/portal/catalog/services/",

    /* Notifications (authenticated user, not admin-namespaced) */
    notifications: "/api/notifications/"
  }
};

  /*
   * Public runtime overrides are intentionally narrow and additive. In
   * particular, endpoint overrides merge into the checked-in contract instead
   * of replacing it, so configuring one deployment-specific endpoint cannot
   * erase the rest of the staff/public API map.
   */
  Object.keys(runtimeConfig).forEach(function (key) {
    if (key !== "API_ENDPOINTS") window.APP_CONFIG[key] = runtimeConfig[key];
  });

  // Re-resolve after the override merge so an empty API_BASE_URL in
  // runtime-config.js can't wipe out the localhost dev fallback.
  window.APP_CONFIG.API_BASE_URL = resolveApiBase(window.APP_CONFIG.API_BASE_URL);

  if (runtimeConfig.API_ENDPOINTS && typeof runtimeConfig.API_ENDPOINTS === "object") {
    Object.assign(window.APP_CONFIG.API_ENDPOINTS, runtimeConfig.API_ENDPOINTS);
  }
})();