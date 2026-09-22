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
let histOpen = false;    // развёрнута ли история изменений
let NICHES = { niches: [], total: 0, default_id: null };   // справочник ниш
let currentNiche = '';   // открытая вкладка: номер ниши или '' — все ниши
let tabsOpen = false;    // раскрыты ли вкладки, не поместившиеся в строку

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
  if (currentNiche) p.set('niche', currentNiche);
  if ($('fCategory').value) p.set('cat', $('fCategory').value);
  if ($('fStatus').value) p.set('status', $('fStatus').value);
  if ($('fHas').value) p.set('has', $('fHas').value);
  if ($('fHidden').value) p.set('hidden', $('fHidden').value);
  p.set('sort', $('fSort').value);
  if (statFilter) Object.entries(statFilter).forEach(([k, v]) => p.set(k, v));
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p;
}

/* ── статистика ───────────────────────────────────────────────────────── */

async function loadStats() {
  const s = await api('/api/stats' + (currentNiche ? '?niche=' + currentNiche : ''));

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
                      $('fHidden').value = ''; }
    offset = 0; loadStats(); loadLeads();
  });
}

/* ── ниши и категории ─────────────────────────────────────────────────── */

const nicheById = (id) => NICHES.niches.find((n) => String(n.id) === String(id));

// Подпись типа объекта: во вкладке ниши ниша и так понятна, во «Всех» — нет
function kindText(l) {
  const cat = l.category || 'без категории';
  if (currentNiche) return cat;
  const n = nicheById(l.niche_id);
  return n ? `${n.title} · ${cat}` : cat;
}

async function loadNiches() {
  NICHES = await api('/api/niches');
  // Нишу могли удалить или объединить, пока вкладка была открыта
  if (currentNiche && !nicheById(currentNiche)) setNicheInUrl('');
  renderTabs();
  fillCategoryFilter();
}

