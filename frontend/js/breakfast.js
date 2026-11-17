/* Complimentary breakfast: preserve entitlement-only, non-stock workflow. */
(function () {
  "use strict";
  const S = window.ReceptionServices, API = window.API, UI = JONE.ui, esc = JONE.esc;
  function resetDate(form) { form.querySelector('[name=service_date]').value = JONE.hotelTodayISO(); }
  function init() {
    S.tabs(name => { if (name === 'history') history(); if (name === 'config') config(); });
    ['[data-breakfast-issue-form]', '[data-breakfast-eligibility-form]'].forEach(selector => {
      const form=document.querySelector(selector); resetDate(form); S.guests(form);
    });
    document.querySelector('[data-breakfast-issue-form]').addEventListener('submit', issue);
    document.querySelector('[data-breakfast-eligibility-form]').addEventListener('submit', eligibility);
    document.querySelector('[data-breakfast-config-form]').addEventListener('submit', saveConfig);
    document.querySelector('[data-breakfast-history-refresh]').addEventListener('click', history);
    banner();
  }
  function selection(form) {
    return {guest_id:Number(form.elements.guest_id.value), booking_id:Number(form.elements.booking_id.value),service_date:form.elements.service_date.value};
  }
  async function banner() {
    const status=document.querySelector('[data-breakfast-config-status]');
    try { const c=S.unwrap(await API.get(S.base+'breakfast/config/')); status.textContent=(c.is_enabled?'Enabled':'Disabled')+(c.service_start?' · '+c.service_start+' – '+c.service_end:''); }
    catch(e) { status.textContent=e.message || 'Unable to load breakfast configuration. Refresh to retry.'; }
  }
  async function issue(e) {
    e.preventDefault(); const form=e.target, payload=selection(form); payload.notes=form.elements.notes.value;
    if (!payload.guest_id || !payload.booking_id) return UI.toast('Select a guest and their booking.','warning');
    try { const data=await S.postOnce(form,S.base+'breakfast/issue/',payload); UI.toast(data.created?'Breakfast recorded.':'Already recorded for this service date.','success'); history(); }
    catch(err) { UI.toast(err.message || 'Breakfast could not be recorded.','error'); }
  }
  async function eligibility(e) {
    e.preventDefault(); const form=e.target, result=document.querySelector('[data-breakfast-eligibility-result]');
    result.innerHTML=S.loading();
    try {
      const d=S.unwrap(await API.get(S.base+'breakfast/eligibility/?'+new URLSearchParams(selection(form))));
      result.innerHTML='<div class="alert" role="status"><strong>'+(d.eligible?'Eligible':'Not eligible')+'</strong> · '+esc(d.eligible?'Breakfast entitlement: '+d.eligible_guests+' guest(s), '+d.check_in+' to '+d.check_out:d.reason)+'</div>';
    } catch(err) { result.innerHTML=S.error(err); }
  }
  async function history() {
    const box=document.querySelector('[data-breakfast-history-container]'); box.innerHTML=S.loading();
    try {
      const rows=S.unwrap(await API.get(S.base+'breakfast/issues/')).results || [];
      if (!rows.length) {box.innerHTML='<p class="text-muted">No breakfast services recorded yet.</p>';return;}
      box.innerHTML='<table class="table"><thead><tr><th>Reference</th><th>Guest</th><th>Room</th><th>Booking / dates</th><th>Service date</th><th>Entitlement</th><th>Staff</th></tr></thead><tbody>'+rows.map(i => '<tr><td>'+esc(i.reference)+'</td><td>'+esc(i.guest_name || 'Name unavailable')+'</td><td>'+esc(i.room_number || 'Not assigned')+'</td><td>'+esc(i.booking_reference || '—')+'<br>'+esc(i.check_in || '')+' – '+esc(i.check_out || '')+'</td><td>'+esc(i.service_date)+'</td><td>'+esc(i.eligible_guests)+'</td><td>'+esc(i.issued_by_name || '—')+'</td></tr>').join('')+'</tbody></table>';
    } catch(err) {box.innerHTML=S.error(err);}
  }
  async function config() {
    try {
      const c=S.unwrap(await API.get(S.base+'breakfast/config/')), form=document.querySelector('[data-breakfast-config-form]');
      form.elements.is_enabled.checked=!!c.is_enabled; ['service_start','service_end','notes'].forEach(n => form.elements[n].value=c[n] || '');
    } catch(err) {UI.toast(err.message,'error');}
  }
  async function saveConfig(e) {
    e.preventDefault(); const form=e.target, b=form.querySelector('[type=submit]'); if (b.disabled) return; b.disabled=true;
    try { await API.put(S.base+'breakfast/config/',{is_enabled:form.elements.is_enabled.checked,service_start:form.elements.service_start.value,service_end:form.elements.service_end.value,notes:form.elements.notes.value}); UI.toast('Configuration saved.','success'); banner(); }
    catch(err) {UI.toast(err.message,'error');} finally {b.disabled=false;}
  }
  if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',init); else init();
})();
