(() => {
  'use strict';
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // Clickable table rows
  $$('tr[data-href]').forEach((tr) => tr.addEventListener('click', (e) => {
    if (!e.target.closest('a, button')) window.location = tr.dataset.href;
  }));

  // Confirmation for destructive / outbound actions
  $$('form[data-confirm]').forEach((f) => f.addEventListener('submit', (e) => {
    if (!window.confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // Copy buttons
  $$('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
    const input = document.querySelector(b.dataset.copy);
    try { await navigator.clipboard.writeText(input.value); } catch (_) { input.select(); document.execCommand('copy'); }
    const t = b.textContent;
    b.textContent = 'Copied';
    setTimeout(() => { b.textContent = t; }, 1500);
  }));

  // Theme editor: presets + live preview
  const pv = document.getElementById('pv');
  if (pv) {
    const map = { primary: '--p', primary_contrast: '--pc', accent: '--a', background: '--bgc', surface: '--s', text: '--t', muted: '--m', border: '--b', danger: '--d' };
    const paint = () => {
      $$('[data-color]').forEach((inp) => {
        pv.style.setProperty(map[inp.dataset.color], inp.value);
        const code = document.querySelector(`[data-hex="${inp.dataset.color}"]`);
        if (code) code.textContent = inp.value.toUpperCase();
      });
      const r = document.querySelector('[data-radius]');
      pv.style.setProperty('--r', `${r.value}px`);
      document.getElementById('radius-val').textContent = `${r.value}px`;
      pv.style.fontFamily = `'${document.querySelector('[data-font]').value}', Inter, sans-serif`;
    };
    $$('[data-color], [data-radius], [data-font]').forEach((i) => i.addEventListener('input', paint));
    $$('[data-preset]').forEach((b) => b.addEventListener('click', () => {
      const p = JSON.parse(b.dataset.preset);
      Object.entries(p).forEach(([k, v]) => {
        const inp = document.querySelector(`[data-color="${k}"]`);
        if (inp) inp.value = v.toLowerCase();
      });
      paint();
    }));
    paint();
  }
})();
