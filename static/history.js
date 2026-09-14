'use strict';

const $ = (id) => document.getElementById(id);
const esc = HistView.esc;

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('Требуется вход');
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const PAGE = 100;
let offset = 0;
let filled = false;      // выпадающие списки заполняются один раз
// Из отчёта о рабочем времени сюда приходят со ссылкой ?login=…&day=…
// Список сотрудников приезжает только с первым ответом, поэтому до его
// заполнения фильтр по человеку берём прямо из ссылки.
let wanted = '';

function params() {
  const p = new URLSearchParams();
  const put = (k, v) => { if (v) p.set(k, v); };
  put('q', $('q').value.trim());
  put('login', $('fPerson').value || wanted);
  put('action', $('fAction').value);
  put('since', $('fSince').value);
  put('until', $('fUntil').value);
  if ($('fSystem').checked) p.set('system', '1');
  p.set('limit', PAGE);
  p.set('offset', offset);
  return p.toString();
}

async function load() {
  $('body').innerHTML = '<div class="empty">Загружаем…</div>';

  let data;
  try {
    data = await api('/api/history?' + params());
  } catch (e) {
    $('body').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }

  if (!filled) {
    data.people.forEach((n) => $('fPerson').add(new Option(n, n)));
    Object.entries(data.actions).forEach(([k, v]) => $('fAction').add(new Option(v, k)));
    if (wanted) $('fPerson').value = wanted;
    filled = true;
  }

  const word = (n) => {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return 'записей';
    if (b === 1) return 'запись';
    return b > 1 && b < 5 ? 'записи' : 'записей';
  };
  $('count').textContent = data.total ? `${data.total} ${word(data.total)}` : '';

  // Заголовок дня над группой: лента без него читается сплошной простынёй.
  let day = '';
  const html = data.rows.map((e) => {
    const head = e.day === day ? '' : `<div class="hday">${esc(e.when.slice(0, 10))}</div>`;
    day = e.day;
    return head + HistView.row(e, { withLead: true, canUndo: true });
  }).join('');

  $('body').innerHTML = html || '<div class="empty">Ничего не найдено</div>';
  HistView.wireUndo($('body'), api, load);

  const shown = data.rows.length;
  $('page').textContent = data.total
    ? `${offset + 1}–${offset + shown} из ${data.total}` : '';
  $('prev').disabled = offset === 0;
  $('next').disabled = offset + shown >= data.total;
}

const rerun = () => { offset = 0; load(); };

async function init() {
  // Подписи полей и названия статусов берём из общей конфигурации:
  // журнал должен говорить «Телефон», а не phone.
  window.CFG = await api('/api/config');

  let t;
  $('q').oninput = () => { clearTimeout(t); t = setTimeout(rerun, 300); };
  ['fPerson', 'fAction', 'fSince', 'fUntil', 'fSystem']
    .forEach((id) => $(id).onchange = rerun);

  $('prev').onclick = () => { offset = Math.max(0, offset - PAGE); load(); };
  $('next').onclick = () => { offset += PAGE; load(); };

  const from = new URLSearchParams(location.search);
  wanted = from.get('login') || '';
  if (from.get('day')) {
    $('fSince').value = from.get('day');
    $('fUntil').value = from.get('day');
  }

  await load();
}

init();
