'use strict';

const $ = (id) => document.getElementById(id);
const PAGE = 100;

let CFG = { statuses: {}, reasons: {}, hot: 60 };
let offset = 0;
let total = 0;
let statFilter = null;      // быстрый фильтр по клику на карточку статистики
let pollTimer = null;

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ── фильтры ──────────────────────────────────────────────────────────── */

function params(extra = {}) {
  const p = new URLSearchParams();
  if ($('q').value.trim()) p.set('q', $('q').value.trim());
  if ($('fReason').value) p.set('reason', $('fReason').value);
  if ($('fCategory').value) p.set('category', $('fCategory').value);
  if ($('fStatus').value) p.set('status', $('fStatus').value);
  if ($('fHas').value) p.set('has', $('fHas').value);
  p.set('sort', $('fSort').value);
  if (statFilter) Object.entries(statFilter).forEach(([k, v]) => p.set(k, v));
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p;
}

/* ── статистика ───────────────────────────────────────────────────────── */

async function loadStats() {
  const s = await api('/api/stats');

  const cards = [
    { n: s.total, t: 'Всего в базе', f: null },
    { n: s.hot, t: `Горячих (балл ≥ ${CFG.hot})`, f: null },
    { n: s.with_contact, t: 'С контактами', f: { has: 'contact' } },
  ];
  for (const [code, n] of Object.entries(s.by_reason)) {
    if (code === '—') continue;
    cards.push({ n, t: s.reason_titles[code] || code, f: { reason: code } });
  }

  $('stats').innerHTML = cards.map((c, i) => `
    <div class="stat${JSON.stringify(c.f) === JSON.stringify(statFilter) && c.f ? ' active' : ''}"
         data-i="${i}">
      <div class="n">${c.n}</div><div class="t">${esc(c.t)}</div>
    </div>`).join('');

  [...$('stats').children].forEach((el, i) => el.onclick = () => {
    const f = cards[i].f;
    statFilter = (JSON.stringify(f) === JSON.stringify(statFilter)) ? null : f;
    if (statFilter) { $('fReason').value = ''; $('fHas').value = ''; }
    offset = 0; loadStats(); loadLeads();
  });

  // категории заполняем один раз, из реальных данных
  const sel = $('fCategory');
  if (sel.options.length <= 1) {
    for (const [cat, n] of Object.entries(s.by_category)) {
      if (cat === '—') continue;
      sel.add(new Option(`${cat} (${n})`, cat));
    }
  }
}

/* ── список лидов ─────────────────────────────────────────────────────── */

function contactChips(l) {
  const out = [];
  if (l.phone) out.push(`<a class="chip" href="tel:${esc(l.phone)}">${esc(l.phone)}</a>`);
  if (l.telegram) out.push(`<a class="chip" target="_blank" href="https://t.me/${esc(l.telegram.replace('@', ''))}">TG</a>`);
  if (l.vk) out.push(`<a class="chip" target="_blank" href="https://vk.com/${esc(l.vk)}">VK</a>`);
  if (l.whatsapp) out.push(`<a class="chip" target="_blank" href="https://wa.me/${esc(l.whatsapp.replace('+', ''))}">WA</a>`);
  if (l.email) out.push(`<a class="chip" href="mailto:${esc(l.email)}">@</a>`);
  return out.length ? `<div class="chips">${out.join('')}</div>`
                    : '<span class="chip off">нет контактов</span>';
}

function scoreClass(n) { return n >= CFG.hot ? 'hot' : n >= CFG.hot / 2 ? 'warm' : 'cold'; }

async function loadLeads() {
  const data = await api('/api/leads?' + params({ limit: PAGE, offset }));
  total = data.total;

  $('rows').innerHTML = data.items.map((l) => `
    <tr data-id="${l.id}">
      <td>
        <div class="name">${esc(l.name)}</div>
        <div class="sub">${esc(l.category || '')}${l.address ? ' · ' + esc(l.address) : ''}</div>
      </td>
      <td><span class="badge r-${esc(l.reason_code)}">${esc(l.reason_text || '')}</span></td>
      <td><span class="score ${scoreClass(l.score)}">${l.score}</span></td>
      <td>${contactChips(l)}</td>
      <td>${l.website
            ? `<a class="chip" target="_blank" rel="noopener" href="${esc(l.website)}">сайт ↗</a>`
            : '<span class="chip off">нет</span>'}</td>
      <td><span class="st ${esc(l.status)}">${esc(CFG.statuses[l.status] || l.status)}</span></td>
    </tr>`).join('');

  $('empty').hidden = data.items.length > 0;
  $('count').textContent = `${total} лидов`;
  $('page').textContent = total ? `${offset + 1}–${Math.min(offset + PAGE, total)} из ${total}` : '';
  $('prev').disabled = offset === 0;
  $('next').disabled = offset + PAGE >= total;

  [...$('rows').children].forEach((tr) => tr.onclick = () => openLead(tr.dataset.id));
}

