'use strict';

const VARIOUS_ARTISTS = '89ad4ac3-39f7-470e-963a-56509c546377';
const CACHE_KEY = 'muspy-import:mb:v1';
const MB_DELAY = 1100;

const $ = sel => document.querySelector(sel);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Artistas indexados por nombre normalizado.
const artists = new Map();
let filter = 'all';
let stopRequested = false;

// ---------- Utilidades de texto ----------

function norm(s) {
  return s.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .replace(/&/g, ' and ')
    .replace(/^(the|el|la|los|las)\s+/, '')
    .replace(/,\s*the$/, '')
    .replace(/[^\p{L}\p{N}]+/gu, '');
}

function similar(a, b) {
  const x = norm(a), y = norm(b);
  if (!x || !y) return false;
  if (x === y) return true;
  const [s, l] = x.length < y.length ? [x, y] : [y, x];
  return l.includes(s) && s.length / l.length >= 0.7;
}

// ---------- CSV ----------

function parseCSV(text) {
  text = text.replace(/^﻿/, '');
  const rows = [];
  let row = [], field = '', inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      if (row.some(f => f !== '')) rows.push(row);
      row = [];
    } else field += c;
  }
  row.push(field);
  if (row.some(f => f !== '')) rows.push(row);
  const header = (rows.shift() || []).map(h => h.trim());
  return rows.map(r => Object.fromEntries(header.map((h, i) => [h, (r[i] || '').trim()])));
}

function detectFormat(rows) {
  const cols = rows.length ? Object.keys(rows[0]) : [];
  if (cols.includes('release_id') && cols.includes('Artist')) return 'discogs';
  if (cols.includes('RYM Album') && cols.includes('Last Name')) return 'rym';
  return null;
}

// RYM exporta algunos textos con entidades HTML ("&amp;").
function decodeEntities(s) {
  return s.replace(/&(#x?[0-9a-f]+|amp|quot|apos|lt|gt|#39);/gi, (m, e) => {
    const named = { amp: '&', quot: '"', apos: "'", lt: '<', gt: '>' }[e.toLowerCase()];
    if (named) return named;
    return String.fromCodePoint(e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
  });
}

function extract(rows, format) {
  const out = [];
  for (const raw of rows) {
    const r = Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, decodeEntities(v)]));
    let name, title;
    if (format === 'discogs') {
      // Discogs añade "(2)" para desambiguar y "*" para variantes del nombre.
      name = r.Artist.replace(/\s\(\d+\)/g, '').replace(/\*(?=\s|,|$)/g, '').trim();
      title = r.Title;
    } else {
      name = [r['First Name'], r['Last Name']].filter(Boolean).join(' ').trim();
      title = r.Title;
    }
    if (!name || ['various', 'variousartists', 'unknownartist'].includes(norm(name))) continue;
    out.push({ name, title });
  }
  return out;
}

function addEntries(entries, source) {
  for (const { name, title } of entries) {
    const key = norm(name) || name.toLowerCase();
    let a = artists.get(key);
    if (!a) {
      a = { key, name, albums: [], sources: new Set(), result: null };
      artists.set(key, a);
    }
    if (title && !a.albums.includes(title)) a.albums.push(title);
    a.sources.add(source);
  }
}

// ---------- Caché ----------

function loadCache() {
  try { return JSON.parse(localStorage.getItem(CACHE_KEY)) || {}; } catch { return {}; }
}
let cache = loadCache();
function saveCache() {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify(cache)); } catch { /* sin almacenamiento */ }
}
function storeResult(a) {
  cache[a.key] = a.result;
  saveCache();
}

// ---------- MusicBrainz ----------

let lastRequest = 0;
async function mb(path) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const wait = lastRequest + MB_DELAY - Date.now();
    if (wait > 0) await sleep(wait);
    lastRequest = Date.now();
    const sep = path.includes('?') ? '&' : '?';
    const r = await fetch(`https://musicbrainz.org/ws/2/${path}${sep}fmt=json`);
    if (r.ok) return r.json();
    if (r.status === 503 || r.status === 429) { await sleep(2000 * (attempt + 1)); continue; }
    if (r.status === 404) return null;
    throw new Error(`MusicBrainz respondió ${r.status}`);
  }
  throw new Error('MusicBrainz no responde');
}

