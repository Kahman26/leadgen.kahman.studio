'use strict';

const $ = (id) => document.getElementById(id);
const PAGE = 100;

let CFG = { statuses: {}, reasons: {}, hot: 60 };
let offset = 0;
let total = 0;
let statFilter = null;      // быстрый фильтр по клику на карточку статистики
let pollTimer = null;
let selectedId = null;   // какой лид открыт в правой панели
let showAll = false;     // развёрнута ли дополнительная информация в карточке

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) {
    // Сессия кончилась — возвращаем на вход, а не показываем ошибку
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('Требуется вход');
  }
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
  if ($('fHidden').value) p.set('hidden', $('fHidden').value);
  if ($('fOrg').value) p.set('org', $('fOrg').value);
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
    { n: s.in_egrul || 0, t: 'Найдены в ЕГРЮЛ', f: { org: 'active' } },
    { n: s.liquidated || 0, t: 'Ликвидированы', f: { org: 'dead' } },
    { n: s.hidden || 0, t: 'Скрытые', f: { hidden: 'only' } },
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
    if (statFilter) { $('fReason').value = ''; $('fHas').value = '';
                      $('fHidden').value = ''; $('fOrg').value = ''; }
    offset = 0; loadStats(); loadLeads();
  });

  // категории заполняем один раз, из реальных данных
  const sel = $('fCategory');
  if (sel.options.length <= 1) {
    const list = $('catList');
    for (const [cat, n] of Object.entries(s.by_category)) {
      if (cat === '—') continue;
      sel.add(new Option(`${cat} (${n})`, cat));
      list.appendChild(new Option(cat));   // те же категории — в подсказки формы
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

// Как гость может забронировать: сам, через заявку или никак
function bookingText(l) {
  const how = l.booking_engine ? ` — ${esc(l.booking_engine)}` : '';
  if (l.booking_type === 'engine') return `онлайн${how}`;
  if (l.booking_type === 'request') return `только заявка${how}`;
  return 'нет — только телефон';
}

function scoreClass(n) { return n >= CFG.hot ? 'hot' : n >= CFG.hot / 2 ? 'warm' : 'cold'; }

async function loadLeads() {
  const data = await api('/api/leads?' + params({ limit: PAGE, offset }));
  total = data.total;

  $('rows').innerHTML = data.items.map((l) => `
    <tr data-id="${l.id}" class="${l.hidden ? 'is-hidden ' : ''}${String(l.id) === String(selectedId) ? 'selected' : ''}">
      <td>
        <div class="name">${esc(l.name)}</div>
        <div class="sub">${esc(l.category || '')}${l.address ? ' · ' + esc(l.address) : ''}</div>
      </td>
      <td><span class="badge r-${esc(l.reason_code)}">${esc(l.reason_text || '')}</span></td>
      <td class="num"><span class="score ${scoreClass(l.score)}">${l.score}</span></td>
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

  // Наверху только то, что нужно каждый раз; остальное — под кнопкой
  const tech = [
    ['Сайт', site ? `<a href="${esc(site)}" target="_blank" rel="noopener">${esc(site)}</a>` : '—'],
    ['Состояние', { ok: 'работает', dead: 'не открывается', none: 'сайта нет',
                    blocked: 'закрыт защитой' }[l.site_status] || '—'],
  ];
  const techMore = [
    ['Код ответа', l.http_code ?? '—'],
    ['Мобильная версия', l.site_status === 'ok' ? (l.mobile_ready ? 'есть' : 'нет') : '—'],
    ['Бронирование', l.site_status === 'ok' ? bookingText(l) : '—'],
    ['Движок', l.cms || '—'],
    ['Копирайт', l.copyright_year || '—'],
    ['Загрузка', l.load_ms ? l.load_ms + ' мс' : '—'],
    ['Домен до', l.domain_expires || '—'],
  ];

  const boss = l.director
    ? l.director + (l.director_post ? `, ${l.director_post.toLowerCase()}` : '')
    : '';
  const req = [
    ['В реестре', l.org_name],
    ['Статус', l.org_status_text],
    ['Руководитель', boss],
    ['ИНН', l.inn],
    ['ОГРН', l.ogrn],
    ['ОКВЭД', l.okved],
    ['Юр. адрес', l.legal_address],
    ['Регистрация', l.registered_at],
    ['Ликвидирована', l.liquidated_at],
    ['Сотрудников', l.employee_count],
    ['Адрес на карте', l.address],
  ].filter(([, v]) => v);

  const cs = Object.entries(l.contact_source || {});

  $('drawer').innerHTML = `
    <div class="cardhead">
      <div>
        <h2>${esc(l.name)}</h2>
        <div class="muted">${esc(l.category || '')} · балл
          <b class="score ${scoreClass(l.score)}">${l.score}</b> ·
          <span class="badge r-${esc(l.reason_code)}">${esc(l.reason_text || '')}</span>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex:0 0 auto">
        <button id="editLead" title="Исправить данные карточки">Изменить</button>
        <button class="close" onclick="closeLead()">✕</button>
      </div>
    </div>

    ${(l.manual_fields || []).length || l.website_manual ? `
      <div class="wasurl" style="margin-top:10px">
        Исправлено вручную: ${esc([...(l.manual_fields || []),
          ...(l.website_manual ? ['сайт'] : [])].map((f) => CFG.editable[f] || f).join(', '))}.
        Сбор эти поля не перезаписывает.
        <button class="linkbtn" id="unlockLead">Вернуть автозаполнение</button>
      </div>` : ''}

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
      <dl class="kv more" ${showAll ? '' : 'hidden'}>${
        techMore.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      <button class="linkbtn" id="toggleMore">${
        showAll ? 'Свернуть' : 'Смотреть полностью'}</button>

      <div class="siteedit">
        <input type="text" id="siteInput" placeholder="Новый адрес сайта"
               value="${esc(l.website || '')}">
        <button id="saveSite">Сохранить</button>
      </div>
      <div class="muted" style="margin-top:6px;font-size:12px">
        Компания могла сменить домен — впишите рабочий адрес.
      </div>

      ${l.previous_website ? `
        <div class="wasurl">Было:
          <a href="${esc(l.previous_website)}" target="_blank" rel="noopener">${esc(l.previous_website)}</a>
        </div>` : ''}

      <button id="recheck" style="margin-top:10px">Перепроверить сайт</button>
      <span class="muted" id="siteMsg"></span>
    </div>

    ${req.length ? `<div class="section"><h3>Реквизиты по ЕГРЮЛ</h3>
      <dl class="kv">${req.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
      ${l.dadata_confidence === 'medium' ? `<div class="wasurl">
        Совпадение неточное — проверьте, та ли это компания.<br>${esc(l.dadata_match || '')}
      </div>` : ''}
      ${l.dadata_confidence === 'high' && l.dadata_match ? `<div class="muted"
        style="margin-top:6px;font-size:12px">Подтверждено: ${esc(l.dadata_match)}</div>` : ''}
      </div>` : ''}

    <div class="section more" ${showAll ? '' : 'hidden'}>
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
    </div>

    <div class="dangerzone">
      ${l.hidden ? `
        <div class="hiddenbanner">
          Лид скрыт${l.hidden_reason ? `: ${esc(l.hidden_reason)}` : ''}.
          В общем списке он не показывается.
        </div>
        <button id="unhide" style="margin-top:10px">Вернуть в список</button>
      ` : `
        <input type="text" id="hideReason" placeholder="Причина: закрылись, не профиль…"
               style="width:100%;margin-bottom:8px">
        <button id="hide" class="danger">Скрыть из списка</button>
      `}
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

  $('editLead').onclick = () => openEditor(l);

  if ($('unlockLead')) {
    $('unlockLead').onclick = async () => {
      await api(`/api/lead/${id}/unlock`, { method: 'POST' });
      await openLead(id);
      loadLeads();
    };
  }

  $('toggleMore').onclick = () => {
    showAll = !showAll;
    $('drawer').querySelectorAll('.more').forEach((el) => { el.hidden = !showAll; });
    $('toggleMore').textContent = showAll ? 'Свернуть' : 'Смотреть полностью';
  };

  // ── смена адреса сайта и перепроверка ──────────────────────────────────
  const msg = (text) => { $('siteMsg').textContent = ' ' + text; };

  $('saveSite').onclick = async () => {
    const value = $('siteInput').value.trim();
    if (value === (l.website || '')) return msg('адрес не изменился');
    $('saveSite').disabled = true;
    msg('проверяю сайт…');
    try {
      await api(`/api/lead/${id}/website`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ website: value }),
      });
      await openLead(id);          // перерисовываем карточку свежими данными
      loadLeads(); loadStats();
    } catch (e) {
      msg(e.message);
      $('saveSite').disabled = false;
    }
  };

  $('recheck').onclick = async () => {
    $('recheck').disabled = true;
    msg('проверяю…');
    try {
      await api(`/api/lead/${id}/recheck`, { method: 'POST' });
      await openLead(id);
      loadLeads(); loadStats();
    } catch (e) {
      msg(e.message);
      $('recheck').disabled = false;
    }
  };

  // ── скрыть / вернуть ───────────────────────────────────────────────────
  const setHidden = async (hidden, reason) => {
    await api('/api/lead/' + id, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden, hidden_reason: reason || '' }),
    });
    loadLeads(); loadStats();
  };

  if ($('hide')) {
    $('hide').onclick = async () => {
      await setHidden(1, $('hideReason').value.trim());
      closeLead();
    };
  }
  if ($('unhide')) {
    $('unhide').onclick = async () => {
      await setHidden(0, '');
      await openLead(id);
    };
  }

  selectedId = String(id);
  markSelected();
  $('drawer').classList.add('on');        // на узком экране панель выезжает поверх
}