/* ── карточка лида ────────────────────────────────────────────────────── */

async function openLead(id) {
  const l = await api('/api/lead/' + id);
  const site = l.final_url || l.website;

  const tech = [
    ['Сайт', site ? `<a href="${esc(site)}" target="_blank" rel="noopener">${esc(site)}</a>` : '—'],
    ['Состояние', { ok: 'работает', dead: 'не открывается', none: 'сайта нет',
                    blocked: 'закрыт защитой' }[l.site_status] || '—'],
    ['Код ответа', l.http_code ?? '—'],
    ['Мобильная версия', l.site_status === 'ok' ? (l.mobile_ready ? 'есть' : 'нет') : '—'],
    ['Онлайн-бронь', l.site_status === 'ok' ? (l.online_booking ? (l.booking_engine || 'есть') : 'нет') : '—'],
    ['Движок', l.cms || '—'],
    ['Копирайт', l.copyright_year || '—'],
    ['Загрузка', l.load_ms ? l.load_ms + ' мс' : '—'],
    ['Домен до', l.domain_expires || '—'],
  ];

  const req = [
    ['Руководитель', l.director], ['ИНН', l.inn], ['ОКВЭД', l.okved],
    ['Регистрация', l.registered_at], ['Адрес', l.address],
  ].filter(([, v]) => v);

  const cs = Object.entries(l.contact_source || {});

  $('drawer').innerHTML = `
    <button class="close" onclick="closeLead()">Закрыть</button>
    <h2>${esc(l.name)}</h2>
    <div class="muted">${esc(l.category || '')} · балл
      <b class="score ${scoreClass(l.score)}">${l.score}</b> ·
      <span class="badge r-${esc(l.reason_code)}">${esc(l.reason_text || '')}</span>
    </div>

    <div class="section">
      <h3>С чего начать разговор</h3>
      <div class="pitch">${esc(l.pitch || '')}</div>
    </div>

    <div class="section">
      <h3>Контакты</h3>
      ${contactChips(l)}
      ${l.phones && l.phones.length > 1
        ? `<div class="muted" style="margin-top:6px">Ещё номера: ${l.phones.slice(1).map(esc).join(', ')}</div>` : ''}
      ${cs.length ? `<div class="muted" style="margin-top:6px">Откуда контакт:
        ${cs.map(([k, v]) => `${esc(k)} — ${esc(v)}`).join(', ')}</div>` : ''}
    </div>

    <div class="section">
      <h3>Чего не хватает</h3>
      ${(l.missing || []).length
        ? `<ul class="missing">${l.missing.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>`
        : '<div class="muted">Проблем не найдено</div>'}
    </div>

    <div class="section">
      <h3>Сайт и техника</h3>
      <dl class="kv">${tech.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
    </div>

    ${req.length ? `<div class="section"><h3>Реквизиты</h3>
      <dl class="kv">${req.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl></div>` : ''}

    <div class="section">
      <h3>Откуда лид</h3>
      <div>${esc(l.source_detail || l.source)}</div>
      ${l.lat ? `<a class="chip" style="margin-top:6px" target="_blank"
         href="https://www.openstreetmap.org/?mlat=${l.lat}&mlon=${l.lon}#map=18/${l.lat}/${l.lon}">на карте ↗</a>` : ''}
    </div>

    <div class="section">
      <h3>Статус</h3>
      <div class="statusrow">
        ${Object.entries(CFG.statuses).map(([k, v]) =>
          `<button data-st="${k}" class="${l.status === k ? 'on' : ''}">${esc(v)}</button>`).join('')}
      </div>
    </div>

    <div class="section">
      <h3>Заметка</h3>
      <textarea id="noteBox" placeholder="Что сказали, когда перезвонить…">${esc(l.note || '')}</textarea>
      <button id="saveNote" style="margin-top:8px">Сохранить заметку</button>
      <span class="muted" id="noteSaved"></span>
    </div>`;

  $('drawer').querySelectorAll('[data-st]').forEach((b) => b.onclick = async () => {
    await api('/api/lead/' + id, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: b.dataset.st }),
    });
    $('drawer').querySelectorAll('[data-st]').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    loadLeads(); loadStats();
  });

  $('saveNote').onclick = async () => {
    await api('/api/lead/' + id, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: $('noteBox').value }),
    });
    $('noteSaved').textContent = ' сохранено';
    setTimeout(() => ($('noteSaved').textContent = ''), 1500);
  };

  $('drawer').classList.add('on');
  $('overlay').classList.add('on');
}

