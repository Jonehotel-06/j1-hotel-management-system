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
    return ["localhost", "127.0.0.1", ""].indexOf(window.location.hostname) !== -1;
  }

  // Django dev server used when running locally and nothing else is configured.
  var LOCAL_API_BASE_URL = "http://127.0.0.1:8000";

  var PRODUCTION_API_BASE_URL = "j1-hotel-management-system-production.up.railway.app";


  function resolveApiBase(value) {
    return normalizeApiBase(value) ||
      (isLocalHost() ? LOCAL_API_BASE_URL : PRODUCTION_API_BASE_URL);
  }

  var API_BASE_URL = resolveApiBase(runtimeConfig.API_BASE_URL);

  window.APP_CONFIG = {
    API_BASE_URL: API_BASE_URL,
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
    settings:      "/api/admin/settings/",        // GET manager+, PATCH admin-only
    stats:         "/api/admin/dashboard/",       // dashboard KPIs
    facilities:    "/api/admin/facilities/",
    policies:      "/api/admin/policies/",
    offers:        "/api/admin/offers/",
    gallery:       "/api/admin/gallery/",

    /* POS & room service (capability-gated server-side) */
    posMenu:             "/api/admin/pos/menu/",
    posMenuCategories:   "/api/admin/pos/menu/categories/",
    posMenuItems:        "/api/admin/pos/menu/items/",
    posMenuModifiers:    "/api/admin/pos/menu/modifiers/",
    posRestaurantTables: "/api/admin/pos/restaurant-tables/",
    posRestaurantTableSessions: "/api/admin/pos/restaurant-table-sessions/",
    posRoomServiceStays: "/api/admin/pos/room-service-stays/",
    posOrders:           "/api/admin/pos/orders/",
    posKitchenTickets:   "/api/admin/pos/kitchen-tickets/",
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
    staffProfiles:         "/api/admin/staff-operations/profiles/",
    shiftTemplates:        "/api/admin/staff-operations/shift-templates/",
    shifts:                "/api/admin/staff-operations/shifts/",
    attendance:            "/api/admin/staff-operations/attendance/",
    attendanceClock:       "/api/admin/staff-operations/attendance/clock/",
    leaveRequests:         "/api/admin/staff-operations/leave-requests/",
    payrollCompensation:   "/api/admin/staff-operations/payroll/compensation/",
    payrollTaxIdentities:  "/api/admin/staff-operations/payroll/tax-identities/",
    payrollStatutoryRules: "/api/admin/staff-operations/payroll/statutory-rules/",
    payrollPeriods:       "/api/admin/staff-operations/payroll/periods/",
    payrollPayslips:      "/api/admin/staff-operations/payroll/payslips/",

    /* Inventory and procurement (capability-gated server-side). */
    inventoryLocations:           "/api/admin/inventory/locations/",
    inventoryItems:               "/api/admin/inventory/items/",
    inventoryMenuItems:           "/api/admin/inventory/menu-items/",
    inventoryBalances:            "/api/admin/inventory/balances/",
    inventoryMovements:           "/api/admin/inventory/movements/",
    inventoryRecipes:             "/api/admin/inventory/recipes/",
    inventoryConsumptionRequests: "/api/admin/inventory/consumption-requests/",
    stockCounts:                  "/api/admin/inventory/stock-counts/",
    inventoryIssues:              "/api/admin/inventory/issues/",
    suppliers:                    "/api/admin/inventory/suppliers/",
    purchaseOrders:               "/api/admin/inventory/purchase-orders/",

    /* Reports (MANAGER+) — financial ledger summary stays separate from
       legacy booking/occupancy compatibility projections. */
    financeSummary:    "/api/admin/finance/reports/summary/",
    reportsRevenue:    "/api/admin/reports/revenue/", // retained for legacy consumers
    reportsOccupancy:  "/api/admin/reports/occupancy/",
    reportsBookings:   "/api/admin/reports/bookings/",

    /* Authoritative availability search (public; used by staff too) */
    availability:  "/api/rooms/availability/",

    /* Verified-email guest portal (opaque X-Portal-Session, never staff JWT). */
    portalAccessRequest: "/api/portal/auth/request/",
    portalAccessConsume: "/api/portal/auth/consume/",
    portalLogout:        "/api/portal/auth/logout/",
    portalOverview:      "/api/portal/me/",
    portalFolios:        "/api/portal/folios/",
    portalRequests:      "/api/portal/requests/",

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