function renderTabs() {
  const tab = (id, title, n) => `
    <button class="tab${String(id) === String(currentNiche) ? ' on' : ''}" data-niche="${id}">
      ${esc(title)} <span class="tabn">${n}</span></button>`;
  $('nicheTabs').innerHTML =
    tab('', 'Все', NICHES.total) +
    NICHES.niches.map((n) => tab(n.id, n.title, n.count)).join('') +
    '<button class="tab tabadd" id="tabAdd" title="Новая ниша">+</button>' +
    `<button class="tab tabmore" id="tabMore" aria-expanded="false">
       <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
         <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8"
               stroke-linecap="round" stroke-linejoin="round"/></svg></button>`;

  $('nicheTabs').querySelectorAll('[data-niche]').forEach((b) => {
    // Выбрали нишу из раскрытого списка — сворачиваем: выбранная вкладка
    // останется в первой строке, остальные строки больше не нужны
    b.onclick = () => { tabsOpen = false; openNiche(b.dataset.niche); };
  });
  $('tabMore').onclick = () => { tabsOpen = !tabsOpen; fitTabs(); };
  $('tabAdd').onclick = async () => {
    const title = (prompt('Название новой ниши') || '').trim();
    if (!title) return;
    try {
      const r = await api('/api/niches', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      NICHES = r;
      openNiche(r.id);
    } catch (e) { alert('Не получилось: ' + e.message); }
  };

  fitTabs();

  const n = nicheById(currentNiche);
  $('cityLabel').textContent = CFG.city + ' · ' + (n ? n.title : 'все ниши');
  document.title = n ? `${n.title} — сборщик лидов` : 'Сборщик лидов';
}

// Вкладки стоят в одну строку. Что не влезло — прячется, а галочка справа
// раскрывает спрятанное следующими строками. Открытая ниша видна всегда,
// даже если по порядку она попала бы в спрятанные: место под неё
// резервируется первым, остальные идут по порядку, пока хватает ширины.
function fitTabs() {
  const box = $('nicheTabs');
  const more = $('tabMore');
  const add = $('tabAdd');
  if (!more) return;
  const tabs = [...box.querySelectorAll('[data-niche]')];

  box.classList.remove('open');
  tabs.forEach((t) => { t.hidden = false; });
  more.hidden = true;

  const gap = parseFloat(getComputedStyle(box).columnGap) || 0;
  const w = (el) => el.offsetWidth + gap;
  const all = tabs.reduce((s, t) => s + w(t), 0) + w(add);

  let hidden = 0;
  if (all > box.clientWidth) {
    more.hidden = false;
    const active = tabs.find((t) => t.classList.contains('on'));
    let room = box.clientWidth - w(add) - w(more) - (active ? w(active) : 0);
    let fits = true;
    for (const t of tabs) {
      if (t === active) continue;
      if (fits && w(t) <= room) {
        room -= w(t);
      } else {
        fits = false;          // дальше не берём даже узкие: порядок важнее
        t.hidden = true;
        hidden++;
      }
    }
  }

  if (!hidden) tabsOpen = false;
  if (tabsOpen) {
    tabs.forEach((t) => { t.hidden = false; });
    box.classList.add('open');
  }
  more.setAttribute('aria-expanded', String(tabsOpen));
  more.title = tabsOpen ? 'Свернуть' : `Ещё ниш: ${hidden}`;
}

// Фильтр типов показывает категории только открытой ниши. Во «Всех» —
// все, сгруппированные по нишам, иначе одноимённые не различить.
function fillCategoryFilter() {
  const sel = $('fCategory');
  const keep = sel.value;
  sel.innerHTML = '<option value="">Все типы</option>';
  const add = (parent, n) => {
    for (const c of n.categories) {
      parent.appendChild(new Option(`${c.title} (${c.count})`, c.id));
    }
  };
  const n = nicheById(currentNiche);
  if (n) {
    add(sel, n);
    if (n.no_category) sel.add(new Option(`Без категории (${n.no_category})`, 'none'));
  } else {
    for (const x of NICHES.niches) {
      if (!x.categories.length) continue;
      const g = document.createElement('optgroup');
      g.label = x.title;
      add(g, x);
      sel.appendChild(g);
    }
  }
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : '';
}

function setNicheInUrl(id, push) {
  currentNiche = id ? String(id) : '';
  const u = new URL(location.href);
  if (currentNiche) u.searchParams.set('niche', currentNiche);
  else u.searchParams.delete('niche');
  u.searchParams.delete('lead');
  history[push ? 'pushState' : 'replaceState'](null, '', u);
  try {
    localStorage.setItem('niche', currentNiche);
  } catch (e) { /* приватный режим — просто не запомним */ }
}

function openNiche(id, push = true) {
  if (String(id || '') !== currentNiche) setNicheInUrl(id, push);
  $('fCategory').value = '';
  offset = 0; statFilter = null;
  renderTabs(); fillCategoryFilter();
  loadStats(); loadLeads();
}

// Ниша и категория в форме: два списка из справочника. Последний пункт
// каждого — «+ Новая…»: под списком появляется поле, и новая ниша или
// категория создаётся на сервере вместе с объектом.
const NEW = '__new';

function pickerHTML(p, nicheId, catId) {
  return `
    <div class="fldrow">
      <label class="fld"><span>Ниша</span>
        <select id="${p}Niche">
          ${NICHES.niches.map((n) => `<option value="${n.id}"${
            String(n.id) === String(nicheId) ? ' selected' : ''}>${esc(n.title)}</option>`).join('')}
          <option value="${NEW}">+ Новая ниша…</option>
        </select>
        <input type="text" id="${p}NicheNew" class="newname" placeholder="Название ниши"
               autocomplete="off" hidden></label>
      <label class="fld"><span>Категория</span>
        <select id="${p}Cat" data-want="${esc(catId || '')}"></select>
        <input type="text" id="${p}CatNew" class="newname" placeholder="Название категории"
               autocomplete="off" hidden></label>
    </div>`;
}

function wirePicker(p) {
  const showNew = (what) => {
    const on = $(p + what).value === NEW;
    $(p + what + 'New').hidden = !on;
    if (on) $(p + what + 'New').focus();
  };
  const fillCats = () => {
    const sel = $(p + 'Cat');
    const want = sel.dataset.want || sel.value;
    const n = nicheById($(p + 'Niche').value);
    sel.innerHTML = '<option value="">Без категории</option>' +
      (n ? n.categories.map((c) => `<option value="${c.id}">${esc(c.title)}</option>`).join('') : '') +
      `<option value="${NEW}">+ Новая категория…</option>`;
    sel.value = [...sel.options].some((o) => o.value === String(want)) ? String(want) : '';
    sel.dataset.want = '';
    showNew('Cat');
  };
  $(p + 'Niche').onchange = () => { showNew('Niche'); fillCats(); };
  $(p + 'Cat').onchange = () => showNew('Cat');
  fillCats();
}

// Рубрика с карт («Автосервис, автотехцентр») → наша категория. Сравниваем
// основы слов: «Автосервис, СТО» совпадёт с «Автосервис», «Баня / сауна» —
// с «Сауна». Ищем во всех нишах, при равенстве выигрывает открытая вкладка.
// Не угадали — категория остаётся пустой, человек выберет сам.
function guessCategory(rubric) {
  const text = String(rubric || '').toLowerCase().replace(/ё/g, 'е');
  if (!text) return null;
  const stems = (title) => title.toLowerCase().replace(/ё/g, 'е')
    .split(/[^a-zа-я0-9]+/).filter((w) => w.length >= 4)
    .map((w) => w.slice(0, Math.max(4, w.length - 2)));
  let best = null;
  for (const n of NICHES.niches) {
    for (const c of n.categories) {
      const score = stems(c.title).filter((st) => text.includes(st)).length
        + (String(n.id) === String(currentNiche) ? 0.5 : 0);
      if (score >= 1 && (!best || score > best.score)) best = { niche: n.id, cat: c.id, score };
    }
  }
  return best;
}

// То, что уходит на сервер: номер выбранного или название нового
function pickerPayload(p) {
  const out = {};
  const niche = $(p + 'Niche').value;
  const cat = $(p + 'Cat').value;
  if (niche === NEW) {
    const t = $(p + 'NicheNew').value.trim();
    if (!t) throw new Error('Впишите название новой ниши');
    out.niche_new = t;
  } else {
    out.niche_id = Number(niche);
  }
  if (cat === NEW) {
    const t = $(p + 'CatNew').value.trim();
    if (!t) throw new Error('Впишите название новой категории');
    out.category_new = t;
  } else {
    out.category_id = cat ? Number(cat) : null;
  }
  return out;
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
        <div class="sub">${esc(kindText(l))}</div>
      </td>
      <td><span class="badge r-${esc(l.reason_code)}">${esc(l.reason_text || '')}</span></td>
      <td class="num">${l.priority
            ? `<span class="prio">${l.priority}</span>`
            : '<span class="muted">—</span>'}</td>
      <td class="num"><span class="score ${scoreClass(l.score)}">${l.score}</span></td>
      <td>${contactChips(l)}</td>
      <td>${l.website
            ? `<a class="chip" target="_blank" rel="noopener" href="${esc(l.website)}">сайт ↗</a>`
            : '<span class="chip off">нет</span>'}</td>
      <td><span class="st ${esc(l.status)}">${esc(CFG.statuses[l.status] || l.status)}${
            l.status === 'no_answer' && l.call_count
              ? ` <b class="tries${l.call_count >= 3 ? ' warn' : ''}">(${l.call_count})</b>`
              : ''}</span></td>
    </tr>`).join('');

  $('empty').hidden = data.items.length > 0;
  // Сбор умеет только нишу по умолчанию — в остальные объекты добавляют руками
  const n = nicheById(currentNiche);
  $('empty').textContent = n && n.id !== NICHES.default_id && !n.count
    ? `В нише «${n.title}» пока пусто. Добавьте объект кнопкой «+ Объект» или загрузите CSV.`
    : 'Пока пусто. Нажмите «Запустить сбор».';
  $('count').textContent = `${total} лидов`;
  $('page').textContent = total ? `${offset + 1}–${Math.min(offset + PAGE, total)} из ${total}` : '';
  $('prev').disabled = offset === 0;
  $('next').disabled = offset + PAGE >= total;

  [...$('rows').children].forEach((tr) => tr.onclick = () => openLead(tr.dataset.id));
}

/* ── ширина карточки ──────────────────────────────────────────────────── */

// На широком мониторе хочется больше карточки, на ноутбуке — больше таблицы.
// Это дело вкуса, поэтому ширину задаёт человек, а не вёрстка, и она
// запоминается до следующего раза.
const SIDE_MIN = 320;

// По умолчанию делим экран пополам: карточку читают не реже списка, и на
// широком мониторе половина ей не жалко. Считаем от рабочей области, а не
// от окна: у неё свои поля.
function defaultSideWidth() {
  const ws = document.querySelector('.workspace');
  return Math.round((ws ? ws.getBoundingClientRect().width : window.innerWidth) / 2);
}

function setSideWidth(px) {
  // Верхняя граница — доля экрана, а не число: на 4K потолок в 800 пикселей
  // был бы бессмысленным, а на 1366 — недостижимым.
  const max = Math.max(SIDE_MIN, Math.round(window.innerWidth * 0.72));
  const w = Math.round(Math.max(SIDE_MIN, Math.min(px, max)));
  $('drawer').style.flexBasis = w + 'px';
  try {
    localStorage.setItem('sideWidth', w);
  } catch (e) {
    // Приватный режим запрещает запись — ширина просто не переживёт перезагрузку
  }
}

function wireSplitter() {
  const sp = $('splitter');
  if (!sp) return;

  sp.onpointerdown = (e) => {
    e.preventDefault();
    // Границу воркспейса берём один раз: во время перетаскивания она
    // не меняется, а getBoundingClientRect на каждое движение — лишняя работа.
    const right = document.querySelector('.workspace').getBoundingClientRect().right;
    sp.setPointerCapture(e.pointerId);
    document.body.classList.add('resizing');

    const move = (ev) => setSideWidth(right - ev.clientX);
    const up = () => {
      document.body.classList.remove('resizing');
      sp.removeEventListener('pointermove', move);
      sp.removeEventListener('pointerup', up);
      sp.removeEventListener('pointercancel', up);
    };
    sp.addEventListener('pointermove', move);
    sp.addEventListener('pointerup', up);
    sp.addEventListener('pointercancel', up);
  };

  // Поймать ровную половину ползунком трудно, а вернуться к ней хочется.
  sp.ondblclick = () => setSideWidth(defaultSideWidth());

  let saved = 0;
  try {
    saved = Number(localStorage.getItem('sideWidth')) || 0;
  } catch (e) { /* читать тоже может быть нельзя */ }
  setSideWidth(saved || defaultSideWidth());
}

/* ── попытки дозвона ──────────────────────────────────────────────────── */

// У «Не дозвонились» блок развёрнут и с кнопкой: продажник вернётся к лиду
// ещё не раз. В остальных статусах — одна строка: когда лид уже перешёл в
// «Связались», важно видеть, сколько он стоил усилий.
function callBlock(l) {
  const n = l.call_count || 0;
  if (l.status === 'no_answer') {
    return `
      <div class="calls">
        <div><b>Попыток дозвона: ${n}</b></div>
        ${l.last_call_text ? `<div class="muted">Последняя: ${esc(l.last_call_text)}</div>` : ''}
        <button id="callAgain">Снова не ответили</button>
      </div>`;
  }
  if (!n) return '';
  return `<div class="calls muted">Звонков: ${n}${
    l.last_call_short ? ', последний ' + esc(l.last_call_short) : ''}</div>`;
}

// Перерисовываем только сам блок: статус не поменялся, дёргать список
// и карточку целиком незачем — иначе панель моргает под курсором.
function bindCall(id, l) {
  const btn = $('callAgain');
  if (!btn) return;
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      Object.assign(l, await api('/api/lead/' + id + '/call', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ outcome: 'no_answer' }),
      }));
    } catch (e) {
      btn.disabled = false;
      alert('Не получилось записать звонок: ' + e.message);
      return;
    }
    $('callBox').innerHTML = callBlock(l);
    bindCall(id, l);
    loadLeadHistory(id);        // попытка уже в журнале — покажем её сразу
  };
}

/* ── сделки и платежи ─────────────────────────────────────────────────── */

// Весь блок — только для владельца базы. Продажник вместо него видит одну
// строку без сумм: своё вознаграждение он не должен считать по карточке.

const rub = (n) => (Number(n) || 0).toLocaleString('ru-RU') + '\u00a0₽';

// Сегодняшний день строкой для поля даты. toISOString() дал бы UTC и в
// Екатеринбурге до пяти утра подставлял вчерашнее число.
function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function sellerOptions(sellers, chosen) {
  return sellers.map((x) =>
    `<option value="${esc(x)}"${x === chosen ? ' selected' : ''}>${esc(x)}</option>`).join('');
}

function dealForm(sellers, id) {
  return `
    <div class="dealform" id="${id}">
      <div class="drow">
        <select data-f="kind">
          <option value="project">Проект</option>
          <option value="retainer">Абонентка</option>
        </select>
        <input type="number" data-f="amount" placeholder="Сумма, ₽" min="1" step="1">
      </div>
      <div class="drow">
        <input type="date" data-f="contract_at" value="${todayISO()}">
        <select data-f="owner_login">${sellerOptions(sellers, CFG.login)}</select>
      </div>
      <label class="dcheck"><input type="checkbox" data-f="is_repeat"> повторная сделка</label>
    </div>`;
}

function paymentRows(d) {
  if (!d.payments.length) return '<div class="muted dpay">Платежей пока нет</div>';
  return d.payments.map((p) => `
    <div class="dpay" data-pay="${p.id}">
      <span class="dpdate">${esc(p.paid_text)}</span>
      <span class="dpsum">${esc(rub(p.amount))}</span>
      <span class="muted">${esc(p.kind_text)}</span>
      <button class="ndel" title="Удалить платёж">✕</button>
    </div>`).join('');
}

// У абонентки amount — месячный платёж, а не план по договору: «поступило
// столько-то из месячного» читалось бы как ошибка. Поэтому у неё ни полосы
// выполнения, ни доли первой оплаты — только сумма и число месяцев.
function dealCard(d) {
  const project = d.kind === 'project';
  const target = project ? rub(d.amount) : rub(d.amount) + ' / мес';
  const totals = project
    ? `Поступило ${esc(rub(d.paid))} из ${esc(rub(d.amount))}${
        d.left ? ', остаток ' + esc(rub(d.left)) : ''}${
        d.payments.length ? ` · первая оплата ${d.first_share}%` : ''}`
    : `Поступило ${esc(rub(d.paid))}${
        d.months ? ` за ${d.months} мес.` : ''}`;
  return `
    <div class="deal" data-deal="${d.id}">
      <div class="dhead">
        <b>${esc(d.kind_text)}</b>
        <span>${esc(target)}</span>
        <span class="muted">${esc(d.contract_text)}</span>
        <span class="muted">${esc(d.owner_login)}</span>
        ${d.is_repeat ? '<span class="dtag">повторная</span>' : ''}
        ${d.payments.length ? '' : '<button class="ndel ddel" title="Удалить сделку">✕</button>'}
      </div>
      ${project ? `<div class="dbar"><i style="width:${d.progress}%"></i></div>` : ''}
      <div class="dsum muted">${totals}</div>
      ${d.low_first ? `<div class="dwarn">меньше ${CFG.bonus_min_share || 30}\u00a0% — не идёт в зачёт премии продажнику</div>` : ''}
      <div class="dpays">${paymentRows(d)}</div>
      <button class="dpayadd">+ оплата</button>
      <div class="payform" hidden>
        <div class="drow">
          <input type="number" data-f="amount" placeholder="Сумма, ₽" min="1" step="1">
          <input type="date" data-f="paid_at" value="${todayISO()}">
        </div>
        <div class="drow">
          <select data-f="kind">
            <option value="first">Первая</option>
            <option value="monthly">Месячный</option>
            <option value="final">Финальная</option>
            <option value="other">Прочее</option>
          </select>
          <button class="primary paysave">Внести</button>
        </div>
      </div>
    </div>`;
}

function readForm(box) {
  const out = {};
  box.querySelectorAll('[data-f]').forEach((el) => {
    out[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value;
  });
  return out;
}

async function loadDeals(id) {
  const box = $('dealBox');
  if (!box) return;
  let data;
  try {
    data = await api(`/api/lead/${id}/deals`);
  } catch (e) {
    box.innerHTML = `<span class="muted">не удалось загрузить: ${esc(e.message)}</span>`;
    return;
  }
  CFG.bonus_min_share = data.bonus_min_share;

  box.innerHTML = data.items.map(dealCard).join('')
    + `<button id="newDeal">Завести сделку</button>
       <div id="newDealForm" hidden>
         ${dealForm(data.sellers, 'newDealFields')}
         <button class="primary" id="saveDeal">Сохранить сделку</button>
       </div>`;

  $('newDeal').onclick = () => {
    $('newDeal').hidden = true;
    $('newDealForm').hidden = false;
  };
  $('saveDeal').onclick = async () => {
    $('saveDeal').disabled = true;
    try {
      await api(`/api/lead/${id}/deals`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(readForm($('newDealFields'))),
      });
    } catch (e) {
      alert('Не получилось завести сделку: ' + e.message);
      $('saveDeal').disabled = false;
      return;
    }
    loadDeals(id); loadLeadHistory(id);
  };

  box.querySelectorAll('.deal').forEach((el) => {
    const dealId = el.dataset.deal;

    el.querySelector('.dpayadd').onclick = () => {
      const f = el.querySelector('.payform');
      f.hidden = !f.hidden;
    };

    el.querySelector('.paysave').onclick = async () => {
      const btn = el.querySelector('.paysave');
      btn.disabled = true;
      try {
        await api(`/api/deal/${dealId}/payments`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(readForm(el.querySelector('.payform'))),
        });
      } catch (e) {
        alert('Не получилось внести платёж: ' + e.message);
        btn.disabled = false;
        return;
      }
      // Первый платёж переводит лид в «Сделку» — карточку и список надо
      // перечитать целиком, статус там уже другой.
      await openLead(id);
      loadLeads(); loadStats();
    };

    const del = el.querySelector('.ddel');
    if (del) del.onclick = async () => {
      if (!confirm('Удалить сделку?')) return;
      try {
        await api('/api/deal/' + dealId, { method: 'DELETE' });
      } catch (e) {
        alert('Не получилось удалить: ' + e.message);
        return;
      }
      loadDeals(id); loadLeadHistory(id);
    };

    el.querySelectorAll('[data-pay] .ndel').forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm('Удалить платёж? Статус лида при этом не изменится.')) return;
        try {
          await api('/api/payment/' + btn.closest('[data-pay]').dataset.pay,
                    { method: 'DELETE' });
        } catch (e) {
          alert('Не получилось удалить: ' + e.message);
          return;
        }
        loadDeals(id); loadLeadHistory(id);
      };
    });
  });
}

/* ── заметки ──────────────────────────────────────────────────────────── */

// Тон автора считаем из логина: один и тот же человек всегда одного цвета,
// а новый сотрудник получает свой сам, без правки кода. В CSS уходит только
// тон — насыщенность и светлота там свои для светлой и тёмной темы.
function authorHue(login) {
  let sum = 0;
  for (const ch of String(login || '')) sum += ch.codePointAt(0);
  return sum % 360;
}

function noteRow(n) {
  // Автора перенесённой заметки в журнале не было — выдумывать его не станем.
  const who = n.system ? 'перенесено из старой карточки' : n.login;
  const canDelete = !n.system && (n.login === CFG.login || CFG.is_admin);
  return `
    <div class="note" data-note="${n.id}" style="--hue:${authorHue(n.login)}">
      <div class="nhead">
        <span class="nwho">${esc(who)}</span>
        <span class="nwhen">${esc(n.when)}</span>
        ${canDelete ? '<button class="ndel" title="Удалить заметку">✕</button>' : ''}
      </div>
      <div class="ntext">${esc(n.text)}</div>
    </div>`;
}

async function loadNotes(id) {
  const box = $('noteFeed');
  if (!box) return;
  let data;
  try {
    data = await api(`/api/lead/${id}/notes`);
  } catch (e) {
    box.innerHTML = `<span class="muted">не удалось загрузить: ${esc(e.message)}</span>`;
    return;
  }
  box.innerHTML = data.items.length
    ? data.items.map(noteRow).join('')
    : '<span class="muted">Заметок пока нет</span>';

  box.querySelectorAll('[data-note] .ndel').forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm('Удалить заметку?')) return;
      btn.disabled = true;
      try {
        await api('/api/note/' + btn.closest('[data-note]').dataset.note,
                  { method: 'DELETE' });
      } catch (e) {
        btn.disabled = false;
        alert('Не получилось удалить: ' + e.message);
        return;
      }
      loadNotes(id); loadLeadHistory(id);
    };
  });
}

/* ── карточка лида ────────────────────────────────────────────────────── */

// В карточке показываем последние правки. Полная лента по всем объектам —
// на отдельной странице, сюда её тащить незачем.
const HISTORY_IN_CARD = 6;

// Изменения от сбора в карточке по умолчанию не показываем: одна
// перепроверка сайта даёт две строки и вытесняет то, что делал человек.
let histSystem = false;

async function loadLeadHistory(id, all) {
  const box = $('leadHistory');
  if (!box || !histOpen) return;
  let data;
  try {
    data = await api(`/api/lead/${id}/history?system=${histSystem ? 1 : 0}`);
  } catch (e) {
    box.innerHTML = `<span class="muted">не удалось загрузить: ${esc(e.message)}</span>`;
    return;
  }

  const toggle = `<button class="linkbtn" id="histSys">${
    histSystem ? 'без изменений от сбора' : 'показать изменения от сбора'}</button>`;

  if (!data.rows.length) {
    box.innerHTML = '<span class="muted">Изменений пока не было</span> ' + toggle;
  } else {
    const rows = all ? data.rows : data.rows.slice(0, HISTORY_IN_CARD);
    const rest = data.rows.length - rows.length;
    box.innerHTML = rows.map((e) =>
        HistView.row(e, { canUndo: data.can_undo })).join('')
      + `<div class="histfoot">${
          rest ? `<button class="linkbtn" id="histMore">Ещё ${rest}</button>` : ''
        }${toggle}</div>`;
  }

  HistView.wireUndo(box, api, async () => {
    await openLead(id);            // значения в карточке тоже изменились
    loadLeads();
  });
  if ($('histMore')) $('histMore').onclick = () => loadLeadHistory(id, true);
  $('histSys').onclick = () => {
    histSystem = !histSystem;
    loadLeadHistory(id, all);
  };
}

async function openLead(id) {
  const l = await api('/api/lead/' + id);
  const site = l.final_url || l.website;

  // Наверху только то, что нужно каждый раз; остальное — под кнопкой
  const tech = [
    ['Сайт', site ? `<a href="${esc(site)}" target="_blank" rel="noopener">${esc(site)}</a>` : '—'],
    ['Состояние', l.site_status === 'parked'
        ? `сайта компании нет${l.parked_reason ? ': ' + esc(l.parked_reason) : ''}`
        : ({ ok: 'работает', dead: 'не открывается', none: 'сайта нет',
             blocked: 'закрыт защитой' }[l.site_status] || '—')],
    ['Бронирование', l.site_status === 'ok' ? bookingText(l) : '—'],
    ['Движок', l.cms || '—'],
  ];
  const techMore = [
    ['Код ответа', l.http_code ?? '—'],
    ['Мобильная версия', l.site_status === 'ok' ? (l.mobile_ready ? 'есть' : 'нет') : '—'],
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
      <h2>${esc(l.name)}</h2>
      <button class="close" onclick="closeLead()" title="Закрыть">✕</button>
    </div>
    <div class="cardmeta">${esc((nicheById(l.niche_id) || {}).title || '')} ·
      ${esc(l.category || 'без категории')} · балл
      <b class="score ${scoreClass(l.score)}">${l.score}</b> ·
      <span class="badge r-${esc(l.reason_code)}">${esc(l.reason_text || '')}</span>
    </div>
    <div class="cardactions">
      <button id="aiLead" title="Проверить объект через чат с Claude">Проверка</button>
      <button id="editLead" title="Исправить данные карточки">Изменить</button>
    </div>

    ${(l.manual_fields || []).length || l.website_manual ? `
      <div class="wasurl" style="margin-top:10px">
        Исправлено вручную: ${esc([...(l.manual_fields || []),
          ...(l.website_manual ? ['сайт'] : [])].map((f) => CFG.editable[f] || f).join(', '))}.
        Сбор эти поля не перезаписывает.
        <button class="linkbtn" id="unlockLead">Вернуть автозаполнение</button>
      </div>` : ''}

    <div class="section">
      <h3>Мой приоритет <span class="muted" style="text-transform:none">— кому звонить раньше</span></h3>
      <div class="prios">
        ${[1,2,3,4,5,6,7,8,9,10].map((n) => `<button data-prio="${n}"
           class="${l.priority === n ? 'on' : ''}">${n}</button>`).join('')}
        <button data-prio="" class="prio-clear" title="Снять оценку">—</button>
      </div>
    </div>

    <div class="section">
      <h3>С чего начать разговор</h3>
      <div class="pitch">${esc(l.pitch || '')}</div>
    </div>

    <div class="section">
      <h3>Контакты</h3>
      ${contactChips(l)}
      ${l.map_url ? `<a class="chip" style="margin-top:6px" target="_blank" rel="noopener"
         href="${esc(l.map_url)}">${/2gis\./.test(l.map_url) ? '2ГИС' : 'Яндекс Карты'} ↗</a>` : ''}
      ${l.phones && l.phones.length > 1
        ? `<div class="muted" style="margin-top:6px">Ещё номера: ${l.phones.slice(1).map(esc).join(', ')}</div>` : ''}
      ${cs.length ? `<div class="muted" style="margin-top:6px">Откуда контакт:
        ${cs.map(([k, v]) => `${esc(k)} — ${esc(v)}`).join(', ')}</div>` : ''}
    </div>

    ${l.ai_checked_at || l.ai_error ? `
    <div class="section">
      <h3>Что нашёл Claude ${l.ai_checked_at
          ? `<span class="muted" style="text-transform:none">— ${esc(l.ai_checked_at.slice(0, 10))}</span>` : ''}</h3>
      ${l.ai_error
        ? `<div class="wasurl">Не получилось: ${esc(l.ai_error)}</div>`
        : `
        ${l.ai_summary ? `<div class="pitch">${esc(l.ai_summary)}</div>` : ''}
        ${(l.ai_problems || []).length ? `<ul class="missing" style="margin-top:8px">${
          l.ai_problems.map((p) => `<li>${esc(p)}</li>`).join('')}</ul>` : ''}
        ${l.ai_director ? `<div class="muted" style="margin-top:8px">Руководитель по поиску:
          ${esc(l.ai_director)}</div>` : ''}
        ${l.ai_found_site ? `<div class="muted" style="margin-top:8px">Найденный сайт:
          <a href="${esc(l.ai_found_site)}" target="_blank" rel="noopener">${esc(l.ai_found_site)}</a></div>` : ''}
        ${(l.ai_aggregators || []).length ? `<div class="muted" style="margin-top:6px">Брони идут через:
          ${l.ai_aggregators.map((a) => a && a.url
            ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.name || a.url)}</a>`
            : esc((a && a.name) || '')).join(', ')}</div>` : ''}
        ${(l.ai_sources || []).length ? `<div class="muted" style="margin-top:6px;font-size:12px">
          Источники: ${l.ai_sources.map((u) =>
            `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(String(u).replace(/^https?:\/\//, '').slice(0, 28))}</a>`
          ).join(', ')}</div>` : ''}
        <div class="muted" style="margin-top:8px;font-size:12px">
          Это находки поиска, а не проверенные данные — сверьтесь по ссылкам.
        </div>`}
    </div>` : ''}

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
      <div id="callBox">${callBlock(l)}</div>
    </div>

    ${CFG.is_admin ? `
    <div class="section">
      <h3>Сделка</h3>
      <div id="dealBox" class="muted">загружаем…</div>
    </div>` : (l.deal_closed ? `
    <div class="section">
      <h3>Сделка</h3>
      <div class="dclosed">Сделка закрыта ${esc(l.deal_closed)}</div>
    </div>` : '')}

    <div class="section">
      <h3>Заметки</h3>
      <textarea id="noteBox" placeholder="Что сказали, когда перезвонить…"></textarea>
      <button id="addNoteBtn" style="margin-top:8px">Добавить заметку</button>
      <div id="noteFeed" class="muted" style="margin-top:12px">загружаем…</div>
    </div>

    <div class="section">
      <h3>История изменений
        <button class="linkbtn" id="toggleHist">${
          histOpen ? 'Свернуть' : 'Смотреть полностью'}</button></h3>
      <div id="leadHistory" class="muted" ${histOpen ? '' : 'hidden'}>загружаем…</div>
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
    // «Не дозвонились» и «Связались» сами пишут попытку — счётчик надо
    // перечитать, иначе блок покажет вчерашние цифры.
    Object.assign(l, await api('/api/lead/' + id));
    $('callBox').innerHTML = callBlock(l);
    bindCall(id, l);
    loadLeads(); loadStats(); loadLeadHistory(id);
  });

  bindCall(id, l);

  $('addNoteBtn').onclick = async () => {
    const text = $('noteBox').value.trim();
    if (!text) return;
    $('addNoteBtn').disabled = true;
    try {
      await api('/api/lead/' + id + '/notes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
    } catch (e) {
      alert('Не получилось сохранить: ' + e.message);
      $('addNoteBtn').disabled = false;
      return;
    }
    $('noteBox').value = '';
    $('addNoteBtn').disabled = false;
    loadNotes(id); loadLeadHistory(id);
  };

  $('drawer').querySelectorAll('[data-prio]').forEach((b) => b.onclick = async () => {
    const value = b.dataset.prio === '' ? null : Number(b.dataset.prio);
    await api('/api/lead/' + id, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ priority: value }),
    });
    $('drawer').querySelectorAll('[data-prio]').forEach((x) => x.classList.remove('on'));
    if (value) b.classList.add('on');
    loadLeads();
  });

  loadNotes(id);
  if (CFG.is_admin) loadDeals(id);
  loadLeadHistory(id);

  $('aiLead').onclick = () => openResearch(l);

  $('editLead').onclick = () => openEditor(l);

  if ($('unlockLead')) {
    $('unlockLead').onclick = async () => {
      await api(`/api/lead/${id}/unlock`, { method: 'POST' });
      await openLead(id);
      loadLeads();
    };
  }

  $('toggleHist').onclick = () => {
    histOpen = !histOpen;
    $('leadHistory').hidden = !histOpen;
    $('toggleHist').textContent = histOpen ? 'Свернуть' : 'Смотреть полностью';
    if (histOpen) loadLeadHistory(id);
  };

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
  ['Объект', ['name', 'address']],
  ['Контакты', ['phone', 'telegram', 'vk', 'whatsapp', 'email']],
  ['Реквизиты по ЕГРЮЛ', ['org_name', 'inn', 'ogrn', 'director', 'director_post',
                          'okved', 'legal_address']],
];

function openResearch(l) {
  $('drawer').innerHTML = `
    <div class="cardhead">
      <h2>${esc(l.name)}</h2>
      <button class="close" id="resBack">Назад</button>
    </div>
    <div class="cardmeta">Проверка через чат</div>

    <div class="section">
      <h3>Шаг 1 — запрос</h3>
      <button id="resCopy" class="primary">Скопировать запрос</button>
      <div class="muted" style="margin-top:6px;font-size:12px">
        Вставьте его в чат с Claude, где включён поиск в интернете.
      </div>
      <textarea id="resBrief" style="min-height:120px;margin-top:8px" readonly></textarea>
    </div>

    <div class="section">
      <h3>Шаг 2 — ответ</h3>
      <textarea id="resAnswer" style="min-height:150px"
                placeholder="Вставьте сюда JSON из чата"></textarea>
      <div class="err" id="resErr"></div>
      <button id="resSave" class="primary" style="margin-top:8px">Сохранить в карточку</button>
      <div class="muted" style="margin-top:6px;font-size:12px">
        Контакты лягут только в пустые поля, статус станет «Автопроверка».
      </div>
    </div>`;

  $('resBack').onclick = () => openLead(l.id);

  api(`/api/lead/${l.id}/brief`)
    .then((d) => { $('resBrief').value = d.text; })
    .catch((e) => { $('resBrief').value = 'Не удалось получить запрос: ' + e.message; });

  $('resCopy').onclick = async () => {
    const text = $('resBrief').value;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // В некоторых браузерах буфер недоступен без жеста — выделяем руками
      $('resBrief').select();
    }
    $('resCopy').textContent = 'Скопировано';
    setTimeout(() => { $('resCopy').textContent = 'Скопировать запрос'; }, 1500);
  };

  $('resSave').onclick = async () => {
    $('resErr').classList.remove('on');
    $('resSave').disabled = true;
    $('resSave').textContent = 'Сохраняю…';
    try {
      await api(`/api/lead/${l.id}/findings`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: $('resAnswer').value }),
      });
      await openLead(l.id);
      loadLeads(); loadStats();
    } catch (e) {
      $('resErr').textContent = e.message;
      $('resErr').classList.add('on');
      $('resSave').disabled = false;
      $('resSave').textContent = 'Сохранить в карточку';
    }
  };
}

function openEditor(l) {
  const field = (key) => `
    <label class="fld"><span>${esc(CFG.editable[key] || key)}</span>
      <input type="text" data-edit="${key}" value="${esc(l[key] ?? '')}"
             autocomplete="off"></label>`;

  $('drawer').innerHTML = `
    <div class="cardhead">
      <h2>${esc(l.name)}</h2>
    </div>
    <div class="cardmeta">Правка карточки</div>
    <div class="err" id="editErr"></div>
    ${EDIT_GROUPS.map(([title, keys], i) => `
      <div class="section"><h3>${title}</h3>${keys.map(field).join('')}${
        i === 0 ? pickerHTML('edit', l.niche_id, l.category_id) : ''}</div>`).join('')}
    <div class="muted" style="font-size:12px;margin-top:12px">
      Исправленные поля сбор больше не перезаписывает. Пустое значение стирает данные.
    </div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <button id="editSave" class="primary">Сохранить</button>
      <button id="editCancel">Отмена</button>
    </div>`;

  $('editCancel').onclick = () => openLead(l.id);
  wirePicker('edit');

  $('editSave').onclick = async () => {
    let payload;
    try {
      payload = pickerPayload('edit');
    } catch (e) {
      $('editErr').textContent = e.message;
      $('editErr').classList.add('on');
      return;
    }
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
      await loadNiches();       // могли появиться новая ниша или категория
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
  // Журналу нужны подписи полей и названия статусов, а живёт он
  // в отдельном файле и до модульной переменной не дотянется.
  window.CFG = CFG;
  $('who').textContent = CFG.login || '';
  // Отчёт по сотрудникам — только владельцу базы. Сервер всё равно
  // проверит права, но и показывать чужую кнопку незачем.
  $('mnMetrics').hidden = !CFG.is_admin;
  $('mnActivity').hidden = !CFG.is_admin;
  $('mnHistory').hidden = !CFG.is_admin;
  $('mnNiches').hidden = !CFG.is_admin;
  $('btnBackup').hidden = !CFG.is_admin;
  for (const [k, v] of Object.entries(CFG.reasons)) $('fReason').add(new Option(v, k));
  for (const [k, v] of Object.entries(CFG.statuses)) $('fStatus').add(new Option(v, k));
  $('dadataHint').textContent = CFG.dadata_ready ? '' : '— нужен токен в .env';
  $('optDadata').checked = CFG.dadata_ready;
  $('optDadata').disabled = !CFG.dadata_ready;

  wireSplitter();
  $('drawer').innerHTML = PLACEHOLDER;

  // Какую нишу открыть: из адреса, а если в нём нет — ту, где работали
  // в прошлый раз. Адрес тут же приводим в соответствие.
  let startNiche = new URLSearchParams(location.search).get('niche');
  if (startNiche === null) {
    try { startNiche = localStorage.getItem('niche') || ''; } catch (e) { startNiche = ''; }
  }
  currentNiche = startNiche || '';
  await loadNiches();
  if (currentNiche) {
    const u = new URL(location.href);
    u.searchParams.set('niche', currentNiche);
    history.replaceState(null, '', u);
  }
  // Сколько вкладок влезает, зависит от ширины окна
  let fitTimer;
  window.addEventListener('resize', () => {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(fitTabs, 100);
  });

  // Кнопки «назад» и «вперёд» браузера переключают вкладки ниш
  window.addEventListener('popstate', () => {
    openNiche(new URLSearchParams(location.search).get('niche') || '', false);
  });

  await loadStats();
  await loadLeads();

  // Из журнала приходят по ссылке на конкретный объект
  const wanted = new URLSearchParams(location.search).get('lead');
  if (wanted) openLead(Number(wanted));

  const rerun = () => { offset = 0; statFilter = null; loadLeads(); loadStats(); };
  let t;
  $('q').oninput = () => { clearTimeout(t); t = setTimeout(rerun, 300); };
  ['fReason', 'fCategory', 'fStatus', 'fHas', 'fHidden', 'fSort']
    .forEach((id) => $(id).onchange = rerun);

  $('prev').onclick = () => { offset = Math.max(0, offset - PAGE); loadLeads(); };
  $('next').onclick = () => { offset += PAGE; loadLeads(); };
  document.onkeydown = (e) => { if (e.key === 'Escape') closeLead(); };

  // Сводка занимает пол-экрана и нужна не всегда — по умолчанию свёрнута
  $('btnStats').onclick = () => {
    const box = $('stats');
    box.hidden = !box.hidden;
    $('btnStats').textContent = box.hidden ? 'Сводка' : 'Свернуть сводку';
  };

  // ── боковое меню ─────────────────────────────────────────────────────
  const menu = (open) => {
    // Скрытие прокрутки убирает системный скроллбар, и страница дёргается
    // вбок на его ширину. Компенсируем отступом ровно на эту ширину.
    const gap = open ? window.innerWidth - document.documentElement.clientWidth : 0;
    document.body.style.paddingRight = gap ? gap + 'px' : '';
    document.body.classList.toggle('noscroll', open);
    $('menu').hidden = !open;
    $('menuBack').hidden = !open;
  };
  $('btnMenu').onclick = () => menu(true);
  $('menuClose').onclick = () => menu(false);
  $('menuBack').onclick = () => menu(false);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu(false); });
  $('menu').querySelectorAll('.menuitem').forEach((el) => {
    el.addEventListener('click', () => menu(false));
  });

  $('btnExport').onclick = () => { location.href = '/api/export.csv?' + params(); };

  // Копия идёт в фоне десятки секунд — ждём и показываем итог
  $('btnBackup').onclick = async () => {
    const btn = $('btnBackup');
    btn.disabled = true;
    btn.textContent = 'Делаю копию…';
    try {
      await api('/api/backup', { method: 'POST' });
      let s;
      do {
        await new Promise((r) => setTimeout(r, 2000));
        s = await api('/api/backup');
      } while (s.running);
      const r = s.last || {};
      alert([
        r.file ? `Снимок базы: ${r.file}` : `Снимок базы не сделан: ${r.file_error}`,
        'rows' in r ? `Google Таблица: выгружено строк ${r.rows}` : `Google Таблица: ${r.sheet_error}`,
      ].join('\n'));
    } catch (e) {
      alert('Не получилось: ' + e.message);
    }
    btn.disabled = false;
    btn.textContent = 'Резервная копия сейчас';
  };

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
                     'addWa', 'addEmail', 'addAddr', 'addNote'];
  let addMapUrl = '';        // ссылка на карточку, если форму заполнили с карт

  // card — то, что прислала кнопка «В leadgen» с Яндекс Карт или 2ГИС
  const openAdd = (card) => {
    addFields.forEach((id) => { $(id).value = ''; });
    addMapUrl = '';
    // Объект по умолчанию ложится в ту нишу, что открыта сейчас
    let niche = currentNiche || NICHES.default_id;
    let cat = '';
    $('addFrom').hidden = !card;

    if (card) {
      const where = card.source === '2gis' ? '2ГИС' : 'Яндекс Карт';
      $('addName').value = card.name || '';
      $('addSite').value = card.website || '';
      $('addPhone').value = (card.phones || []).join(', ');
      $('addTg').value = card.telegram || '';
      $('addVk').value = card.vk || '';
      $('addWa').value = card.whatsapp || '';
      $('addEmail').value = card.email || '';
      $('addAddr').value = card.address || '';
      if (card.rubric) $('addNote').value = `Рубрика в ${where === '2ГИС' ? '2ГИС' : 'Яндекс Картах'}: ${card.rubric}`;
      addMapUrl = card.map_url || '';
      const guess = guessCategory(card.rubric);
      if (guess) { niche = guess.niche; cat = guess.cat; }
      $('addFrom').textContent = `Заполнено из ${where}. Проверьте поля и нишу, потом «Добавить».`;
    }

    $('addPicker').innerHTML = pickerHTML('add', niche, cat);
    wirePicker('add');
    $('addErr').classList.remove('on');
    $('addDialog').showModal();
    $('addName').focus();
  };
  $('btnAdd').onclick = () => openAdd(null);

  // Кнопка «В leadgen» открывает страницу с данными карточки после «#add=».
  // Часть после «#» на сервер не уходит; сразу убираем её из адреса, чтобы
  // обновление страницы не открывало форму второй раз.
  const takeCard = () => {
    if (!location.hash.startsWith('#add=')) return;
    let card = null;
    try {
      card = JSON.parse(decodeURIComponent(location.hash.slice(5)));
    } catch (e) { /* битая ссылка — просто не открываем форму */ }
    history.replaceState(null, '', location.pathname + location.search);
    if (card && typeof card === 'object') openAdd(card);
  };
  takeCard();
  window.addEventListener('hashchange', takeCard);
  $('addCancel').onclick = () => $('addDialog').close();

  $('addGo').onclick = async () => {
    const name = $('addName').value.trim();
    if (!name) {
      $('addErr').textContent = 'Без названия объект не добавить';
      $('addErr').classList.add('on');
      return;
    }
    let picked;
    try {
      picked = pickerPayload('add');
    } catch (e) {
      $('addErr').textContent = e.message;
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
          whatsapp: $('addWa').value, email: $('addEmail').value,
          map_url: addMapUrl,
          address: $('addAddr').value,
          note: $('addNote').value,
          ...picked,
        }),
      });
      $('addDialog').close();
      await loadNiches();
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
      // Без колонки niche объекты лягут в открытую нишу
      const r = await api('/api/import' + (currentNiche ? '?niche=' + currentNiche : ''),
                          { method: 'POST', body: fd });
      alert(`Добавлено лидов: ${r.added}`);
      loadNiches(); loadStats(); loadLeads();
    } catch (e) { alert('Ошибка импорта: ' + e.message); }
    $('btnImport').disabled = false;
    $('btnImport').textContent = 'Импорт CSV';
    $('fileInput').value = '';
  };

  const s = await api('/api/run/status');
  if (s.running) startPolling();
}

init();