function closeLead() {
  $('drawer').classList.remove('on');
  $('overlay').classList.remove('on');
}
window.closeLead = closeLead;

/* ── запуск сбора ─────────────────────────────────────────────────────── */

async function pollRun() {
  const s = await api('/api/run/status');
  $('runbox').classList.toggle('on', s.running || !!s.error);
  $('runStage').textContent = s.stage;
  $('runCount').textContent = s.total ? `— проверено ${s.done} из ${s.total}` : '';
  $('runBar').style.width = s.total ? (100 * s.done / s.total) + '%' : '0';
  $('runLog').textContent = s.log.join('\n');
  $('runLog').scrollTop = 1e6;
  $('btnRun').disabled = s.running;

  if (!s.running) {
    clearInterval(pollTimer); pollTimer = null;
    $('btnRun').textContent = 'Запустить сбор';
    loadStats(); loadLeads();
    setTimeout(() => { if (!s.error) $('runbox').classList.remove('on'); }, 8000);
  }
}

function startPolling() {
  $('btnRun').disabled = true;
  $('btnRun').textContent = 'Идёт сбор…';
  $('runbox').classList.add('on');
  if (!pollTimer) pollTimer = setInterval(pollRun, 1500);
  pollRun();
}

/* ── инициализация ────────────────────────────────────────────────────── */

async function init() {
  CFG = await api('/api/config');
  $('cityLabel').textContent = CFG.city + ' · ниша бронирования';
  for (const [k, v] of Object.entries(CFG.reasons)) $('fReason').add(new Option(v, k));
  for (const [k, v] of Object.entries(CFG.statuses)) $('fStatus').add(new Option(v, k));
  $('dadataHint').textContent = CFG.dadata_ready ? '' : '— нужен токен в .env';
  $('optDadata').checked = CFG.dadata_ready;
  $('optDadata').disabled = !CFG.dadata_ready;

  await loadStats();
  await loadLeads();

  const rerun = () => { offset = 0; statFilter = null; loadLeads(); loadStats(); };
  let t;
  $('q').oninput = () => { clearTimeout(t); t = setTimeout(rerun, 300); };
  ['fReason', 'fCategory', 'fStatus', 'fHas', 'fSort'].forEach((id) => $(id).onchange = rerun);

  $('prev').onclick = () => { offset = Math.max(0, offset - PAGE); loadLeads(); };
  $('next').onclick = () => { offset += PAGE; loadLeads(); };
  $('overlay').onclick = closeLead;
  document.onkeydown = (e) => { if (e.key === 'Escape') closeLead(); };

  $('btnExport').onclick = () => { location.href = '/api/export.csv?' + params(); };

  $('btnRun').onclick = () => $('runDialog').showModal();
  $('runCancel').onclick = () => $('runDialog').close();
  $('runGo').onclick = async () => {
    $('runDialog').close();
    await api('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        use_osm: $('optOsm').checked, use_dadata: $('optDadata').checked,
        do_whois: $('optWhois').checked, use_cache: $('optCache').checked,
      }),
    });
    startPolling();
  };

  $('runRescore').onclick = async () => {
    $('runRescore').disabled = true;
    try {
      const r = await api('/api/rescore', { method: 'POST' });
      $('runDialog').close();
      loadStats(); loadLeads();
      alert(`Пересчитано лидов: ${r.updated}`);
    } catch (e) { alert('Не получилось: ' + e.message); }
    $('runRescore').disabled = false;
  };

  $('btnImport').onclick = () => $('fileInput').click();
  $('fileInput').onchange = async () => {
    const f = $('fileInput').files[0];
    if (!f) return;
    $('btnImport').disabled = true;
    $('btnImport').textContent = 'Загружаю…';
    try {
      const fd = new FormData(); fd.append('file', f);
      const r = await api('/api/import', { method: 'POST', body: fd });
      alert(`Добавлено лидов: ${r.added}`);
      loadStats(); loadLeads();
    } catch (e) { alert('Ошибка импорта: ' + e.message); }
    $('btnImport').disabled = false;
    $('btnImport').textContent = 'Импорт CSV';
    $('fileInput').value = '';
  };

  const s = await api('/api/run/status');
  if (s.running) startPolling();
}

init();
