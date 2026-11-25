// Shared behaviour for every SIREN page: API calls, errors, toasts, drawers,
// keyboard shortcuts, theme and the system status in the top bar.

const SIREN = (() => {
  const COLOUR_CLASS = { MERAH: 'red', KUNING: 'yellow', HIJAU: 'green' };
  const SOURCE = {
    SENSOR: { text: 'SENSOR', cls: '' },
    MANUAL: { text: 'MANUAL', cls: 'manual' },
    MOCK: { text: 'SIMULASI', cls: 'mock' },
  };
  const ACVPU = {
    alert: 'Alert', confusion: 'Bingung baru', voice: 'Respons suara', pain: 'Respons nyeri', unresponsive: 'Tidak responsif',
  };

  // ------------------------------------------------------------- formatting
  function num(value, digits = 0) {
    if (value === null || value === undefined || value === '') return '—';
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(digits) : '—';
  }
  function time(iso, seconds = false) {
    if (!iso) return '—';
    const opts = { hour: '2-digit', minute: '2-digit', hour12: false };
    if (seconds) opts.second = '2-digit';
    return new Date(iso).toLocaleTimeString('id-ID', opts).replace(/\./g, ':');
  }
  function dateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return `${d.toLocaleDateString('id-ID', { day: 'numeric', month: 'short', year: 'numeric' })}, ${time(iso)}`;
  }
  function elapsed(iso) {
    if (!iso) return '—';
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    const m = Math.floor(s / 60);
    if (m < 1) return `${s} dtk`;
    if (m < 60) return `${m} mnt`;
    return `${Math.floor(m / 60)} j ${m % 60} mnt`;
  }
  function setText(id, value) {
    const node = typeof id === 'string' ? document.getElementById(id) : id;
    if (node) node.textContent = value;
  }
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of [].concat(children)) if (child !== null && child !== undefined) node.append(child);
    return node;
  }
  function colourTag(colour) {
    return el('span', { class: `tag ${COLOUR_CLASS[colour] || ''}`, text: colour || '—' });
  }

  // -------------------------------------------------------------------- API
  const FALLBACK = {
    400: 'Permintaan tidak dapat diproses.',
    404: 'Data atau halaman tidak ditemukan.',
    409: 'Aksi ini tidak dapat dilakukan pada data saat ini.',
    422: 'Ada isian yang belum benar.',
    503: 'Layanan sedang tidak tersedia.',
  };

  async function api(path, { method = 'GET', body, timeout = 10000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      let data = null;
      try { data = await response.json(); } catch (error) { /* not JSON */ }
      connection.ok();
      if (!response.ok) {
        const detail = data && typeof data.detail === 'string' ? data.detail : null;
        return {
          ok: false,
          status: response.status,
          error: detail || FALLBACK[response.status] || (response.status >= 500
            ? 'Terjadi kesalahan di server SIREN. Coba lagi.'
            : `Permintaan gagal (kode ${response.status}).`),
          fields: (data && data.errors) || {},
        };
      }
      return { ok: true, status: response.status, data };
    } catch (error) {
      connection.lost();
      return {
        ok: false,
        status: 0,
        offline: true,
        fields: {},
        error: error.name === 'AbortError'
          ? 'Server SIREN tidak merespons. Coba lagi sebentar.'
          : 'Tidak dapat terhubung ke server SIREN.',
      };
    } finally {
      clearTimeout(timer);
    }
  }

  // ------------------------------------------------------------- connection
  const connection = (() => {
    let lastOk = new Date();
    let down = false;
    function banner() { return document.getElementById('connBanner'); }
    return {
      ok() {
        lastOk = new Date();
        if (down) {
          down = false;
          banner()?.classList.add('hidden');
          toast('Koneksi ke server SIREN pulih.', { type: 'success' });
        }
      },
      lost() {
        const node = banner();
        if (!node) return;
        down = true;
        node.classList.remove('hidden');
        setText('connSince', time(lastOk.toISOString(), true));
      },
      get down() { return down; },
    };
  })();

  // ------------------------------------------------------------------ toast
  const TOAST_TIME = { success: 3500, info: 4500, warn: 7000, error: 0 };
  function toast(message, { type = 'info', action, timeout } = {}) {
    const box = document.getElementById('toasts');
    if (!box) return { close() {} };
    const node = el('div', { class: `toast ${type}`, role: type === 'error' ? 'alert' : 'status' });
    const close = () => node.remove();
    const actions = el('div', { class: 'toast-actions' });
    if (action) {
      actions.append(el('button', {
        class: 'btn btn-sm', type: 'button', text: action.label,
        onclick: () => { close(); action.onClick(); },
      }));
    }
    actions.append(el('button', { class: 'btn btn-sm btn-ghost', type: 'button', text: 'Tutup', 'aria-label': 'Tutup notifikasi', onclick: close }));
    node.append(el('p', { text: message }), actions);
    box.append(node);
    while (box.children.length > 4) box.firstElementChild.remove();
    const ms = timeout ?? TOAST_TIME[type];
    if (ms) setTimeout(close, ms);
    return { close };
  }

  // ------------------------------------------------------------------ forms
  function clearErrors(form) {
    form.querySelectorAll('[aria-invalid="true"]').forEach((node) => node.removeAttribute('aria-invalid'));
    form.querySelectorAll('.field-error').forEach((node) => { node.textContent = ''; });
    form.querySelectorAll('.form-alert').forEach((node) => { node.textContent = ''; });
  }

  // Marks each field the server rejected and shows the rest in the form alert.
  function showErrors(form, result) {
    clearErrors(form);
    const loose = [];
    let first = null;
    for (const [name, message] of Object.entries(result.fields || {})) {
      const input = form.querySelector(`[name="${name}"]`);
      const holder = input && input.closest('.field');
      const slot = holder && holder.querySelector('.field-error');
      if (input && slot) {
        input.setAttribute('aria-invalid', 'true');
        slot.textContent = message;
        first = first || input;
      } else {
        loose.push(message);
      }
    }
    const alert = form.querySelector('.form-alert');
    const summary = loose.length ? loose.join(' ') : (first ? 'Periksa isian yang ditandai merah.' : result.error);
    if (alert) alert.textContent = summary;
    else if (summary) toast(summary, { type: 'error' });
    if (first) first.focus();
  }

  // Client-side check before sending, using the inputs' own min/max/required.
  function checkNumbers(form) {
    const fields = {};
    form.querySelectorAll('input[type="number"]').forEach((input) => {
      if (input.value === '') {
        if (input.required) fields[input.name] = `${labelOf(input)} wajib diisi.`;
        return;
      }
      const value = Number(input.value);
      const unit = input.dataset.unit ? ` ${input.dataset.unit}` : '';
      if (!Number.isFinite(value)) fields[input.name] = `${labelOf(input)} harus berupa angka.`;
      else if (input.min !== '' && value < Number(input.min)) fields[input.name] = `${labelOf(input)} minimal ${input.min}${unit}.`;
      else if (input.max !== '' && value > Number(input.max)) fields[input.name] = `${labelOf(input)} maksimal ${input.max}${unit}.`;
    });
    return fields;
  }
  function labelOf(input) {
    const label = input.closest('.field')?.querySelector('span');
    return label ? label.textContent.replace(/\s*\(.*\)\s*$/, '').trim() : input.name;
  }

  async function busy(button, task) {
    if (!button) return task();
    button.classList.add('is-busy');
    button.disabled = true;
    try { return await task(); } finally {
      button.classList.remove('is-busy');
      button.disabled = false;
    }
  }

  // Downloads through fetch so a server error becomes a message, not a broken file.
  async function download(url, filename, button) {
    return busy(button, async () => {
      try {
        const response = await fetch(url);
        if (!response.ok) {
          let detail = null;
          try { detail = (await response.json()).detail; } catch (error) { /* ignore */ }
          toast(detail || `Unduhan gagal (kode ${response.status}).`, { type: 'error' });
          return false;
        }
        const blob = await response.blob();
        const link = el('a', { href: URL.createObjectURL(blob), download: filename });
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(link.href), 4000);
        toast(`${filename} tersimpan di folder Unduhan.`, { type: 'success' });
        return true;
      } catch (error) {
        connection.lost();
        toast('Unduhan gagal: server SIREN tidak dapat dihubungi.', { type: 'error' });
        return false;
      }
    });
  }

  // ----------------------------------------------------------------- drawer
  let openDrawerNode = null;
  let returnFocus = null;
  function openDrawer(id) {
    closeDrawer();
    const drawer = document.getElementById(id);
    if (!drawer) return;
    returnFocus = document.activeElement;
    openDrawerNode = drawer;
    document.getElementById('backdrop')?.classList.add('open');
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    drawer.dispatchEvent(new CustomEvent('drawer:open'));
    setTimeout(() => drawer.querySelector('input:not([type="checkbox"]):not([type="radio"]), textarea, select, button.btn-primary')?.focus(), 60);
  }
  function closeDrawer() {
    if (!openDrawerNode) return;
    openDrawerNode.classList.remove('open');
    openDrawerNode.setAttribute('aria-hidden', 'true');
    document.getElementById('backdrop')?.classList.remove('open');
    openDrawerNode = null;
    returnFocus?.focus?.();
  }

  // -------------------------------------------------------------- shortcuts
  const shortcuts = [];
  function shortcut(keys, description, handler, { inInputs = false } = {}) {
    shortcuts.push({ keys: [].concat(keys), description, handler, inInputs });
  }
  function typing(target) {
    return target.closest('input, textarea, select, [contenteditable="true"]')
      && !['radio', 'checkbox'].includes(target.type);
  }
  function keyName(event) {
    const parts = [];
    if (event.ctrlKey) parts.push('Ctrl');
    if (event.altKey) parts.push('Alt');
    parts.push(event.key.length === 1 ? event.key.toUpperCase() : event.key);
    return parts.join('+');
  }
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      const help = document.getElementById('helpDialog');
      if (help?.open) { event.preventDefault(); help.close(); return; }
      if (openDrawerNode) { event.preventDefault(); closeDrawer(); }
      return;
    }
    const name = keyName(event);
    if (name === '?' || name === 'Shift+?') {
      if (typing(event.target)) return;
      event.preventDefault();
      showHelp();
      return;
    }
    for (const item of shortcuts) {
      if (!item.keys.includes(name)) continue;
      if (!item.inInputs && typing(event.target)) return;
      if (openDrawerNode && !item.inInputs) return;
      event.preventDefault();
      item.handler(event);
      return;
    }
  });

  function showHelp() {
    const dialog = document.getElementById('helpDialog');
    if (!dialog) return;
    const list = dialog.querySelector('.shortcut-list');
    const rows = [...shortcuts, { keys: ['?'], description: 'Tampilkan daftar pintasan ini' }, { keys: ['Esc'], description: 'Tutup panel' }];
    list.replaceChildren(...rows.flatMap((item) => [
      el('dt', {}, item.keys.map((k) => el('kbd', { text: k }))),
      el('dd', { text: item.description }),
    ]));
    dialog.showModal();
  }

  // ------------------------------------------------------------------ theme
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('siren_theme', theme); } catch (error) { /* storage unavailable */ }
    const button = document.getElementById('themeBtn');
    if (button) {
      // The button shows a moon in light mode and a sun in dark mode (CSS)
      const label = theme === 'dark' ? 'Ganti ke tema terang' : 'Ganti ke tema gelap';
      button.setAttribute('aria-label', label);
      button.title = label;
    }
  }

  // ----------------------------------------------------------- system state
  function setDot(id, state, text, title) {
    const node = document.getElementById(id);
    if (!node) return;
    node.querySelector('.dot').className = `dot ${state}`;
    node.querySelector('.txt').textContent = text;
    node.title = title;
  }
  function renderSensors(s) {
    const ppg = s.ppg.status === 'CONNECTED'
      ? (s.ppg.finger === false ? ['warn', 'Jari?', 'Sensor PPG terhubung, jari tidak terdeteksi'] : ['ok', 'PPG', 'Sensor PPG terhubung'])
      : s.ppg.status === 'STALE' ? ['warn', 'PPG', 'Sensor PPG berhenti mengirim data'] : ['bad', 'PPG', 'Sensor PPG belum terdeteksi'];
    const temp = s.temp.via
      ? ['ok', 'Suhu', `Sensor suhu terhubung (${s.temp.via})`]
      : s.temp.status === 'STALE' ? ['warn', 'Suhu', 'Sensor suhu berhenti mengirim data'] : ['bad', 'Suhu', 'Sensor suhu belum terdeteksi'];
    setDot('sysPpg', ...ppg);
    setDot('sysTemp', ...temp);
    document.getElementById('simBadge')?.classList.toggle('hidden', s.mode !== 'MOCK');
  }
  function renderPrinter(p) {
    if (p.backend === 'none') setDot('sysPrinter', '', 'Printer', 'Printer belum diatur');
    else if (p.backend === 'file') setDot('sysPrinter', 'warn', 'Printer', 'Mode file: label disimpan sebagai gambar');
    else if (p.reachable === false) setDot('sysPrinter', 'bad', 'Printer', `Printer tidak ditemukan di ${p.device}`);
    else setDot('sysPrinter', 'ok', 'Printer', `Printer siap (${p.device})`);
  }
  async function refreshSystem() {
    const [sensors, printer] = await Promise.all([api('/api/sensors/status'), api('/api/printer/status')]);
    if (sensors.ok) renderSensors(sensors.data);
    if (printer.ok) renderPrinter(printer.data);
  }

  // ----------------------------------------------------------- notifications
  // MERAH: a short alarm (three bursts). NEWS2 rise and cuff reminders: one beep.
  // Sound is synthesised, so it works offline with no audio files.
  const sound = (() => {
    let ctx = null;
    let loop = null;
    function context() {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return null;
      if (!ctx) ctx = new Ctx();
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      return ctx;
    }
    function tone(freq, start, length) {
      const c = context();
      if (!c) return;
      const osc = c.createOscillator();
      const gain = c.createGain();
      osc.type = 'square';
      osc.frequency.value = freq;
      const t0 = c.currentTime + start;
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.18, t0 + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + length);
      osc.connect(gain).connect(c.destination);
      osc.start(t0);
      osc.stop(t0 + length + 0.02);
    }
    function alarmBurst() { [0, 0.22, 0.44].forEach((s) => tone(988, s, 0.16)); }
    return {
      // Browsers may block sound until the page has been touched once
      unlock: () => context(),
      blocked: () => !ctx || ctx.state !== 'running',
      beep() { tone(660, 0, 0.14); tone(880, 0.18, 0.18); },
      alarm() {
        if (loop) return;
        let left = 3;
        alarmBurst();
        loop = setInterval(() => { left -= 1; if (left <= 0) { clearInterval(loop); loop = null; } else alarmBurst(); }, 1500);
      },
      stop() { clearInterval(loop); loop = null; },
    };
  })();

  // A small list at the top left. A new card comes in on top and pushes the
  // older ones down. A NEWS2 rise leaves after 5 s and MERAH after 7 s,
  // or on a tap. A cuff reminder stays until "Ukur".
  const alerts = (() => {
    const SHOWN = 3;                 // cards visible at once
    const GAP = 8;
    const SHOW_MS = { beep: 5000, alarm: 7000 };
    const cards = new Map();         // id -> { item, node }
    const sounded = new Set();
    const gone = new Set();          // closed on this screen; the server may still list it a moment
    let order = [];                  // ids, newest first

    function iconFor(item) {
      const tpl = document.getElementById('alertIcons');
      const src = tpl && tpl.content.querySelector(`[data-icon="${item.action === 'cuff' ? 'cuff' : 'warn'}"]`);
      return src ? src.firstElementChild.cloneNode(true) : el('span');
    }

    function build(item) {
      const tone = item.level === 'alarm' ? 'alarm' : item.level === 'remind' ? 'remind' : 'beep';
      const node = el('section', { class: `alert-card ${tone} entering`, role: item.level === 'alarm' ? 'alert' : 'status' });
      const body = el('div', { class: 'alert-card-body' }, [
        el('div', { class: 'alert-card-head' }, [
          el('strong', { class: 'alert-card-title', text: item.title || '' }),
          el('time', { class: 'alert-card-time', text: time(item.created_at, false) }),
        ]),
        el('p', { class: 'alert-card-msg', text: item.message || '' }),
      ]);
      node.append(iconFor(item), body);
      if (item.action === 'cuff') {
        const go = el('button', { type: 'button', class: 'alert-card-btn', text: 'Ukur' });
        go.addEventListener('click', (ev) => { ev.stopPropagation(); act(item); });
        node.append(go);
      } else {
        node.title = 'Sentuh untuk menutup';
        node.addEventListener('click', () => dismiss(item));
      }
      return node;
    }

    function layout() {
      let y = 0;
      order.forEach((id, i) => {
        const { node } = cards.get(id);
        node.style.zIndex = String(10 - i);
        node.style.transform = `translateY(${y}px)`;
        node.style.opacity = i < SHOWN ? '' : '0';
        node.style.pointerEvents = i < SHOWN ? '' : 'none';
        y += node.offsetHeight + GAP;
      });
      const muted = order.length && sound.blocked();
      document.getElementById('alertStack')?.classList.toggle('muted', Boolean(muted));
    }

    function remove(id) {
      const entry = cards.get(id);
      if (!entry) return;
      cards.delete(id);
      order = order.filter((x) => x !== id);
      entry.node.classList.add('leaving');
      setTimeout(() => entry.node.remove(), 240);
    }

    function render(all) {
      const stack = document.getElementById('alertStack');
      if (!stack) return;
      const items = all.filter((i) => !gone.has(i.id));
      const ids = new Set(items.map((i) => i.id));
      [...cards.keys()].filter((id) => !ids.has(id)).forEach(remove);
      items.forEach((item) => {
        if (cards.has(item.id)) return;
        const node = build(item);
        cards.set(item.id, { item, node });
        stack.prepend(node);
        requestAnimationFrame(() => requestAnimationFrame(() => node.classList.remove('entering')));
        if (!sounded.has(item.id)) {
          sounded.add(item.id);
          if (item.level === 'alarm') sound.alarm(); else sound.beep();
        }
        if (item.action !== 'cuff' && SHOW_MS[item.level]) {
          setTimeout(() => { gone.add(item.id); remove(item.id); layout(); }, SHOW_MS[item.level]);
        }
      });
      order = items.map((i) => i.id);   // the server lists newest first
      layout();
    }

    async function poll() {
      if (document.body.dataset.alerts === 'off') return;
      const result = await api('/api/notifications');
      if (result.ok) render(result.data.items);
    }

    async function dismiss(item) {
      gone.add(item.id);
      remove(item.id);
      layout();
      if (item.level === 'alarm') sound.stop();
      const result = await api(`/api/notifications/${item.id}/ack`, { method: 'POST' });
      if (!result.ok) toast(`Notifikasi belum tertutup. ${result.error}`, { type: 'error' });
    }

    // K closes the newest alert; a cuff reminder only goes with "Ukur"
    function acknowledge() {
      const id = order.find((x) => cards.get(x).item.action !== 'cuff');
      if (id) dismiss(cards.get(id).item);
    }

    // "Ukur": the monitor keeps the PPG from before the cuff inflates; any
    // other page goes to the monitor first.
    async function act(item) {
      if (typeof window.sirenCuffStart === 'function') {
        await dismiss(item);
        window.sirenCuffStart();
      } else {
        window.location.href = '/monitor?cuff=1';
      }
    }

    return { poll, acknowledge, act };
  })();

  function startClock() {
    const node = document.getElementById('clock');
    if (!node) return;
    const tick = () => { node.textContent = time(new Date().toISOString(), true); };
    tick();
    setInterval(tick, 1000);
  }

  document.addEventListener('DOMContentLoaded', () => {
    startClock();
    applyTheme(document.documentElement.dataset.theme || 'light');
    document.getElementById('themeBtn')?.addEventListener('click', () => {
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    });
    document.getElementById('helpBtn')?.addEventListener('click', showHelp);
    document.getElementById('backdrop')?.addEventListener('click', closeDrawer);
    document.querySelectorAll('[data-close-drawer]').forEach((b) => b.addEventListener('click', closeDrawer));
    document.querySelectorAll('[data-close-dialog]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));
    refreshSystem();
    setInterval(refreshSystem, 5000);
    ['pointerdown', 'keydown'].forEach((type) => document.addEventListener(type, sound.unlock, { passive: true }));
    shortcut('K', 'Tutup notifikasi', () => alerts.acknowledge());
    window.sirenAlerts = alerts;   // pages refresh the stack right after an action that closes a reminder
    if (document.body.dataset.alerts !== 'off') {
      alerts.poll();
      setInterval(alerts.poll, 2000);
    }
    // /monitor#drawerManual opens that panel once the page has its data
    const hash = window.location.hash.slice(1);
    if (hash && document.getElementById(hash)?.classList.contains('drawer')) setTimeout(() => openDrawer(hash), 600);
  });

  return {
    COLOUR_CLASS, SOURCE, ACVPU,
    num, time, dateTime, elapsed, setText, el, colourTag,
    api, toast, connection, clearErrors, showErrors, checkNumbers, busy, download,
    openDrawer, closeDrawer, shortcut, renderSensors,
  };
})();