const lucene = s => '"' + s.replace(/[\\"]/g, '\\$&') + '"';
const describe = x => [x.disambiguation, x.type, x.country].filter(Boolean).join(' · ');
const option = list => ({ ids: list.map(x => x.id), names: list.map(x => x.name), label: list.map(x => x.name).join(' + '), desc: list.length === 1 ? describe(list[0]) : '' });

async function resolve(a) {
  // "Compositor - Intérpretes" (clásica) o "A / B": se usa también el primer nombre.
  const short = a.name.split(/\s+[-–]\s+|\s+\/\s+/)[0].trim();
  const names = short !== a.name ? [a.name, short] : [a.name];

  // 1) Buscar uno de sus discos: el artista acreditado es casi seguro el correcto.
  for (const title of a.albums.slice(0, 2)) {
    const data = await mb('release-group/?limit=5&query=' + encodeURIComponent(`releasegroup:${lucene(title)} AND artist:${lucene(a.name)}`));
    for (const rg of data?.['release-groups'] || []) {
      if (rg.score < 80) break;
      const credit = rg['artist-credit'] || [];
      const credited = credit.filter(c => c.artist.id !== VARIOUS_ARTISTS);
      if (!credited.length) continue;
      const joined = credit.map(c => c.name + (c.joinphrase || '')).join('');
      if (norm(joined) === norm(a.name)) {
        return { confidence: 'alta', options: [option(credited.map(c => c.artist))], choice: 0, checked: true };
      }
      const exact = credited.find(c => names.some(n => norm(n) === norm(c.name) || norm(n) === norm(c.artist.name)));
      if (exact) return { confidence: 'alta', options: [option([exact.artist])], choice: 0, checked: true };
      const close = credited.find(c => names.some(n => similar(n, c.artist.name)));
      if (close && rg.score >= 95) return { confidence: 'media', options: [option([close.artist])], choice: 0, checked: true };
    }
  }

  // 2) Buscar el artista por nombre.
  const query = names[names.length - 1];
  const data = await mb('artist/?limit=6&query=' + encodeURIComponent(`artist:${lucene(query)}`));
  const found = (data?.artists || []).filter(x => x.score >= 50);
  const options = found.map(x => option([x]));
  const exact = found.filter(x => norm(x.name) === norm(query) || (x.aliases || []).some(al => norm(al.name) === norm(query)));
  if (exact.length === 1 && exact[0].score >= 90) {
    return { confidence: 'media', options, choice: found.indexOf(exact[0]), checked: true };
  }
  if (exact.length) return { confidence: 'baja', options, choice: found.indexOf(exact[0]), checked: false };
  if (options.length) return { confidence: 'baja', options, choice: -1, checked: false };
  return { confidence: 'ninguna', options: [], choice: -1, checked: false };
}

async function resolveAll() {
  const todo = [...artists.values()].filter(a => !a.result);
  stopRequested = false;
  $('#btn-resolve').disabled = true;
  $('#btn-stop').hidden = false;
  $('#progress').hidden = false;
  $('#step-review').hidden = false;
  render();

  let done = 0, failures = 0, fatal = '';
  const started = Date.now();
  while (done < todo.length && !stopRequested) {
    const a = todo[done];
    updateProgress(done, todo.length, started);
    try {
      a.result = await resolve(a);
      storeResult(a);
    } catch (e) {
      if (++failures >= 3) {
        fatal = e instanceof TypeError
          ? 'No se puede conectar con musicbrainz.org desde tu red. Abre https://musicbrainz.org en otra pestaña para comprobarlo, o prueba con otra conexión o una VPN.'
          : e.message;
        break;
      }
      updateProgress(done, todo.length, started, e.message + ' — reintentando en 10 s');
      await sleep(10000);
      continue;
    }
    failures = 0;
    done++;
    renderRow(a);
    updateCounts();
  }
  updateProgress(done, todo.length, started, fatal || (stopRequested ? 'Detenido' : 'Terminado'));
  $('#btn-resolve').disabled = false;
  $('#btn-resolve').textContent = stopRequested || fatal ? 'Continuar' : 'Buscar artistas';
  $('#btn-stop').hidden = true;
  updateSend();
}

function updateProgress(done, total, started, note) {
  const pct = total ? (100 * done / total) : 100;
  $('#progress .bar').style.width = pct + '%';
  let label = `${done} de ${total}`;
  if (note) label += ` · ${note}`;
  else if (done > 0) {
    const left = Math.ceil((Date.now() - started) / done * (total - done) / 60000);
    label += ` · quedan ~${left} min`;
  }
  $('#progress .label').textContent = label;
}

// ---------- Tabla ----------

function needsReview(a) {
  return a.result && (a.result.confidence === 'baja' || a.result.confidence === 'ninguna' || (a.result.confidence !== 'alta' && a.result.options.length > 1));
}

function visible(a) {
  const q = norm($('#search').value || '');
  if (q && !norm(a.name).includes(q)) return false;
  if (filter === 'review') return needsReview(a);
  if (filter === 'none') return a.result?.confidence === 'ninguna';
  return true;
}

function rowHTML(a) {
  const r = a.result;
  const albums = a.albums.slice(0, 2).map(esc).join(' · ') + (a.albums.length > 2 ? ' …' : '');
  let match, badge;
  if (!r) {
    match = '<span class="sub">pendiente</span>';
    badge = '<span class="badge pendiente">—</span>';
  } else {
    const chosen = r.options[r.choice];
    const link = chosen ? chosen.ids.map((id, i) => `<a href="https://musicbrainz.org/artist/${id}" target="_blank" rel="noopener">${esc(chosen.names[i] || id)}</a>`).join(' + ') : '';
    if (r.options.length > 1 || r.confidence !== 'alta') {
      const opts = r.options.map((o, i) => `<option value="${i}"${i === r.choice ? ' selected' : ''}>${esc(o.label)}${o.desc ? ' (' + esc(o.desc) + ')' : ''}</option>`).join('');
      match = `<select data-act="choice"><option value="-1"${r.choice < 0 ? ' selected' : ''}>— ninguno —</option>${opts}<option value="manual">Otro (pegar MBID o enlace)…</option></select>`;
      if (link) match += `<div class="sub">${link}</div>`;
    } else {
      match = link + (chosen.desc ? `<div class="sub">${esc(chosen.desc)}</div>` : '');
    }
    badge = `<span class="badge ${r.confidence}">${r.confidence}</span>`;
  }
  const checked = r?.checked && r.choice >= 0;
  return `<td><input type="checkbox" data-act="check"${checked ? ' checked' : ''}${!r || r.choice < 0 ? ' disabled' : ''}></td>`
    + `<td>${esc(a.name)}<div class="sub">${albums}</div></td>`
    + `<td class="n">${a.albums.length}</td>`
    + `<td>${match}</td><td>${badge}</td>`;
}

function renderRow(a) {
  const tr = document.querySelector(`tr[data-key="${CSS.escape(a.key)}"]`);
  if (!tr) return;
  tr.innerHTML = rowHTML(a);
  tr.className = a.result?.checked && a.result.choice >= 0 ? '' : 'off';
  tr.hidden = !visible(a);
}

function render() {
  const list = [...artists.values()].sort((x, y) => x.name.localeCompare(y.name, 'es'));
  $('#table tbody').innerHTML = list.map(a => `<tr data-key="${esc(a.key)}"></tr>`).join('');
  list.forEach(renderRow);
  updateCounts();
}

function updateCounts() {
  const all = [...artists.values()];
  const counts = { all: all.length, review: all.filter(needsReview).length, none: all.filter(a => a.result?.confidence === 'ninguna').length };
  document.querySelectorAll('.filters button').forEach(b => { b.querySelector('span').textContent = counts[b.dataset.filter]; });
  updateSend();
}

function selectedIds() {
  const ids = new Set();
  for (const a of artists.values()) {
    const r = a.result;
    if (r?.checked && r.choice >= 0) r.options[r.choice].ids.forEach(id => ids.add(id));
  }
  return [...ids];
}

function updateSend() {
  const ids = selectedIds();
  const anyResolved = [...artists.values()].some(a => a.result);
  $('#step-send').hidden = !anyResolved;
  $('#sel-count').textContent = ids.length;
  $('#open-muspy').href = 'https://muspy.com/artists#muspy-import=' + ids.map(id => id.replace(/-/g, '')).join('');
}

async function manualChoice(a) {
  const input = prompt(`MBID o enlace de MusicBrainz para «${a.name}»:`) || '';
  const m = input.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  if (!m) { if (input) alert('No encuentro un MBID en el texto pegado.'); return; }
  const id = m[0].toLowerCase();
  let artist = { id, name: id };
  try { artist = (await mb('artist/' + id)) || artist; } catch { /* se queda el MBID como nombre */ }
  a.result.options.push(option([artist]));
  a.result.choice = a.result.options.length - 1;
  a.result.checked = true;
  a.result.confidence = 'alta';
}

// ---------- Bookmarklet (se ejecuta dentro de muspy.com) ----------

function muspyBookmarklet() {
  if (location.hostname.replace(/^www\./, '') !== 'muspy.com') { alert('Abre muspy.com, inicia sesión y vuelve a pulsar este marcador.'); return; }
  if (window.__muspyImport) return;
  const m = location.hash.match(/muspy-import=([0-9a-f]+)/i);
  const raw = (m ? m[1] : prompt('Pega la lista de artistas copiada desde la web de importación:') || '').toLowerCase().replace(/[^0-9a-f]/g, '');
  const ids = [];
  for (let i = 0; i + 32 <= raw.length; i += 32) {
    const h = raw.slice(i, i + 32);
    ids.push([h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join('-'));
  }
  if (!ids.length) { alert('No hay artistas en la lista.'); return; }
  window.__muspyImport = true;

  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:16px;right:16px;z-index:99999;width:min(420px,calc(100vw - 32px));max-height:80vh;display:flex;flex-direction:column;gap:8px;background:#fff;color:#111;border:1px solid #ccc;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.25);font:14px/1.4 system-ui,sans-serif;padding:14px;text-align:left';
  box.innerHTML = '<b>Importar a muspy</b><div data-s></div><div style="height:6px;background:#eee;border-radius:3px;overflow:hidden"><div data-b style="height:100%;width:0;background:#c2410c;transition:width .3s"></div></div><ol data-l style="overflow:auto;margin:0;padding-left:22px;font-size:13px;flex:1"></ol><button data-x>Detener</button>';
  document.body.appendChild(box);
  const el = s => box.querySelector('[data-' + s + ']');
  const log = (text, color) => {
    const li = document.createElement('li');
    li.textContent = text;
    if (color) li.style.color = color;
    el('l').appendChild(li);
    li.scrollIntoView({ block: 'nearest' });
  };
  const html = async r => new DOMParser().parseFromString(await r.text(), 'text/html');
  let stop = false, finished = false;
  const finish = text => {
    finished = true;
    window.__muspyImport = false;
    el('s').textContent = text;
    el('x').textContent = 'Cerrar y recargar';
  };
  el('x').onclick = () => { if (finished) location.href = '/artists'; else { stop = true; el('x').disabled = true; } };

  (async () => {
    el('s').textContent = 'Comprobando sesión…';
    const r = await fetch('/artists', { credentials: 'same-origin' });
    if (/\/signin/.test(r.url)) { finish('No has iniciado sesión. Entra en muspy y vuelve a pulsar el marcador.'); return; }
    const doc = await html(r);
    const have = new Set([...doc.querySelectorAll('#form-artists input[name=id]')].map(i => i.value.toLowerCase()));
    const todo = ids.filter(id => !have.has(id));
    if (todo.length < ids.length) log((ids.length - todo.length) + ' ya los seguías.', '#666');
    let ok = 0, fail = 0;
    for (let i = 0; i < todo.length && !stop; i++) {
      el('s').textContent = 'Siguiendo ' + (i + 1) + ' de ' + todo.length + '…';
      el('b').style.width = (100 * i / todo.length) + '%';
      try {
        const res = await fetch('/artists-add?id=' + todo[i], { credentials: 'same-origin' });
        const msgs = res.ok ? [...(await html(res)).querySelectorAll('.message')] : [];
        const msg = msgs[msgs.length - 1];
        const good = !!msg && msg.classList.contains('success');
        const text = msg ? msg.textContent.trim() : (res.status === 404 ? 'muspy no pudo obtenerlo de MusicBrainz, prueba más tarde' : 'error ' + res.status);
        if (good) { ok++; log(text.replace(/ has been added!$/, '')); }
        else { fail++; log(todo[i] + ': ' + text, '#b91c1c'); }
      } catch (e) {
        fail++;
        log(todo[i] + ': ' + e.message, '#b91c1c');
      }
      await new Promise(res => setTimeout(res, 300));
    }
    el('b').style.width = '100%';
    finish((stop ? 'Detenido. ' : 'Hecho. ') + ok + ' añadidos' + (fail ? ', ' + fail + ' con error' : '') + '.');
  })().catch(e => finish('Error: ' + e.message));
}

$('#bookmarklet').href = 'javascript:(' + encodeURIComponent(muspyBookmarklet.toString()) + ')();void 0';
$('#bookmarklet').addEventListener('click', e => { e.preventDefault(); alert('Arrastra este enlace a tu barra de marcadores; luego púlsalo estando en muspy.com.'); });

// ---------- Eventos ----------

async function handleFiles(files) {
  for (const file of files) {
    const li = document.createElement('li');
    try {
      const rows = parseCSV(await file.text());
      const format = detectFormat(rows);
      if (!format) throw new Error('formato no reconocido');
      const entries = extract(rows, format);
      addEntries(entries, format);
      li.innerHTML = `<b>${esc(file.name)}</b> · ${format === 'discogs' ? 'Discogs' : 'RateYourMusic'} · ${rows.length} discos`;
    } catch (e) {
      li.innerHTML = `<b>${esc(file.name)}</b> · error: ${esc(e.message)}`;
    }
    $('#file-list').appendChild(li);
  }
  for (const a of artists.values()) if (!a.result && cache[a.key]) a.result = cache[a.key];
  const pending = [...artists.values()].filter(a => !a.result).length;
  const li = document.createElement('li');
  li.textContent = `${artists.size} artistas distintos en total` + (pending < artists.size ? ` (${artists.size - pending} ya buscados antes)` : '') + '.';
  $('#file-list').appendChild(li);
  $('#step-resolve').hidden = false;
  $('#btn-resolve').textContent = pending ? `Buscar ${pending} artistas` : 'Todo buscado';
  $('#btn-resolve').disabled = !pending;
  if (pending < artists.size) { $('#step-review').hidden = false; render(); }
}

const drop = $('#drop');
$('#file').addEventListener('change', e => handleFiles(e.target.files));
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('over'); handleFiles(e.dataTransfer.files); });

$('#btn-resolve').addEventListener('click', resolveAll);
$('#btn-stop').addEventListener('click', () => { stopRequested = true; $('#btn-stop').disabled = true; });
$('#btn-clear-cache').addEventListener('click', () => {
  if (!confirm('¿Borrar las búsquedas guardadas en este navegador?')) return;
  cache = {};
  saveCache();
  location.reload();
});

$('.filters').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  filter = b.dataset.filter;
  document.querySelectorAll('.filters button').forEach(x => x.classList.toggle('active', x === b));
  artists.forEach(renderRow);
});
$('#search').addEventListener('input', () => artists.forEach(renderRow));

$('#check-all').addEventListener('change', e => {
  for (const a of artists.values()) {
    if (a.result && a.result.choice >= 0 && visible(a)) { a.result.checked = e.target.checked; storeResult(a); renderRow(a); }
  }
  updateSend();
});

$('#table tbody').addEventListener('change', async e => {
  const tr = e.target.closest('tr');
  const a = tr && artists.get(tr.dataset.key);
  if (!a?.result) return;
  if (e.target.dataset.act === 'check') a.result.checked = e.target.checked;
  if (e.target.dataset.act === 'choice') {
    if (e.target.value === 'manual') await manualChoice(a);
    else {
      a.result.choice = Number(e.target.value);
      a.result.checked = a.result.choice >= 0;
    }
  }
  storeResult(a);
  renderRow(a);
  updateCounts();
});

$('#btn-copy').addEventListener('click', async () => {
  const text = selectedIds().map(id => id.replace(/-/g, '')).join('');
  try { await navigator.clipboard.writeText(text); $('#btn-copy').textContent = 'Copiada'; }
  catch { prompt('Copia este texto:', text); }
});
