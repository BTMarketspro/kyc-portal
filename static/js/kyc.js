/* KYC / KYB verification wizard. No build step, no dependencies. */
(() => {
  'use strict';

  const boot = JSON.parse(document.getElementById('boot').textContent);
  const form = document.getElementById('kyc');
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const q = (v) => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const steps = $$('.step', form);
  const LINE_FIELDS = new Set(['collections_data.top_5_senders', 'payouts_data.top_5_beneficiaries', 'payouts_data.beneficiary_industries']);
  const lookupCache = {};
  let current = 0;
  let reached = 0;
  let dirty = false;

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}) {
    const headers = { 'X-KYC-Token': boot.token, Accept: 'application/json' };
    let body;
    if (opts.json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(opts.json); }
    try {
      const res = await fetch(path, { method: body ? 'POST' : 'GET', headers, body, credentials: 'same-origin' });
      let data = {};
      try { data = await res.json(); } catch (_) { /* empty body */ }
      return { ok: res.ok, status: res.status, data };
    } catch (_) {
      return { ok: false, status: 0, data: { message: 'Network error. Check your connection and try again.' } };
    }
  }

  function lookup(name, country) {
    const key = name + ':' + (country || '');
    if (!lookupCache[key]) {
      lookupCache[key] = fetch(`/api/lookups/${name}${country ? '?country=' + encodeURIComponent(country) : ''}`)
        .then((r) => (r.ok ? r.json() : Promise.reject(r)))
        .catch(() => { delete lookupCache[key]; return name === 'postal' ? null : []; });
    }
    return lookupCache[key];
  }

  // ------------------------------------------------------------------ paths
  function itemPrefix(el) {
    const item = el.closest('[data-item]');
    return item ? item.dataset.prefix : '';
  }
  function resolveRef(ref, el) {
    if (!ref) return ref;
    return ref.startsWith('@') ? `${itemPrefix(el)}.${ref.slice(1)}` : ref;
  }
  function named(path) { return form.querySelector(`[name="${q(path)}"]`); }

  function readValue(path) {
    const els = $$(`[name="${q(path)}"]`, form);
    if (!els.length) return undefined;
    const el = els[0];
    if (el.dataset.type === 'array') return els.filter((x) => x.checked).map((x) => x.value);
    if (el.type === 'radio') {
      const c = els.find((x) => x.checked);
      if (!c) return undefined;
      return c.dataset.type === 'bool' ? c.value === 'true' : c.value;
    }
    if (el.type === 'checkbox') return el.checked;
    return el.value;
  }

  function setPath(obj, path, value) {
    const parts = path.split('.');
    let o = obj;
    parts.forEach((p, i) => {
      const last = i === parts.length - 1;
      const nextIsIndex = !last && /^\d+$/.test(parts[i + 1]);
      const key = /^\d+$/.test(p) ? Number(p) : p;
      if (last) { o[key] = value; return; }
      if (o[key] === undefined || o[key] === null || typeof o[key] !== 'object') o[key] = nextIsIndex ? [] : {};
      o = o[key];
    });
  }
  function getPath(obj, path) {
    return path.split('.').reduce((o, p) => (o == null ? undefined : o[/^\d+$/.test(p) ? Number(p) : p]), obj);
  }

  // ------------------------------------------------------------------ collect / fill
  function collect() {
    const data = {};
    for (const el of form.elements) {
      if (!el.name || el.disabled || el.type === 'file' || el.closest('.is-off')) continue;
      const t = el.dataset.type;
      if (t === 'array') {
        let arr = getPath(data, el.name);
        if (!Array.isArray(arr)) { arr = []; setPath(data, el.name, arr); }
        if (el.checked) arr.push(el.value);
        continue;
      }
      if (el.type === 'radio') {
        if (el.checked) setPath(data, el.name, t === 'bool' ? el.value === 'true' : el.value);
        continue;
      }
      if (el.type === 'checkbox') { setPath(data, el.name, el.checked); continue; }
      let v = el.value.trim();
      if (t === 'json') { try { v = JSON.parse(el.value || '[]'); } catch (_) { v = []; } }
      else if (LINE_FIELDS.has(el.name)) v = el.value.split('\n').map((s) => s.trim()).filter(Boolean);
      setPath(data, el.name, v);
    }
    if (boot.flow === 'business') {
      if (data._same_address) {
        const proof = (data.physical_address || {}).proof_of_address_file || '';
        data.physical_address = Object.assign({}, data.registered_address || {}, { proof_of_address_file: proof });
      }
      (data.associated_persons || []).forEach((p) => {
        if (p && !p.has_ownership && (p.ownership_percentage === undefined || p.ownership_percentage === '')) p.ownership_percentage = 0;
      });
    }
    return data;
  }

  function setEl(el, v) {
    if (v === undefined || v === null) return;
    const t = el.dataset.type;
    if (t === 'array') { el.checked = Array.isArray(v) && v.includes(el.value); return; }
    if (el.type === 'radio') { el.checked = String(v) === el.value; return; }
    if (el.type === 'checkbox') { el.checked = v === true || v === 'true' || v === 1 || v === '1'; return; }
    if (t === 'json') { el.value = JSON.stringify(Array.isArray(v) ? v : []); return; }
    if (Array.isArray(v)) { el.value = v.join('\n'); return; }
    if (el.tagName === 'SELECT' && (el.dataset.subdivisions || el.dataset.idtypes)) el.dataset.pending = v;
    el.value = v;
  }

  function fill(data) {
    $$('[data-repeater]', form).forEach((rep) => {
      const arr = getPath(data, rep.dataset.repeater);
      const start = Number(rep.dataset.start || 0);
      const need = Array.isArray(arr) ? Math.max(0, arr.length - start) : 0;
      while ($$(':scope > .items > [data-item]', rep).length < need) addItem(rep, false);
    });
    for (const el of form.elements) {
      if (!el.name || el.type === 'file') continue;
      setEl(el, getPath(data, el.name));
    }
  }

  // ------------------------------------------------------------------ conditions
  function evalTerm(term, el) {
    const m = term.trim().match(/^(@?[\w.]+)\s*(!=|=|~)\s*(.*)$/);
    if (!m) return true;
    const [, ref, op, raw] = m;
    const v = readValue(resolveRef(ref, el));
    const val = raw.trim();
    if (op === '~') return Array.isArray(v) && v.some((x) => x !== val);
    let eq;
    if (Array.isArray(v)) eq = v.includes(val);
    else eq = String(v === undefined ? '' : v) === val;
    return op === '=' ? eq : !eq;
  }
  function refreshConditions() {
    $$('[data-show-if]', form).forEach((el) => {
      const show = el.dataset.showIf.split('||').some((clause) => clause.split('&&').every((t) => evalTerm(t, el)));
      el.classList.toggle('is-off', !show);
    });
    updateOwnershipTotal();
  }
  let condQueued = false;
  function queueConditions() {
    if (condQueued) return;
    condQueued = true;
    requestAnimationFrame(() => { condQueued = false; refreshConditions(); });
  }

  // ------------------------------------------------------------------ select sources
  function flag(cc) {
    return cc.toUpperCase().replace(/./g, (c) => String.fromCodePoint(127397 + c.charCodeAt(0)));
  }
  function fillSelect(sel, items, keepFirst = true) {
    const pending = sel.dataset.pending ?? sel.value;
    const first = keepFirst ? sel.options[0] : null;
    sel.innerHTML = '';
    if (first) sel.appendChild(first);
    const frag = document.createDocumentFragment();
    items.forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; frag.appendChild(o); });
    sel.appendChild(frag);
    if (pending && items.some(([v]) => v === pending)) sel.value = pending;
    delete sel.dataset.pending;
  }
  function initSources(root) {
    $$('select[data-source]', root).forEach((sel) => {
      const src = sel.dataset.source;
      if (src === 'countries') fillSelect(sel, boot.countries.map((c) => [c.code, c.name]));
      else if (src === 'calling_codes') {
        const seen = new Set();
        const items = [];
        boot.calling_codes.slice().sort((a, b) => a.name.localeCompare(b.name)).forEach((c) => {
          items.push([c.dial, `${flag(c.country)} ${c.dial}  ${c.name}`]);
          seen.add(c.dial);
        });
        fillSelect(sel, items);
        sel.dataset.countryCodes = '1';
      }
    });
    $$('[data-follow]', root).forEach((sel) => watch(sel, sel.dataset.follow, (v) => {
      if (!sel.value && v) { sel.value = v; sel.dispatchEvent(new Event('change', { bubbles: true })); }
    }));
    $$('[data-follow-country]', root).forEach((sel) => watch(sel, sel.dataset.followCountry, (v) => {
      if (sel.value || !v) return;
      const hit = boot.calling_codes.find((c) => c.country === v);
      if (hit) { sel.value = hit.dial; sel.dispatchEvent(new Event('change', { bubbles: true })); }
    }));
    $$('select[data-subdivisions]', root).forEach((sel) => watch(sel, sel.dataset.subdivisions, (cc) => loadSubdivisions(sel, cc), true));
    $$('select[data-idtypes]', root).forEach((sel) => watch(sel, sel.dataset.idtypes, (cc) => loadIdTypes(sel, cc), true));
    $$('input[data-postal]', root).forEach((inp) => watch(inp, inp.dataset.postal, (cc) => loadPostal(inp, cc), true));
  }

  // Calls fn(value) whenever the referenced field changes (and once now if `now`).
  function watch(el, ref, fn, now = false) {
    form.addEventListener('change', (ev) => {
      if (!ev.target.name || !el.isConnected) return;
      if (ev.target.name === resolveRef(ref, el)) fn(ev.target.value);
    });
    if (now) { const v = readValue(resolveRef(ref, el)); if (v) fn(v); }
  }

  async function loadSubdivisions(sel, cc) {
    if (!cc) { fillSelect(sel, []); return []; }
    sel.disabled = true;
    const subs = await lookup('subdivisions', cc);
    sel.disabled = false;
    if (!subs.length) {
      fillSelect(sel, [['N/A', 'Not applicable']]);
      if (!sel.value) sel.value = 'N/A';
    } else {
      fillSelect(sel, subs.map((s) => [s.code, s.name]));
    }
    return subs;
  }
  async function loadIdTypes(sel, cc) {
    if (!cc) { fillSelect(sel, []); return; }
    sel.disabled = true;
    const types = await lookup('id-types', cc);
    sel.disabled = false;
    const skip = new Set(boot.enums.non_photo_id_types);
    fillSelect(sel, types.filter((t) => !skip.has(t.type)).map((t) => [t.type, t.description]));
    sel.dispatchEvent(new Event('input', { bubbles: true }));
  }
  async function loadPostal(inp, cc) {
    const field = inp.closest('.field');
    const label = field && $('.label', field);
    if (!cc) return;
    const fmt = await lookup('postal', cc);
    if (!fmt) return;
    inp.dataset.regex = fmt.uses_postal_codes ? fmt.js_regex : '';
    inp.required = !!fmt.uses_postal_codes;
    inp.placeholder = fmt.samples && fmt.samples.length ? `e.g. ${fmt.samples[0]}` : '';
    if (label) {
      const marker = $('.req, .opt', label);
      if (marker) marker.outerHTML = fmt.uses_postal_codes ? '<span class="req" aria-hidden="true">*</span>' : '<span class="opt">Optional</span>';
    }
  }

  // ------------------------------------------------------------------ menus (typeahead / chips / address)
  function menuController(input, menu, onPick) {
    let items = [];
    let active = -1;
    const render = (list, html) => {
      items = list;
      active = list.length ? 0 : -1;
      menu.innerHTML = list.map((it, i) => `<li role="option" data-i="${i}" class="${i === 0 ? 'active' : ''}">${html(it)}</li>`).join('');
      menu.classList.toggle('open', list.length > 0);
      input.setAttribute('aria-expanded', list.length > 0 ? 'true' : 'false');
    };
    const close = () => { menu.classList.remove('open'); input.setAttribute('aria-expanded', 'false'); };
    const setActive = (i) => {
      $$('li', menu).forEach((li, k) => li.classList.toggle('active', k === i));
      active = i;
      const li = menu.children[i];
      if (li) li.scrollIntoView({ block: 'nearest' });
    };
    menu.addEventListener('mousedown', (e) => {
      const li = e.target.closest('li');
      if (!li) return;
      e.preventDefault();
      onPick(items[Number(li.dataset.i)]);
      close();
    });
    input.addEventListener('keydown', (e) => {
      if (!menu.classList.contains('open')) return;
      if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(items.length - 1, active + 1)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(0, active - 1)); }
      else if (e.key === 'Enter') { e.preventDefault(); if (items[active]) { onPick(items[active]); close(); } }
      else if (e.key === 'Escape') close();
    });
    input.addEventListener('blur', () => setTimeout(close, 120));
    return { render, close };
  }
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function initTypeahead(box) {
    const input = $('input.input', box);
    const hidden = $('input[type=hidden]', box);
    const src = box.dataset.typeahead;
    let list = null;
    const load = async () => (list = list || await lookup(src));
    const pick = (o) => {
      input.value = o.label;
      hidden.value = o.code;
      hidden.dispatchEvent(new Event('change', { bubbles: true }));
      clearError(hidden.name);
    };
    const m = menuController(input, $('.menu', box), pick);
    input.addEventListener('focus', load);
    input.addEventListener('input', async () => {
      hidden.value = '';
      const all = await load();
      const words = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      if (!words.length) { m.close(); return; }
      const hits = all.filter((o) => {
        const l = o.label.toLowerCase();
        return words.every((w) => l.includes(w) || o.code.startsWith(w));
      }).sort((a, b) => a.label.toLowerCase().indexOf(words[0]) - b.label.toLowerCase().indexOf(words[0])).slice(0, 40);
      m.render(hits, (o) => `${esc(o.label)} <small>${esc(o.code)}</small>`);
    });
    box.sync = async () => {
      if (!hidden.value) return;
      const all = await load();
      const hit = all.find((o) => o.code === hidden.value);
      input.value = hit ? hit.label : hidden.value;
    };
  }

  function initChips(box) {
    const input = $('input.input', box);
    const hidden = $('input[type=hidden]', box);
    const listEl = $('.chip-list', box);
    const src = box.dataset.chips;
    const all = (src === 'currencies' ? boot.enums.currencies : boot.enums.transaction_countries) || [];
    const values = () => { try { return JSON.parse(hidden.value || '[]'); } catch (_) { return []; } };
    const draw = () => {
      listEl.innerHTML = values().map((v) => {
        const o = all.find((x) => x.code === v);
        return `<span class="chip" title="${esc(o ? o.name : v)}">${esc(v)}<button type="button" data-v="${esc(v)}" aria-label="Remove ${esc(v)}">×</button></span>`;
      }).join('');
    };
    const set = (arr) => { hidden.value = JSON.stringify(arr); draw(); hidden.dispatchEvent(new Event('change', { bubbles: true })); };
    const m = menuController(input, $('.menu', box), (o) => { set([...new Set([...values(), o.code])]); input.value = ''; });
    input.addEventListener('input', () => {
      const t = input.value.trim().toLowerCase();
      if (!t) { m.close(); return; }
      const have = new Set(values());
      const rank = (o) => (o.code.toLowerCase() === t ? 0 : o.code.toLowerCase().startsWith(t) ? 1 : o.name.toLowerCase().startsWith(t) ? 2 : 3);
      m.render(all.filter((o) => !have.has(o.code) && (o.code.toLowerCase().startsWith(t) || o.name.toLowerCase().includes(t)))
        .sort((a, b) => rank(a) - rank(b)).slice(0, 30),
        (o) => `${esc(o.name)} <small>${esc(o.code)}</small>`);
    });
    listEl.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-v]');
      if (b) set(values().filter((v) => v !== b.dataset.v));
    });
    box.sync = draw;
    draw();
  }

  // ------------------------------------------------------------------ address autocomplete
  function initAddress(block) {
    const wrap = $('[data-addr-search]', block);
    const input = $('input', wrap);
    const countrySel = $('[data-address-country]', block);
    const hint = $('.addr-hint', wrap);
    let session = newSession();
    let timer;
    let searchSeq = 0;
    if (boot.address_provider === 'google') $('.powered', wrap).hidden = false;

    function newSession() { return (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)); }
    const syncEnabled = () => {
      input.disabled = !countrySel.value;
      hint.textContent = countrySel.value ? 'Start typing and pick your address — or fill in the fields below.' :
        'Select a country first, then search — or fill in the fields below.';
    };
    countrySel.addEventListener('change', syncEnabled);
    syncEnabled();

    const m = menuController(input, $('.menu', wrap), async (item) => {
      let addr = item.address;
      if (!addr) {
        const r = await api(`/api/address/details?id=${encodeURIComponent(item.id)}&country=${countrySel.value}&session=${session}`);
        addr = r.data.address;
      }
      session = newSession();
      if (addr) apply(addr);
    });

    input.addEventListener('input', () => {
      clearTimeout(timer);
      const term = input.value.trim();
      if (term.length < 3) { m.close(); return; }
      const seq = ++searchSeq;
      timer = setTimeout(async () => {
        const r = await api(`/api/address/autocomplete?q=${encodeURIComponent(term)}&country=${countrySel.value}&session=${session}`);
        if (seq !== searchSeq) return; // a newer search is in flight
        const res = (r.data && r.data.results) || [];
        if (!res.length && r.data && r.data.error) hint.textContent = r.data.error;
        m.render(res, (it) => `<span class="addr-main">${esc(it.main)}</span><small>${esc(it.secondary)}</small>`);
      }, 260);
    });

    async function apply(a) {
      const prefix = block.dataset.address;
      const set = (k, v) => { const el = named(`${prefix}.${k}`); if (el && v !== undefined && v !== null) { el.value = v; clearError(el.name); el.dispatchEvent(new Event('input', { bubbles: true })); } };
      if (a.country && a.country !== countrySel.value && boot.countries.some((c) => c.code === a.country)) {
        countrySel.value = a.country;
        countrySel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      set('street_line_1', a.street_line_1 || '');
      set('street_line_2', a.street_line_2 || '');
      set('city', a.city || '');
      set('postal_code', a.postal_code || '');
      const stateSel = named(`${prefix}.state`);
      if (stateSel) {
        await loadSubdivisions(stateSel, countrySel.value);
        if (a.state) { stateSel.value = a.state; clearError(stateSel.name); }
        else stateSel.value = stateSel.value === 'N/A' ? 'N/A' : '';
      }
      input.value = '';
      hint.textContent = a.state || !stateSel || stateSel.value ? 'Address filled in — please check every field below.'
        : `Address filled in — please select your state / region${a.state_text ? ` (${a.state_text})` : ''}.`;
      queueConditions();
      dirty = true;
    }
  }

  // ------------------------------------------------------------------ uploads
  function shortName(name, max = 34) {
    if (!name || name.length <= max) return name || '';
    const dot = name.lastIndexOf('.');
    const ext = dot > 0 && name.length - dot <= 6 ? name.slice(dot) : '';
    const keep = max - ext.length - 1;
    return `${name.slice(0, Math.ceil(keep * 0.6))}…${name.slice(name.length - ext.length - Math.floor(keep * 0.4))}`;
  }
  function fmtSize(n) { return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`; }

  function initUploader(u) {
    const hidden = $('input[type=hidden]', u);
    const file = $('input[type=file]', u);
    const drop = $('.drop', u);
    const prev = $('.preview', u);
    const thumb = $('.thumb', u);
    const fname = $('.fname', u);
    const fstate = $('.fstate', u);
    const bar = $('.bar span', u);

    const showPreview = (on) => { prev.hidden = !on; drop.hidden = on; };
    const setThumb = (src, isPdf) => {
      thumb.innerHTML = src ? `<img alt="" src="${src}">` : `<svg class="ico lg"><use href="#${isPdf ? 'i-doc' : 'i-idcard'}"/></svg>`;
    };
    drop.addEventListener('click', () => file.click());
    $('[data-act=replace]', u).addEventListener('click', () => file.click());
    $('[data-act=remove]', u).addEventListener('click', () => {
      hidden.value = '';
      showPreview(false);
      u.classList.remove('done', 'busy');
      dirty = true;
    });
    ['dragenter', 'dragover'].forEach((ev) => u.addEventListener(ev, (e) => { e.preventDefault(); u.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((ev) => u.addEventListener(ev, (e) => { e.preventDefault(); u.classList.remove('drag'); }));
    u.addEventListener('drop', (e) => { if (e.dataTransfer.files[0]) handle(e.dataTransfer.files[0]); });
    file.addEventListener('change', () => { if (file.files[0]) handle(file.files[0]); file.value = ''; });

    function handle(f) {
      clearError(hidden.name);
      if (boot.preview) { showError(hidden.name, 'Uploads are disabled in preview mode.'); return; }
      if (f.size > boot.file_limits.raw_max) { showError(hidden.name, `This file is larger than ${fmtSize(boot.file_limits.raw_max)}.`); return; }
      const isImg = /^image\/(jpeg|png|gif|webp|bmp)$/.test(f.type);
      setThumb(isImg ? URL.createObjectURL(f) : '', /pdf/.test(f.type));
      fname.textContent = shortName(f.name);
      fname.title = f.name;
      fstate.textContent = 'Uploading… 0%';
      bar.style.width = '0%';
      u.classList.add('busy');
      u.classList.remove('done');
      showPreview(true);

      const fd = new FormData();
      fd.append('file', f);
      fd.append('field', hidden.name);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/upload');
      xhr.setRequestHeader('X-KYC-Token', boot.token);
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        bar.style.width = `${Math.min(pct, 96)}%`;
        fstate.textContent = pct >= 100 ? 'Optimising file…' : `Uploading… ${pct}%`;
      };
      xhr.onload = () => {
        u.classList.remove('busy');
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
        if (xhr.status === 200 && data.ref) {
          hidden.value = data.ref;
          bar.style.width = '100%';
          u.classList.add('done');
          const changed = data.original_size !== data.size ? ` · optimised from ${fmtSize(data.original_size)}` : '';
          fstate.textContent = `Uploaded · ${fmtSize(data.size)}${changed}`;
          if (!isImg) setThumb(`/api/files/${data.id}/thumb?t=${encodeURIComponent(boot.token)}`, data.mime === 'application/pdf');
          hidden.dispatchEvent(new Event('change', { bubbles: true }));
          dirty = true;
        } else {
          hidden.value = '';
          showPreview(false);
          showError(hidden.name, data.message || 'Upload failed. Please try again.');
        }
      };
      xhr.onerror = () => {
        u.classList.remove('busy');
        hidden.value = '';
        showPreview(false);
        showError(hidden.name, 'Upload failed. Check your connection and try again.');
      };
      xhr.send(fd);
    }

    u.sync = async () => {
      if (!hidden.value) { showPreview(false); return; }
      const id = hidden.value.slice(5);
      const r = await api(`/api/files/${id}/meta`);
      if (!r.ok) { hidden.value = ''; showPreview(false); return; }
      fname.textContent = shortName(r.data.name);
      fname.title = r.data.name;
      fstate.textContent = `Uploaded · ${fmtSize(r.data.size)}`;
      bar.style.width = '100%';
      setThumb(`/api/files/${id}/thumb?t=${encodeURIComponent(boot.token)}`, r.data.mime === 'application/pdf');
      u.classList.add('done');
      showPreview(true);
    };
  }

  // ------------------------------------------------------------------ repeaters
  function renumber(item, from, to) {
    const all = [item, ...$$('*', item)];
    all.forEach((el) => {
      for (const attr of Array.from(el.attributes)) {
        if (attr.value.includes(from)) el.setAttribute(attr.name, attr.value.split(from).join(to));
      }
    });
  }

  function addItem(rep, focus = true) {
    const name = rep.dataset.repeater;
    const start = Number(rep.dataset.start || 0);
    const items = $(':scope > .items', rep);
    const idx = start + items.children.length;
    const frag = $(':scope > template', rep).content.cloneNode(true);
    const item = frag.firstElementChild;
    renumber(item, '__i__', String(idx));
    item.dataset.prefix = `${name}.${idx}`;
    items.appendChild(item);
    initWidgets(item);
    updateRepeater(rep);
    refreshConditions();
    if (focus) { const first = $('input:not([type=hidden]), select', item); if (first) first.focus(); }
    return item;
  }

  function removeItem(rep, item) {
    const name = rep.dataset.repeater;
    const start = Number(rep.dataset.start || 0);
    item.remove();
    $$(':scope > .items > [data-item]', rep).forEach((it, k) => {
      const idx = start + k;
      const old = it.dataset.prefix;
      if (old !== `${name}.${idx}`) { renumber(it, `${old}.`, `${name}.${idx}.`); it.dataset.prefix = `${name}.${idx}`; }
    });
    updateRepeater(rep);
    refreshConditions();
    dirty = true;
  }

  function updateRepeater(rep) {
    const n = $$(':scope > .items > [data-item]', rep).length;
    const max = Number(rep.dataset.max || 99);
    const min = Number(rep.dataset.min || 0);
    $(':scope > [data-add]', rep).hidden = n >= max;
    $$(':scope > .items > [data-item]', rep).forEach((it, k) => {
      const rm = $('[data-remove]', it);
      if (rm) rm.hidden = n <= min;
      const title = $('[data-person-title]', it);
      if (title) title.textContent = personName(it) || `Person ${k + 1}`;
    });
  }

  function personName(item) {
    return $$('[data-person-name]', item).map((i) => i.value.trim()).filter(Boolean).join(' ');
  }

  function initRepeaters() {
    $$('[data-repeater]', form).forEach((rep) => {
      $(':scope > [data-add]', rep).addEventListener('click', () => { addItem(rep); dirty = true; });
      rep.addEventListener('click', (e) => {
        const rm = e.target.closest('[data-remove]');
        if (rm && rm.closest('[data-repeater]') === rep) removeItem(rep, rm.closest('[data-item]'));
      });
      const min = Number(rep.dataset.min || 0);
      for (let i = 0; i < min; i++) addItem(rep, false);
      updateRepeater(rep);
    });
  }

  // ------------------------------------------------------------------ widgets
  const today = new Date();
  const iso = (d) => d.toISOString().slice(0, 10);
  function initWidgets(root) {
    initSources(root);
    $$('[data-typeahead]', root).forEach(initTypeahead);
    $$('[data-chips]', root).forEach(initChips);
    $$('[data-uploader]', root).forEach(initUploader);
    $$('[data-address]', root).forEach(initAddress);
    $$('input[type=date]', root).forEach((inp) => {
      const mode = inp.dataset.date;
      const d = new Date(today);
      if (mode === 'birth') { d.setFullYear(d.getFullYear() - 18); inp.max = iso(d); const m = new Date(today); m.setFullYear(m.getFullYear() - 120); inp.min = iso(m); }
      if (mode === 'past') { d.setDate(d.getDate() - 1); inp.max = iso(d); }
      if (mode === 'future') { d.setDate(d.getDate() + 1); inp.min = iso(d); }
      if (mode === 'notfuture') inp.max = iso(today);
    });
    $$('textarea[data-minlen], textarea[maxlength]', root).forEach((ta) => {
      const c = root.querySelector(`[data-counter-for="${q(ta.name)}"]`);
      if (!c) return;
      const upd = () => {
        const n = ta.value.trim().length;
        const min = Number(ta.dataset.minlen || 0);
        c.textContent = `${n}${ta.maxLength > 0 ? ' / ' + ta.maxLength : ''}${min && n < min ? ` · ${min - n} more needed` : ''}`;
        c.classList.toggle('ok', !min || n >= min);
      };
      ta.addEventListener('input', upd);
      ta.sync = upd;
      upd();
    });
    $$('.checkgrid[data-exclusive]', root).forEach((g) => g.addEventListener('change', (e) => {
      const ex = g.dataset.exclusive;
      if (!e.target.checked) return;
      $$('input', g).forEach((i) => { if (e.target.value === ex ? i.value !== ex : i.value === ex) i.checked = false; });
    }));
  }

  function syncWidgets() {
    $$('[data-typeahead], [data-chips], [data-uploader], textarea').forEach((w) => w.sync && w.sync());
    $$('select[data-subdivisions]').forEach((s) => { const cc = readValue(resolveRef(s.dataset.subdivisions, s)); if (cc) loadSubdivisions(s, cc); });
    $$('select[data-idtypes]').forEach((s) => { const cc = readValue(resolveRef(s.dataset.idtypes, s)); if (cc) loadIdTypes(s, cc); });
    $$('input[data-postal]').forEach((i) => { const cc = readValue(resolveRef(i.dataset.postal, i)); if (cc) loadPostal(i, cc); });
    $$('[data-address-country]').forEach((s) => s.dispatchEvent(new Event('change')));
    $$('[data-repeater]').forEach(updateRepeater);
  }

  // ------------------------------------------------------------------ accounts & ownership
  function initAccounts() {
    const boxes = $$('[data-acc]', form);
    form.addEventListener('change', (e) => {
      const k = e.target.dataset && e.target.dataset.acc;
      if (!k || !e.target.checked) return;
      if (k === 'eurde') boxes.forEach((b) => { if (['usd', 'eur'].includes(b.dataset.acc)) b.checked = false; });
      if (['usd', 'eur'].includes(k)) boxes.forEach((b) => { if (b.dataset.acc === 'eurde') b.checked = false; });
    });
  }
  function updateOwnershipTotal() {
    const box = $('#own-total');
    if (!box) return;
    let total = 0;
    let owners = 0;
    $$('[data-repeater="associated_persons"] [data-item]').forEach((it) => {
      const own = $('[name$=".has_ownership"]', it);
      if (own && own.checked) { owners++; total += parseFloat(($('[data-ownership]', it) || {}).value || 0) || 0; }
    });
    box.hidden = owners === 0;
    box.textContent = `Total ownership declared: ${Math.round(total * 100) / 100}%`;
    box.classList.toggle('bad', total > 100);
  }

  // ------------------------------------------------------------------ errors
  function slotFor(path) {
    const visibleFirst = (sel) => { const all = $$(sel, form); return all.find((x) => !x.closest('.is-off')) || all[0]; };
    let el = visibleFirst(`[data-error-for="${q(path)}"]`);
    if (!el) {
      const f = visibleFirst(`[data-field="${q(path)}"]`);
      if (f) el = f;
    }
    if (!el) {
      const input = named(path);
      if (input) el = input.closest('[data-field]') || input.parentElement;
    }
    if (!el) return null;
    if (el.classList.contains('field-error')) return el;
    let slot = $(':scope > .field-error', el) || $('.field-error', el);
    if (!slot) { slot = document.createElement('p'); slot.className = 'field-error'; el.appendChild(slot); }
    return slot;
  }
  function showError(path, msg) {
    const slot = slotFor(path);
    if (!slot) return false;
    slot.textContent = Array.isArray(msg) ? msg[0] : msg;
    const f = slot.closest('[data-field]');
    if (f) f.classList.add('has-error');
    return true;
  }
  function clearError(path) {
    const slot = slotFor(path);
    if (slot) { slot.textContent = ''; const f = slot.closest('[data-field]'); if (f) f.classList.remove('has-error'); }
  }
  function clearErrors(root) {
    $$('.field-error', root).forEach((s) => { s.textContent = ''; });
    $$('.has-error', root).forEach((s) => s.classList.remove('has-error'));
    globalError('');
  }
  function globalError(msg) {
    const b = $('#global-error');
    b.textContent = msg;
    b.hidden = !msg;
    if (msg) b.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  function applyErrors(errors) {
    const unplaced = [];
    Object.entries(errors).forEach(([path, msgs]) => {
      const p = path.replace(/\.\*\./g, '.0.');
      if (!showError(p, msgs)) unplaced.push(Array.isArray(msgs) ? msgs[0] : msgs);
    });
    if (unplaced.length) globalError(unplaced.join(' '));
    const first = $('.has-error', steps[current]) || $('.field-error:not(:empty)', steps[current]);
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  function stepIndexForErrors(errors) {
    let best = Infinity;
    Object.keys(errors).forEach((path) => {
      const slot = slotFor(path.replace(/\.\*\./g, '.0.'));
      const st = slot && slot.closest('.step');
      if (st) best = Math.min(best, steps.indexOf(st));
    });
    return best === Infinity ? -1 : best;
  }

  // ------------------------------------------------------------------ client-side checks (fast feedback; server is authoritative)
  function clientCheck(step) {
    const errs = {};
    const add = (p, m) => { if (!errs[p]) errs[p] = [m]; };
    const visible = (el) => !el.closest('.is-off');
    $$('[name]', step).forEach((el) => {
      if (!visible(el) || el.disabled || el.type === 'file') return;
      const v = (el.value || '').trim();
      const required = el.required || el.dataset.required === '1';
      if (el.type === 'radio' || el.type === 'checkbox') return;
      if (required && !v) {
        if (el.type === 'hidden' && el.closest('[data-uploader]')) add(el.name, 'Please upload this document.');
        else if (el.type === 'hidden' && el.closest('[data-typeahead]')) add(el.name, 'Select an option from the list.');
        else add(el.name, 'This field is required.');
        return;
      }
      if (!v) return;
      if (el.dataset.name !== undefined && boot.enums.banned_name_words.some((w) => v.toLowerCase().includes(w)))
        add(el.name, 'This name can\'t be accepted. Please enter your legal name exactly as it appears on your ID.');
      if (el.dataset.email !== undefined) {
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v)) add(el.name, 'Enter a valid email address.');
        else if (boot.enums.banned_email_words.some((w) => v.toLowerCase().includes(w))) add(el.name, 'This email address can\'t be accepted. Please use a different email address.');
      }
      if (el.dataset.digits && !new RegExp(`^\\d{${el.dataset.digits}}$`).test(v)) add(el.name, `Must be exactly ${el.dataset.digits} digits.`);
      if (el.dataset.minlen && v.length < Number(el.dataset.minlen)) add(el.name, `Please write at least ${el.dataset.minlen} characters.`);
      if (el.dataset.regex) { try { if (!new RegExp(el.dataset.regex).test(v)) add(el.name, `Enter a valid postal code${el.placeholder ? ` (${el.placeholder})` : ''}.`); } catch (_) { /* ignore */ } }
      if (el.type === 'date') {
        if (el.max && v > el.max) add(el.name, el.dataset.date === 'birth' ? 'You must be at least 18 years old.' : 'This date must be in the past.');
        if (el.min && v < el.min) add(el.name, el.dataset.date === 'future' ? 'This document has expired.' : 'Enter a valid date.');
      }
    });
    $$('[data-field] .segmented', step).forEach((seg) => {
      const f = seg.closest('[data-field]');
      if (!visible(f) || !$('.req', f)) return;
      if (!$$('input', seg).some((i) => i.checked)) add(f.dataset.field, 'Please choose Yes or No.');
    });
    return errs;
  }

  // ------------------------------------------------------------------ navigation
  function buildRail() {
    const ol = $('#steps');
    ol.innerHTML = steps.map((s, i) => `
      <li data-i="${i}"><button type="button">
        <span class="dot"><svg class="ico sm"><use href="#${s.dataset.icon || 'i-check'}"/></svg><svg class="ico sm tick"><use href="#i-check"/></svg></span>
        <span class="txt"><strong>${esc(s.dataset.title)}</strong><small>${esc(s.dataset.sub || '')}</small></span>
      </button></li>`).join('');
    ol.addEventListener('click', (e) => {
      const li = e.target.closest('li');
      if (!li) return;
      const i = Number(li.dataset.i);
      if (i <= reached) show(i);
    });
  }
  function show(i) {
    current = Math.max(0, Math.min(steps.length - 1, i));
    reached = Math.max(reached, current);
    steps.forEach((s, k) => s.classList.toggle('active', k === current));
    $$('#steps li').forEach((li, k) => {
      li.classList.toggle('active', k === current);
      li.classList.toggle('done', k < reached || (k < current));
      li.classList.toggle('locked', k > reached);
    });
    $('#back').style.visibility = current === 0 ? 'hidden' : 'visible';
    const isLast = current === steps.length - 1;
    $('#next span').textContent = isLast ? 'Submit verification' : 'Continue';
    $('#m-step').textContent = `Step ${current + 1} of ${steps.length} · ${steps[current].dataset.title}`;
    $('#m-bar').style.width = `${((current + 1) / steps.length) * 100}%`;
    if (isLast) renderReview();
    globalError('');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  function busy(on) {
    const b = $('#next');
    b.disabled = on;
    b.classList.toggle('loading', on);
  }

  async function next() {
    const step = steps[current];
    clearErrors(step);
    if (current === steps.length - 1) return submit();
    const errs = clientCheck(step);
    if (Object.keys(errs).length) { applyErrors(errs); return; }
    if (boot.preview) { show(current + 1); return; }
    busy(true);
    const r = await api('/api/validate', { json: { data: collect(), steps: [step.dataset.step] } });
    busy(false);
    if (r.status === 0 || r.status >= 500 || r.status === 401 || r.status === 409) { globalError(r.data.message || 'Something went wrong. Please try again.'); return; }
    if (r.data.errors && Object.keys(r.data.errors).length) { applyErrors(r.data.errors); return; }
    dirty = false;
    show(current + 1);
  }

  async function submit() {
    const consent = named('consent');
    if (!consent.checked) { showError('consent', 'Please confirm the declaration to continue.'); return; }
    if (boot.preview) { globalError('Preview mode — submissions are disabled.'); return; }
    busy(true);
    $('#next span').textContent = 'Submitting securely…';
    const r = await api('/api/submit', { json: { data: collect() } });
    busy(false);
    $('#next span').textContent = 'Submit verification';
    if (r.status === 422 && r.data.errors) {
      const idx = stepIndexForErrors(r.data.errors);
      if (idx >= 0 && idx !== current) show(idx);
      clearErrors(steps[current]);
      applyErrors(r.data.errors);
      globalError(r.data.source === 'review'
        ? 'Some details need correcting before we can continue. Please review the highlighted fields.'
        : 'Please correct the highlighted fields.');
      return;
    }
    if (r.data && r.data.status && r.data.title) { done(r.data); return; }
    globalError(r.data.message || 'We could not submit your verification. Please try again.');
  }

  function done(res) {
    form.hidden = true;
    const d = $('#done');
    d.hidden = false;
    const icon = $('.status-icon', d);
    icon.className = `status-icon ${res.kind || 'success'}`;
    $('use', icon).setAttribute('href', res.kind === 'error' ? '#i-alert' : res.kind === 'pending' ? '#i-clock' : '#i-check');
    $('#done-title').textContent = res.title;
    $('#done-text').textContent = res.message;
    if (res.return_url && res.kind === 'success') { const a = $('#done-return'); a.href = res.return_url; a.hidden = false; }
    $$('#steps li').forEach((li) => { li.classList.add('done'); li.classList.remove('active', 'locked'); });
    $('.mobile-progress').hidden = true;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // ------------------------------------------------------------------ review
  function labelOf(el) {
    const f = el.closest('[data-field]');
    const l = f && $('.label', f);
    if (!l) return '';
    return Array.from(l.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').replace(/\*/g, '').trim();
  }
  function displayOf(el) {
    if (el.type === 'hidden') {
      if (el.closest('[data-uploader]')) return el.value ? `📎 ${$('.fname', el.closest('[data-uploader]')).textContent}` : '';
      if (el.closest('[data-typeahead]')) return el.value ? $('input.input', el.closest('[data-typeahead]')).value : '';
      if (el.dataset.type === 'json') { try { return JSON.parse(el.value || '[]').join(', '); } catch (_) { return ''; } }
      return '';
    }
    if (el.tagName === 'SELECT') return el.value ? el.options[el.selectedIndex].textContent.trim() : '';
    if (el.type === 'radio') return el.checked ? (el.value === 'true' ? 'Yes' : el.value === 'false' ? 'No' : el.value) : '';
    if (el.type === 'checkbox') return el.checked ? 'Yes' : '';
    return el.value.trim();
  }
  function renderReview() {
    const out = [];
    steps.slice(0, -1).forEach((step, i) => {
      const rows = [];
      let lastItem = null;
      const seenGroups = new Set();
      $$('[name]', step).forEach((el) => {
        if (el.closest('.is-off') || el.type === 'file' || el.closest('[data-addr-search]')) return;
        const item = el.closest('[data-repeater="associated_persons"] [data-item]');
        if (item && item !== lastItem) { lastItem = item; rows.push(`<div class="rv-sub">${esc(personName(item) || 'Person')}</div>`); }
        if (el.dataset.type === 'array') {
          if (seenGroups.has(el.name)) return;
          seenGroups.add(el.name);
          const vals = $$(`[name="${q(el.name)}"]`, step).filter((x) => x.checked).map((x) => x.nextElementSibling.textContent.trim());
          if (vals.length) {
            const lbl = el.closest('.roles') ? 'Roles' : labelOf(el);
            rows.push(`<div class="rv-row"><span>${esc(lbl)}</span><strong>${esc(vals.join(', '))}</strong></div>`);
          }
          return;
        }
        if (el.type === 'checkbox' && el.closest('.roles')) {
          if (seenGroups.has(item)) return;
          seenGroups.add(item);
          const vals = $$('.roles input', item).filter((x) => x.checked).map((x) => x.nextElementSibling.textContent.trim());
          if (vals.length) rows.push(`<div class="rv-row"><span>Roles</span><strong>${esc(vals.join(', '))}</strong></div>`);
          return;
        }
        const v = displayOf(el);
        if (!v) return;
        let lbl = labelOf(el);
        if (el.type === 'checkbox') lbl = $('strong', el.closest('label'))?.textContent || $('.acc-body strong', el.closest('label'))?.textContent || lbl;
        if (!lbl) return;
        rows.push(`<div class="rv-row"><span>${esc(lbl)}</span><strong>${esc(v)}</strong></div>`);
      });
      if (!rows.length) return;
      out.push(`<div class="rv-card"><div class="rv-head"><h3>${esc(step.dataset.title)}</h3>
        <button type="button" class="btn-ghost sm" data-goto="${i}"><svg class="ico sm"><use href="#i-edit"/></svg> Edit</button></div>${rows.join('')}</div>`);
    });
    const box = $('#review');
    box.innerHTML = out.join('');
    $$('[data-goto]', box).forEach((b) => b.addEventListener('click', () => show(Number(b.dataset.goto))));
  }

  // ------------------------------------------------------------------ returning customer: load saved details
  function initPrefill() {
    const box = $('#prefill');
    if (!box) return;
    const err = $('.pf-error', box);
    const msg = $('.pf-msg', box);
    const otp = $('.otp', box);
    const stage = (name) => $$('[data-pf]', box).forEach((el) => { el.hidden = el.dataset.pf !== name; });
    const setBusy = (btn, on) => { btn.disabled = on; };

    $$('[data-pf-send]', box).forEach((btn) => btn.addEventListener('click', async () => {
      err.textContent = '';
      setBusy(btn, true);
      const r = await api('/api/prefill/send', { json: {} });
      setBusy(btn, false);
      if (!r.ok) { err.textContent = r.data.message || 'We could not send the code. Please try again.'; return; }
      msg.innerHTML = `We sent a 6-digit code to <b>${esc(r.data.email)}</b>. It expires in 10 minutes.`;
      stage('code');
      otp.value = '';
      otp.focus();
    }));
    otp.addEventListener('input', () => { otp.value = otp.value.replace(/\D/g, '').slice(0, 6); err.textContent = ''; });
    otp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); $('[data-pf-verify]', box).click(); } });
    const verifyBtn = $('[data-pf-verify]', box);
    verifyBtn.addEventListener('click', async () => {
      if (otp.value.length !== 6) { err.textContent = 'Enter the 6-digit code from your email.'; return; }
      setBusy(verifyBtn, true);
      const r = await api('/api/prefill/verify', { json: { code: otp.value } });
      setBusy(verifyBtn, false);
      if (!r.ok) { err.textContent = r.data.message || 'Verification failed.'; return; }
      fill(r.data.data || {});
      syncWidgets();
      refreshConditions();
      reached = steps.length - 1;
      show(0);
      box.classList.add('ok');
      box.innerHTML = '<div class="prefill-icon" aria-hidden="true"><svg class="ico"><use href="#i-check"/></svg></div>' +
        '<div class="prefill-body"><strong>Your saved details are loaded</strong><p>Please review each step, update anything that changed, and submit again.</p></div>';
    });
    $('[data-pf-dismiss]', box).addEventListener('click', () => box.remove());
  }

  // ------------------------------------------------------------------ boot
  function init() {
    initPrefill();
    buildRail();
    initWidgets(form);
    initRepeaters();
    initAccounts();

    const draft = boot.draft || {};
    const hasDraft = Object.keys(draft).length > 0;
    if (hasDraft) {
      fill(draft);
      reached = steps.length - 1;
    } else {
      $$('[data-acc][data-default]').forEach((b) => { b.checked = true; });
    }
    syncWidgets();
    refreshConditions();

    form.addEventListener('input', (e) => {
      dirty = true;
      if (e.target.name) clearError(e.target.name);
      if (e.target.dataset.digitsOnly !== undefined || e.target.dataset.digits) e.target.value = e.target.value.replace(/\D/g, '');
      if (e.target.dataset.personName !== undefined) { const rep = e.target.closest('[data-repeater]'); if (rep) updateRepeater(rep); }
      queueConditions();
    });
    form.addEventListener('change', (e) => {
      dirty = true;
      if (e.target.name) clearError(e.target.name);
      const f = e.target.closest('[data-field]');
      if (f) f.classList.remove('has-error');
      queueConditions();
    });
    $('#next').addEventListener('click', next);
    $('#back').addEventListener('click', () => show(current - 1));
    form.addEventListener('submit', (e) => { e.preventDefault(); next(); });

    // Autosave the (encrypted, server-side) draft a few seconds after any change.
    if (!boot.preview) {
      setInterval(() => {
        if (!dirty || $('#done').hidden === false) return;
        dirty = false;
        api('/api/draft', { json: { data: collect() } });
      }, 3000);
    }

    const errs = boot.server_errors || {};
    if (Object.keys(errs).length) {
      const idx = stepIndexForErrors(errs);
      show(idx >= 0 ? idx : 0);
      applyErrors(errs);
      globalError('Some details need correcting. Please review the highlighted fields and submit again.');
    } else {
      show(0);
    }
  }

  init();
})();