// Порядок полей в форме правки: сначала про объект, потом контакты, потом реестр
const EDIT_GROUPS = [
  ['Объект', ['name', 'category', 'address']],
  ['Контакты', ['phone', 'telegram', 'vk', 'whatsapp', 'email']],
  ['Реквизиты по ЕГРЮЛ', ['org_name', 'inn', 'ogrn', 'director', 'director_post',
                          'okved', 'legal_address']],
];

function openEditor(l) {
  const field = (key) => `
    <label class="fld"><span>${esc(CFG.editable[key] || key)}</span>
      <input type="text" data-edit="${key}" value="${esc(l[key] ?? '')}"
             autocomplete="off"></label>`;

  $('drawer').innerHTML = `
    <div class="cardhead">
      <div><h2>${esc(l.name)}</h2>
      <div class="muted">Правка карточки</div></div>
    </div>
    <div class="err" id="editErr"></div>
    ${EDIT_GROUPS.map(([title, keys]) => `
      <div class="section"><h3>${title}</h3>${keys.map(field).join('')}</div>`).join('')}
    <div class="muted" style="font-size:12px;margin-top:12px">
      Исправленные поля сбор больше не перезаписывает. Пустое значение стирает данные.
    </div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button id="editSave" class="primary">Сохранить</button>
      <button id="editCancel">Отмена</button>
    </div>`;

  $('editCancel').onclick = () => openLead(l.id);

  $('editSave').onclick = async () => {
    const payload = {};
    $('drawer').querySelectorAll('[data-edit]').forEach((el) => {
      payload[el.dataset.edit] = el.value;
    });
    $('editSave').disabled = true;
    $('editSave').textContent = 'Сохраняю…';
    try {
      await api(`/api/lead/${l.id}/edit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      await openLead(l.id);
      loadLeads(); loadStats();
    } catch (e) {
      $('editErr').textContent = e.message;
      $('editErr').classList.add('on');
      $('editSave').disabled = false;
      $('editSave').textContent = 'Сохранить';
    }
  };
}

function markSelected() {
  [...$('rows').children].forEach((tr) => {
    tr.classList.toggle('selected', tr.dataset.id === String(selectedId));
  });
}

const PLACEHOLDER = '<div class="placeholder">Выберите объект в списке слева</div>';

function closeLead() {
  selectedId = null;
  markSelected();
  $('drawer').classList.remove('on');
  $('drawer').innerHTML = PLACEHOLDER;
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
  $('who').textContent = CFG.login || '';
  for (const [k, v] of Object.entries(CFG.reasons)) $('fReason').add(new Option(v, k));
  for (const [k, v] of Object.entries(CFG.statuses)) $('fStatus').add(new Option(v, k));
  $('dadataHint').textContent = CFG.dadata_ready ? '' : '— нужен токен в .env';
  $('optDadata').checked = CFG.dadata_ready;
  $('optDadata').disabled = !CFG.dadata_ready;

  $('drawer').innerHTML = PLACEHOLDER;

  await loadStats();
  await loadLeads();

  const rerun = () => { offset = 0; statFilter = null; loadLeads(); loadStats(); };
  let t;
  $('q').oninput = () => { clearTimeout(t); t = setTimeout(rerun, 300); };
  ['fReason', 'fCategory', 'fStatus', 'fHas', 'fHidden', 'fOrg', 'fSort']
    .forEach((id) => $(id).onchange = rerun);

  $('prev').onclick = () => { offset = Math.max(0, offset - PAGE); loadLeads(); };
  $('next').onclick = () => { offset += PAGE; loadLeads(); };
  document.onkeydown = (e) => { if (e.key === 'Escape') closeLead(); };

  $('btnExport').onclick = () => { location.href = '/api/export.csv?' + params(); };

  $('btnLogout').onclick = async () => {
    await fetch('/logout', { method: 'POST' });
    location.href = '/login';
  };

  $('btnRun').onclick = () => $('runDialog').showModal();
  $('runCancel').onclick = () => $('runDialog').close();
  $('runGo').onclick = async () => {
    $('runDialog').close();
    await api('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        use_osm: $('optOsm').checked, use_dadata: $('optDadata').checked,
        do_whois: $('optWhois').checked, use_cache: $('optCache').checked,
        dadata_discover: $('optDiscover').checked,
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

  // ── добавление объекта руками ────────────────────────────────────────
  const addFields = ['addName', 'addSite', 'addPhone', 'addTg', 'addVk',
                     'addCat', 'addAddr', 'addNote'];

  $('btnAdd').onclick = () => {
    addFields.forEach((id) => { $(id).value = ''; });
    $('addErr').classList.remove('on');
    $('addDialog').showModal();
    $('addName').focus();
  };
  $('addCancel').onclick = () => $('addDialog').close();

  $('addGo').onclick = async () => {
    const name = $('addName').value.trim();
    if (!name) {
      $('addErr').textContent = 'Без названия объект не добавить';
      $('addErr').classList.add('on');
      return;
    }
    $('addErr').classList.remove('on');
    $('addGo').disabled = true;
    $('addGo').textContent = 'Проверяю сайт…';

    try {
      const lead = await api('/api/lead', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          website: $('addSite').value, phone: $('addPhone').value,
          telegram: $('addTg').value, vk: $('addVk').value,
          category: $('addCat').value, address: $('addAddr').value,
          note: $('addNote').value,
        }),
      });
      $('addDialog').close();
      await loadLeads();
      await loadStats();
      openLead(lead.id);                  // сразу показываем, что получилось
    } catch (e) {
      $('addErr').textContent = e.message;
      $('addErr').classList.add('on');
    }
    $('addGo').disabled = false;
    $('addGo').textContent = 'Добавить';
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
