/* js/audit-detail.js */
/* ==========================================================================
   J-ONE HOTEL & LODGE — shared read-only audit-event renderer.

   One implementation of the audit-event body, used by
   - dashboard/audit-logs.html  (opens the event in the shared modal, so the
     operator keeps their filter, page and keyboard position), and
   - dashboard/audit-log-details.html (the standalone, linkable page).

   Read-only by design: it renders the immutable record returned by
   GET /api/admin/audit/<id>/ and exposes no edit, delete or retry control.
   ========================================================================== */
(function (window) {
  "use strict";

  function esc(value) {
    return window.JONE && JONE.esc ? JONE.esc(value) : String(value == null ? "" : value);
  }

  function fieldRow(label, value) {
    return '<div class="field"><div class="field-label">' + esc(label) + '</div>' +
      '<div class="audit-value">' + esc(value == null || value === "" ? "\u2014" : value) + "</div></div>";
  }

  function pretty(value) {
    try {
      return JSON.stringify(value || {}, null, 2);
    } catch (_) {
      return "{}";
    }
  }

  /* The complete, unmodified record body. */
  function render(record) {
    var a = record || {};
    return '<div class="form-grid fg-2">' +
        [
          ["Event", a.action ? String(a.action).replace(/_/g, " ") : null],
          ["Time", a.created_at ? JONE.formatDateTime(a.created_at) : null],
          ["Actor", a.actor_name || a.actor_email || "System"],
          ["Actor email", a.actor_email],
          ["Object", ((a.object_type || "") + " " + (a.object_id || "")).trim()],
          ["IP address", a.ip_address],
          ["Summary", a.summary]
        ].map(function (row) { return fieldRow(row[0], row[1]); }).join("") +
      "</div>" +
      '<div class="form-grid fg-2" style="margin-top:1rem;">' +
        '<div><h3 style="font-size:var(--fs-md);margin-bottom:0.5rem;">Changes</h3>' +
        '<pre class="audit-json">' + esc(pretty(a.changes)) + "</pre></div>" +
        '<div><h3 style="font-size:var(--fs-md);margin-bottom:0.5rem;">Metadata</h3>' +
        '<pre class="audit-json">' + esc(pretty(a.metadata)) + "</pre></div>" +
      "</div>";
  }

  function loadingBlock() {
    return '<div class="loading-block"><div class="spinner" role="status"></div>' +
      '<p class="muted">Loading audit event&hellip;</p></div>';
  }

  function errorBlock(title, message) {
    return '<div class="empty-state"><span data-icon="alertTriangle" data-size="34"></span>' +
      '<h3>' + esc(title) + "</h3><p>" + esc(message || "Please try again.") + "</p></div>";
  }

  function errorCopy(err) {
    if (err && err.status === 404) return ["Audit event not found", "This audit event does not exist."];
    if (err && err.status === 403) return ["Access denied", "Only administrators can view audit events."];
    return ["Audit event could not be loaded", (err && err.message) || "Please try again in a moment."];
  }

  /* Open one audit event in the shared accessible modal (focus trap, Escape to
     close, focus returned to the row that opened it by JONE.ui.modal). */
  function open(id, options) {
    options = options || {};
    if (!id || !window.JONE || !JONE.ui || !JONE.ui.modal) return null;
    var body = document.createElement("div");
    body.innerHTML = loadingBlock();

    var footer = document.createElement("div");
    footer.style.display = "flex";
    footer.style.gap = "0.75rem";
    footer.style.justifyContent = "flex-end";
    footer.style.flexWrap = "wrap";
    var pageLink = document.createElement("a");
    pageLink.className = "btn btn-sm btn-outline";
    pageLink.href = "audit-log-details.html?id=" + encodeURIComponent(id);
    pageLink.textContent = "Open full page";
    footer.appendChild(pageLink);

    JONE.ui.modal.open({
      title: options.title || "Audit event",
      body: body,
      size: "modal-lg",
      footer: footer
    });

    window.API.getOne("auditLogs", id).then(function (res) {
      var record = (res && res.data) || {};
      var panel = body.closest(".modal-panel");
      var heading = panel ? panel.querySelector(".modal-head h3") : null;
      if (heading && record.action) heading.textContent = String(record.action).replace(/_/g, " ");
      body.innerHTML = render(record);
    }).catch(function (err) {
      var copy = errorCopy(err);
      body.innerHTML = errorBlock(copy[0], copy[1]);
      if (window.JONE.icons && JONE.icons.inject) JONE.icons.inject(body);
    });
    return true;
  }

  window.JONE = window.JONE || {};
  window.JONE.auditDetail = { render: render, open: open, loadingBlock: loadingBlock, errorBlock: errorBlock, errorCopy: errorCopy };
})(window);
