/* Helpers loaded ONLY by the assigned reception pages. */
(function () {
  "use strict";
  const API = window.API, esc = JONE.esc;
  const base = "/api/admin/service-requests/";
  function unwrap(r) { return r && r.data !== undefined ? r.data : r; }
  function loading() { return '<div class="service-loading" role="status" aria-label="Loading"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><span class="visually-hidden">Loading data</span></div>'; }
  function error(err) { return '<div class="alert alert-error" role="alert">' + esc(err.message || "Unable to load data. Please retry.") + '</div>'; }
  function key() { return window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(36).slice(2); }
  function tabs(onChange) {
    document.querySelectorAll('[data-tab]').forEach(tab => tab.addEventListener('click', () => {
      document.querySelectorAll('[data-tab]').forEach(t => {t.classList.toggle('active', t === tab); t.setAttribute('aria-selected', String(t === tab));});
      document.querySelectorAll('[data-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== tab.dataset.tab));
      if (onChange) onChange(tab.dataset.tab);
    }));
  }
  // Preserve the exact key for a lost response, including after page refresh.
  function postOnce(form, url, payload) {
    if (form._busy) return Promise.reject(new Error("This submission is still processing."));
    const slot = "jone.reception.pending:" + url;
    const fingerprint = JSON.stringify(payload);
    let pending;
    try { pending = JSON.parse(sessionStorage.getItem(slot)); } catch (_) {}
    if (!pending || pending.fingerprint !== fingerprint) pending = {fingerprint, key:key()};
    try { sessionStorage.setItem(slot, JSON.stringify(pending)); } catch (_) {}
    form._busy = true;
    const button = form.querySelector('[type=submit]');
    if (button) button.disabled = true;
    return API.post(url, Object.assign({}, payload, {idempotency_key:pending.key})).then(r => {
      try { sessionStorage.removeItem(slot); } catch (_) {}
      return unwrap(r);
    }).finally(() => { form._busy = false; if (button) button.disabled = false; });
  }
  // Values always come from guest -> booking -> room relationships.
  function guests(form, opts) {
    const select = form.querySelector('[name=guest_id]'), booking = form.querySelector('[name=booking_id]');
    const room = form.querySelector('[name=room_number]');
    const context = form.querySelector('[data-guest-context]');
    const search = form.querySelector('[data-guest-search]');
    let rows = [], seq = 0, timer;
    function contextChange() {
      const g = rows.find(g => String(g.id) === select.value);
      const b = g && g.bookings.find(b => String(b.id) === booking.value);
      if (room) {
        room.innerHTML = '<option value="">— Select assigned room —</option>';
        ((b && b.rooms) || []).forEach(n => room.add(new Option(n,n)));
        if (b && b.rooms.length === 1) room.value = b.rooms[0];
      }
      if (context) context.textContent = b ? g.full_name + " · " + b.reference + " · Room " + (b.rooms.join(", ") || "not assigned") + " · " + b.check_in + " to " + b.check_out + " · " + b.status : (g ? "No current stay selected. Select a booking where applicable." : "Select a guest to view their stay.");
    }
    function guestChange() {
      const g = rows.find(g => String(g.id) === select.value);
      booking.innerHTML = '<option value="">— No current stay —</option>';
      ((g && g.bookings) || []).forEach(b => booking.add(new Option(b.reference + " · " + b.rooms.join(", ") + " · " + b.check_in + " – " + b.check_out, b.id)));
      if (g && g.bookings.length === 1) booking.value = g.bookings[0].id;
      contextChange();
    }
    async function load(term) {
      const current = ++seq, keep = select.value;
      select.disabled = true;
      try {
        const data = unwrap(await API.get(base + "reception/guests/?search=" + encodeURIComponent(term || "")));
        if (current !== seq) return;
        rows = data.results || [];
        select.innerHTML = '<option value="">— Select guest (search to refine) —</option>';
        rows.forEach(g => select.add(new Option(g.full_name + (g.bookings.length ? " · Room " + g.bookings.map(b => b.rooms.join(", ")).join(" / ") : ""),g.id)));
        if (keep) select.value = keep;
        guestChange();
      } catch(err) {
        if (current !== seq) return;
        select.innerHTML = '<option value="">Unable to load guests — search again to retry</option>';
        if (context) context.textContent = err.message;
      } finally { if (current === seq) select.disabled = false; }
    }
    select.addEventListener('change', guestChange);
    booking.addEventListener('change', contextChange);
    form.addEventListener('reset', () => setTimeout(guestChange, 0));
    if (search) search.addEventListener('input', () => {clearTimeout(timer); timer=setTimeout(() => load(search.value),250);});
    load("").then(async () => {
      if (!opts || !opts.deepLink) return;
      const params = new URLSearchParams(location.search), ref = params.get('booking') || params.get('ref');
      if (!ref) return;
      try {
        const b = unwrap(await API.get('/api/admin/bookings/' + encodeURIComponent(ref) + '/'));
        if (!b.guest || !b.guest.id) throw new Error("The booking guest could not be loaded.");
        const g = {id:b.guest.id,full_name:b.guest.full_name || [b.guest.first_name,b.guest.last_name].filter(Boolean).join(' '),bookings:[{id:b.id,reference:b.booking_reference,status:b.status,check_in:b.check_in,check_out:b.check_out,rooms:(b.room_assignments || []).map(a => a.room_number).filter(Boolean)}]};
        rows = [g]; select.innerHTML=''; select.add(new Option(g.full_name,g.id)); guestChange();
        if (room && params.get('room') && g.bookings[0].rooms.includes(params.get('room'))) room.value=params.get('room');
      } catch(err) { if (context) context.textContent=err.message; }
    });
    return {reload:load};
  }
  window.ReceptionServices = {base,unwrap,loading,error,key,tabs,postOnce,guests};
})();
