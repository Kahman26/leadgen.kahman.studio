'use strict';

const $ = (id) => document.getElementById(id);

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('Требуется вход');
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

// «7 ч 25 мин» читается быстрее, чем 7.42 — отчёт смотрят глазами, а не считают.
function hours(sec) {
  // Округляем сразу до минут, а не отдельно часы и остаток: иначе
  // 4 ч 59,5 мин превращается в «4 ч 60 мин».
  const total = Math.round(sec / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (!h) return `${m} мин`;
  return m ? `${h} ч ${m} мин` : `${h} ч`;
}

// Русские числительные: 1 правка, 2 правки, 5 правок
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  if (a > 10 && a < 20) return many;
  const b = a % 10;
  if (b === 1) return one;
  return b > 1 && b < 5 ? few : many;
}

function dayLabel(iso) {
  const d = new Date(iso + 'T00:00:00');
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `${dd}.${mm}, ${WEEKDAYS[d.getDay()]}`;
}

function dayRow(d, longest, login) {
  // Полоса показывает день относительно самого длинного в выборке: так сразу
  // видно, где смена была полной, а где человек заглянул на десять минут.
  const width = longest ? Math.max(2, Math.round(d.seconds / longest * 100)) : 0;
  const spans = d.intervals
    .map((i) => `<span class="chip" title="${hours(i.seconds)}">${esc(i.from)}–${esc(i.to)}</span>`)
    .join(' ');
  // «Сидел 6 часов» само по себе ничего не значит. Ссылка ведёт в журнал
  // за этот день по этому человеку — там видно, что он за это время сделал.
  const link = `/history?login=${encodeURIComponent(login)}&day=${d.day}`;
  return `
    <tr>
      <td class="day"><a href="${link}" title="Что он делал в этот день">${dayLabel(d.day)}</a></td>
      <td class="num sum">${hours(d.seconds)}</td>
      <td class="barcell"><i style="width:${width}%"></i></td>
      <td><div class="chips">${spans}</div></td>
    </tr>`;
}

function personBlock(p, edits) {
  if (!p.days.length) {
    return `<div class="person">
      <div class="personhead"><h2>${esc(p.login)}</h2>
        <span class="muted">за период не работал</span></div>
    </div>`;
  }

  const longest = Math.max(...p.days.map((d) => d.seconds));
  const workdays = p.days.length;
  const mark = p.online
    ? '<span class="chip online">сейчас в базе</span>'
    : (p.last_seen ? `<span class="muted">был ${esc(p.last_seen)}</span>` : '');

  return `
    <div class="person">
      <div class="personhead">
        <h2>${esc(p.login)}</h2>
        ${mark}
        <span class="spacer"></span>
        <span class="total">${hours(p.total)}</span>
        <span class="muted">за ${workdays} ${plural(workdays, 'день', 'дня', 'дней')}
          · в среднем ${hours(Math.round(p.total / workdays))} в день${
          edits ? ` · ${edits.edits} ${plural(edits.edits, 'правка', 'правки', 'правок')}
          по ${edits.leads} ${plural(edits.leads, 'объекту', 'объектам', 'объектам')}` : ''}</span>
      </div>
      <div class="tablebox">
        <table>
          <thead><tr>
            <th>День</th><th class="num">Итого</th><th></th><th>Промежутки работы</th>
          </tr></thead>
          <tbody>${p.days.map((d) => dayRow(d, longest, p.login)).join('')}</tbody>
        </table>
      </div>
    </div>`;
}

async function load() {
  const days = $('fDays').value;
  const login = $('fPerson').value;
  $('body').innerHTML = '<div class="empty">Загружаем…</div>';

  let data;
  try {
    data = await api(`/api/activity?days=${days}&login=${encodeURIComponent(login)}`);
  } catch (e) {
    $('body').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }

  // Список сотрудников приходит вместе с отчётом — новый человек в .env
  // появляется в выпадающем списке сам, без правки страницы.
  const chosen = $('fPerson').value;
  $('fPerson').innerHTML = '<option value="">Все сотрудники</option>' +
    data.people.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  $('fPerson').value = chosen;

  $('range').textContent = `${dayLabel(data.since)} — ${dayLabel(data.until)}`;

  $('body').innerHTML = data.report.length
    ? data.report.map((p) => personBlock(p, (data.edits || {})[p.login])).join('')
    : '<div class="empty">За этот период в базе никто не работал</div>';
}

$('fDays').onchange = load;
$('fPerson').onchange = load;
load();
