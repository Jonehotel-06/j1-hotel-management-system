/* Ledger preview: server-calculated amounts only; existing API/auth conventions. */
(function () {
  "use strict";
  if (!window.__ledgerAllowed) return;
  const logout = document.querySelector("[data-logout]");
  if (logout) logout.addEventListener("click", () => window.Auth.logout());
  const ROOT = "/api/admin/finance/rebuild/";
  const $ = (selector) => document.querySelector(selector);
  const start = $("#ledger-start"), end = $("#ledger-end"), reportType = $("#ledger-report-type");
  const account = $("#ledger-account"), accountWrap = $("#ledger-account-wrap");
  const status = $("#ledger-report-status"), tbody = $("#ledger-table-body"), thead = $("#ledger-table-head");
  const totals = $("#ledger-report-totals"), cards = $("#ledger-cards"), warning = $("#ledger-warning");
  let page = 1, reportCount = 0, loadingToken = 0;
  const pageSize = 50;
  const formatMoney = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat("en-NG", {style: "currency", currency: "NGN", minimumFractionDigits: 2}).format(number) : "—";
  };
  const pretty = (key) => String(key).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const lagosToday = () => {
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone: "Africa/Lagos", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date());
    const map = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${map.year}-${map.month}-${map.day}`;
  };
  const moveDate = (value, delta) => {
    const d = new Date(value + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + delta); return d.toISOString().slice(0, 10);
  };
  function period(kind) {
    const today = lagosToday();
    end.value = today;
    start.value = kind === "today" ? today : kind === "week" ? moveDate(today, -6) : today.slice(0, 8) + "01";
    document.querySelectorAll("[data-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.period === kind)));
  }
  function params() {
    return {start: start.value, end: end.value, currency: "NGN"};
  }
  function reportParams() {
    const result = params();
    if (reportType.value === "general-ledger") { result.account = account.value; result.page = page; result.page_size = pageSize; }
    return result;
  }
  function setError(node, message) { node.textContent = message || "The report could not be loaded. Try again."; }
  async function loadOverview() {
    cards.setAttribute("aria-busy", "true");
    document.querySelectorAll("[data-metric]").forEach((node) => { node.textContent = "—"; node.classList.add("ledger-skeleton"); });
    warning.textContent = "";
    try {
      const result = await API.get(ROOT + "overview/", {params: params()});
      const data = result.data || {};
      document.querySelectorAll("[data-metric]").forEach((node) => {
        node.textContent = formatMoney(data[node.dataset.metric]); node.classList.remove("ledger-skeleton");
      });
      warning.textContent = (data.warnings || []).join(" ");
    } catch (error) {
      document.querySelectorAll("[data-metric]").forEach((node) => { node.textContent = "—"; node.classList.remove("ledger-skeleton"); });
      setError(warning, error.message);
    } finally { cards.setAttribute("aria-busy", "false"); }
  }
  async function loadAccounts() {
    try {
      const response = await API.get(ROOT + "accounts/");
      const accounts = (response.data && response.data.accounts) || [];
      account.replaceChildren();
      accounts.forEach((item) => {
        const option = document.createElement("option"); option.value = item.code; option.textContent = `${item.code} — ${item.label}`; account.appendChild(option);
      });
    } catch (error) { setError(status, error.message); }
  }
  function renderTotals(data) {
    totals.replaceChildren();
    const dataTotals = data.totals || {};
    Object.entries(dataTotals).forEach(([key, value]) => {
      const badge = document.createElement("span"); badge.className = "ledger-total";
      badge.appendChild(document.createTextNode(pretty(key)));
      const amount = document.createElement("strong"); amount.textContent = formatMoney(value); badge.appendChild(amount); totals.appendChild(badge);
    });
    if (data.balanced !== undefined) {
      const badge = document.createElement("span"); badge.className = "ledger-total"; badge.textContent = data.balanced ? "Ledger balances" : "Difference requires review"; totals.appendChild(badge);
    }
    if (data.report === "general-ledger") {
      [["Opening", data.opening_debit_balance], ["Debits", data.period_debit], ["Credits", data.period_credit], ["Closing", data.closing_debit_balance]].forEach(([label, value]) => {
        const badge = document.createElement("span"); badge.className = "ledger-total"; badge.textContent = label + " ";
        const strong = document.createElement("strong"); strong.textContent = formatMoney(value); badge.appendChild(strong); totals.appendChild(badge);
      });
    }
  }
  function renderTable(data) {
    thead.replaceChildren(); tbody.replaceChildren();
    const rows = data.records || [];
    if (!rows.length) { status.textContent = "No posted ledger lines for this selection."; return; }
    const columns = Object.keys(rows[0]).filter((key) => key !== "mapped");
    const headRow = document.createElement("tr");
    columns.forEach((column) => { const th = document.createElement("th"); th.scope = "col"; th.textContent = pretty(column); headRow.appendChild(th); });
    thead.appendChild(headRow);
    const moneyColumns = new Set(["amount", "opening", "closing", "debit", "credit", "increases", "decreases", "opening_debit", "opening_credit", "period_debit", "period_credit", "closing_debit", "closing_credit", "running_debit_balance"]);
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      columns.forEach((column) => {
        const td = document.createElement("td");
        if (column === "code" && row.code && row.code !== "UN_CLOSED_EARNINGS") {
          const link = document.createElement("a"); link.href = "#ledger-report"; link.textContent = row.code;
          link.addEventListener("click", () => { reportType.value = "general-ledger"; account.value = row.code; toggleAccount(); page = 1; loadReport(); });
          td.appendChild(link);
        } else { td.textContent = moneyColumns.has(column) ? formatMoney(row[column]) : String(row[column] == null ? "" : row[column]); }
        tr.appendChild(td);
      }); tbody.appendChild(tr);
    });
    status.textContent = (data.warnings || []).join(" ");
  }
  function toggleAccount() { accountWrap.hidden = reportType.value !== "general-ledger"; }
  function updatePages(data) {
    const isLedger = reportType.value === "general-ledger";
    reportCount = data.count || 0;
    $("#ledger-prev").disabled = !isLedger || page <= 1;
    $("#ledger-next").disabled = !isLedger || page * pageSize >= reportCount;
    $("#ledger-page-label").textContent = isLedger ? `Page ${page} · ${reportCount} lines` : "";
  }
  async function loadReport() {
    if (!start.value || !end.value || end.value < start.value) { setError(status, "Choose a valid date range."); return; }
    if (reportType.value === "general-ledger" && !account.value) { setError(status, "Choose an account."); return; }
    const token = ++loadingToken;
    status.textContent = "Loading ledger records…"; thead.replaceChildren(); tbody.replaceChildren(); totals.replaceChildren();
    try {
      const result = await API.get(ROOT + "reports/" + reportType.value + "/", {params: reportParams()});
      if (token !== loadingToken) return;
      renderTotals(result.data); renderTable(result.data); updatePages(result.data);
    } catch (error) { if (token === loadingToken) setError(status, error.message); }
  }
  document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => { period(button.dataset.period); page = 1; loadOverview(); loadReport(); }));
  $("#ledger-apply").addEventListener("click", () => { if (!start.value || !end.value || end.value < start.value) { setError(status, "Choose a valid date range."); return; } page = 1; loadOverview(); loadReport(); });
  reportType.addEventListener("change", () => { toggleAccount(); page = 1; });
  $("#ledger-load-report").addEventListener("click", () => { page = 1; loadReport(); });
  $("#ledger-prev").addEventListener("click", () => { if (page > 1) { page -= 1; loadReport(); } });
  $("#ledger-next").addEventListener("click", () => { if (page * pageSize < reportCount) { page += 1; loadReport(); } });
  document.querySelectorAll("[data-open-report]").forEach((link) => link.addEventListener("click", () => { reportType.value = link.dataset.openReport; toggleAccount(); page = 1; loadReport(); }));
  document.querySelectorAll("[data-export]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const query = reportType.value === "general-ledger" ? {account: account.value, page: 1, page_size: 5000} : {};
      await API.downloadReport(ROOT + "reports/" + reportType.value + "/", button.dataset.export, {start: start.value, end: end.value, params: query});
    } catch (error) { setError(status, error.message); }
  }));
  period("month"); toggleAccount(); loadAccounts().then(() => { loadOverview(); loadReport(); });
})();
