/* Reusable room asset checks. Uses the existing authenticated API client. */
(function () {
  'use strict';
  if (!window.__housekeepingAllowed) return;
  const root = window.APP_CONFIG.API_ENDPOINTS.housekeepingTasks.replace(/\/?$/, '/') + 'assets/';
  const $ = id => document.getElementById(id);
  const esc = value => JONE.esc(String(value == null ? '' : value));
  let inspection = null, queuePage = 1, reportPage = 1, config = null, items = [];
  const message = text => { $('asset-message').textContent = text; };
  const error = e => message(e.message || 'Unable to complete request.');
  function pager(el, page, pagination, callback) {
    el.innerHTML = '';
    if (!pagination || pagination.total_pages <= 1) return;
    ['Previous', 'Next'].forEach((label, index) => {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'btn btn-sm btn-outline';
      b.textContent = label; b.disabled = index ? page >= pagination.total_pages : page <= 1;
      b.onclick = () => callback(page + (index ? 1 : -1)); el.appendChild(b);
    });
    el.appendChild(document.createTextNode(' Page ' + page + ' of ' + pagination.total_pages));
  }
  async function queue(page) {
    queuePage = page || 1;
    try {
      const r = await API.get(root + 'queue/', {params:{page:queuePage}});
      $('asset-queue').innerHTML = '<h3>Pending inspections</h3>' + (r.data.length ? r.data.map(a =>
        '<div class="asset-row"><strong>Room ' + esc(a.room) + '</strong> · ' + esc(a.room_type) + ' · Booking ' + esc(a.booking) + ' · ' + (a.phase === 'PRE' ? 'Pre-check-in' : 'Post-checkout') +
        (a.phase === 'POST' && !a.pre_completed ? ' — pre-check-in evidence missing; escalate' : '') +
        ' <button type="button" class="btn btn-sm btn-outline" data-start="' + Number(a.assignment_id) + '" data-phase="' + esc(a.phase) + '">Open checklist</button></div>').join('') : '<p>No pending inspections.</p>');
      pager($('asset-pager'), queuePage, r.pagination, queue);
    } catch (e) { error(e); }
  }
  function renderInspection() {
    const i = inspection;
    let html = '<hr><h3>' + (i.phase === 'PRE' ? 'Pre-check-in' : 'Post-checkout') + ' · Room ' + esc(i.room) + '</h3><p>Booking ' + esc(i.booking) + (i.stay ? ' · Stay ' + esc(i.stay) : '') + '</p>';
    if (i.completed_at) { $('asset-inspection').innerHTML = html + '<p>Already completed; this evidence is read-only.</p>'; return; }
    html += '<form id="asset-check-form">';
    i.checklist.forEach(x => {
      const d = i.draft[String(x.item_id)] || {};
      html += '<fieldset class="asset-card" data-item="' + Number(x.item_id) + '"><legend>' + esc(x.name) + '</legend><p>Expected: ' + Number(x.expected) + (i.phase === 'POST' ? ' · Before: ' + Number(x.before) + ' (' + esc(x.before_condition) + ') ' + esc(x.before_remarks || '') + ' · Evidence: ' + esc(x.before_evidence || 'none') + ' · ' + esc(x.before_inspector) + ' · ' + esc(x.before_inspected_at) : '') + '</p><p class="asset-result" aria-live="polite"></p>' +
      '<label><input type="checkbox" name="checked" ' + (d.actual !== undefined ? 'checked' : '') + '> Item checked</label> ' +
      '<label>Actual quantity <input class="input" type="number" name="actual" min="0" max="10000" inputmode="numeric" value="' + esc(d.actual !== undefined ? d.actual : x.expected) + '"></label> ' +
      '<label>Condition <select class="select" name="condition"><option value="GOOD">Good</option><option value="DAMAGED" ' + (d.condition === 'DAMAGED' ? 'selected' : '') + '>Damaged</option></select></label> ' +
      '<label><input type="checkbox" name="replaced" ' + (d.replaced ? 'checked' : '') + '> Replaced / replenished</label> ' +
      '<label>Remarks <input class="input" name="remarks" maxlength="5000" value="' + esc(d.remarks || '') + '"></label> ' +
      '<label>Evidence reference (optional) <input class="input" name="evidence" maxlength="1000" value="' + esc(d.evidence || '') + '"></label></fieldset>';
    });
    html += '<p><button type="button" class="btn btn-sm btn-outline" data-save="draft">Save progress</button> <button type="button" class="btn btn-sm" data-save="complete">Complete verification</button></p></form>';
    $('asset-inspection').innerHTML = html;
    const form = $('asset-check-form');
    function updateResults() { form.querySelectorAll('fieldset').forEach(c => {
      const ref = i.checklist.find(x => x.item_id === Number(c.dataset.item));
      const checked = c.querySelector('[name=checked]').checked;
      const actual = Number(c.querySelector('[name=actual]').value);
      const baseline = i.phase === 'POST' ? ref.before : ref.expected;
      const damaged = c.querySelector('[name=condition]').value === 'DAMAGED';
      const result = c.querySelector('.asset-result');
      result.textContent = !checked ? 'Not checked' : (actual < baseline ? (baseline - actual) + ' missing' : actual > baseline ? (actual - baseline) + ' additional / replenished' : 'Quantity present') + (damaged ? ' · Damaged' : '');
      result.style.fontWeight = checked && (actual !== baseline || damaged) ? 'bold' : 'normal';
    }); }
    form.addEventListener('input', updateResults); form.addEventListener('change', updateResults); updateResults();
  }
  async function start(id, phase, button) {
    button.disabled = true;
    try { inspection = (await API.post(root + 'inspections/', {assignment_id:id, phase:phase})).data; renderInspection(); message('Checklist opened.'); }
    catch (e) { error(e); } finally { button.disabled = false; }
  }
  async function save(complete, button) {
    const cards = Array.from(document.querySelectorAll('#asset-check-form fieldset'));
    if (complete && cards.some(c => !c.querySelector('[name=checked]').checked)) { message('Check every item before completing verification.'); return; }
    const selected = cards.filter(c => c.querySelector('[name=checked]').checked);
    if (selected.some(c => !c.querySelector('[name=actual]').validity.valid || c.querySelector('[name=actual]').value === '')) { message('Enter a valid actual quantity for every checked item.'); return; }
    const lines = selected.map(c => ({item_id:Number(c.dataset.item), actual:Number(c.querySelector('[name=actual]').value), condition:c.querySelector('[name=condition]').value,
      replaced:c.querySelector('[name=replaced]').checked, remarks:c.querySelector('[name=remarks]').value, evidence:c.querySelector('[name=evidence]').value}));
    button.disabled = true;
    try {
      inspection = (await API.post(root + 'inspections/' + inspection.id + '/', {revision:inspection.revision, complete:complete, lines:lines})).data;
      renderInspection(); message(complete ? 'Verification completed successfully. Discrepancies are available for management review.' : 'Progress saved.');
      if (complete) { queue(queuePage); loadReport(reportPage); }
    } catch (e) { error(e); } finally { button.disabled = false; }
  }
  function options() {
    const scope = $('asset-config-form').elements.scope.value;
    $('asset-config-form').elements.scope_id.innerHTML = (scope === 'room' ? config.room_options : config.type_options).map(x => '<option value="' + Number(x.id) + '">' + esc(x.room_number || x.name) + '</option>').join('');
    $('asset-config-form').elements.item_id.innerHTML = items.filter(x => x.category === 'REUSABLE').map(x => '<option value="' + Number(x.id) + '">' + esc(x.name) + '</option>').join('');
  }
  async function loadConfig() {
    try {
      const r = await Promise.all([API.get(root + 'configuration/'), API.get(root + 'catalog/')]); config = r[0].data; items = r[1].data;
      $('asset-management').hidden = false; options();
      $('asset-config-list').innerHTML = '<ul>' + config.types.map(x => '<li>Type ' + esc(x.room_type__name) + ' · ' + esc(x.item__name) + ': ' + Number(x.quantity) + '</li>').concat(config.rooms.map(x => '<li>Room ' + esc(x.room__room_number) + ' · ' + esc(x.item__name) + ': ' + Number(x.quantity) + '</li>')).join('') + '</ul>';
      loadReport(reportPage);
    } catch (e) { if (e.status !== 403 && e.status !== 401) error(e); }
  }
  async function loadReport(page) {
    if ($('asset-management').hidden) return;
    reportPage = page || 1;
    try {
      const r = await API.get(root + 'discrepancies/', {params:{page:reportPage}});
      $('asset-report').innerHTML = r.data.length ? r.data.map(x => '<article class="asset-card"><strong>Room ' + esc(x.room) + ' · ' + esc(x.item) + '</strong><p>' + esc(x.room_type) + ' · Booking ' + esc(x.booking) + (x.stay ? ' · Stay ' + esc(x.stay) : '') + ' · ' + esc(x.phase) + '</p><p>Expected ' + Number(x.expected) + ' · Before ' + (x.before == null ? '—' : Number(x.before)) + ' · Actual ' + Number(x.actual) + ' · Missing ' + Number(x.missing) + ' · ' + esc(x.condition) + (x.replaced ? ' · Replaced/replenished' : '') + '</p><p>' + esc(x.housekeeper) + ' · ' + esc(x.inspected_at) + ' · ' + esc(x.status) + '</p><p>Remarks: ' + esc(x.remarks) + ' · Evidence reference: ' + esc(x.evidence) + '</p><ol>' + x.history.map(h => '<li>' + esc(h.created_at) + ' · ' + esc(h.actor) + ' · ' + esc(h.status) + ': ' + esc(h.explanation) + '</li>').join('') + '</ol><form data-review="' + Number(x.id) + '"><label>Status <select class="select" name="status"><option value="INVESTIGATING">Investigating</option><option value="RESOLVED">Resolved</option></select></label><label>Explanation / action <input class="input" name="explanation" required maxlength="5000"></label> <button class="btn btn-sm">Record review</button></form></article>').join('') : '<p>No discrepancies.</p>';
      pager($('asset-report-pager'), reportPage, r.pagination, loadReport);
    } catch (e) { error(e); }
  }
  $('asset-refresh').onclick = () => { queue(1); loadConfig(); };
  $('asset-queue').onclick = e => { const b = e.target.closest('[data-start]'); if (b) start(Number(b.dataset.start), b.dataset.phase, b); };
  $('asset-inspection').onclick = e => { const b = e.target.closest('[data-save]'); if (b) save(b.dataset.save === 'complete', b); };
  $('asset-config-form').elements.scope.onchange = options;
  $('asset-catalog-form').onsubmit = async e => { e.preventDefault(); const b = e.target.querySelector('button'); b.disabled = true; try { await API.post(root + 'catalog/', {name:e.target.elements.name.value,category:e.target.elements.category.value}); e.target.reset(); await loadConfig(); message('Reusable item saved.'); } catch (err) { error(err); } finally { b.disabled = false; } };
  $('asset-config-form').onsubmit = async e => { e.preventDefault(); const f=e.target, b=f.querySelector('button'); b.disabled = true; try { await API.post(root + 'configuration/', {scope:f.elements.scope.value,scope_id:Number(f.elements.scope_id.value),item_id:Number(f.elements.item_id.value),quantity:Number(f.elements.quantity.value)}); await loadConfig(); message('Expectation saved.'); } catch (err) { error(err); } finally { b.disabled = false; } };
  $('asset-report').onsubmit = async e => { e.preventDefault(); const f=e.target,b=f.querySelector('button'); b.disabled=true; try { await API.post(root + 'discrepancies/' + Number(f.dataset.review) + '/reviews/', {status:f.elements.status.value,explanation:f.elements.explanation.value}); await loadReport(reportPage); message('Review recorded.'); } catch (err) { error(err); } finally { b.disabled=false; } };
  queue(1); loadConfig();
})();
