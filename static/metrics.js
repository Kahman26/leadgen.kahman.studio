'use strict';

// Страница только рисует: все формулы живут в metrics.py. Дублировать их
// здесь нельзя — разойдутся, а по этим цифрам платят людям.

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

const rub = (n) => (Number(n) || 0).toLocaleString('ru-RU') + ' ₽';
const num = (n) => (Number(n) || 0).toLocaleString('ru-RU');

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  if (a > 10 && a < 20) return many;
  const b = a % 10;
  if (b === 1) return one;
  return b > 1 && b < 5 ? few : many;
}

function dayRu(iso) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-');
  return `${d}.${m}.${y}`;
}

// Стрелка к прошлому периоду. Без неё число ничего не значит: «пять сделок» —
// это много или мало, понятно только рядом с прошлым месяцем.
function delta(d) {
  if (d === null || d === undefined) return '';
  if (d === 0) return '<span class="dl flat">без изменений</span>';
  const up = d > 0;
  return `<span class="dl ${up ? 'up' : 'down'}">${up ? '↑' : '↓'} ${Math.abs(d)} %</span>`;
}

function tile(title, value, d, note) {
  return `
    <div class="tile">
      <div class="tt">${esc(title)}</div>
      <div class="tv">${value}</div>
      <div class="tn">${delta(d)}${note ? ` <span class="muted">${note}</span>` : ''}</div>
    </div>`;
}

/* ── воронка ──────────────────────────────────────────────────────────── */

// Рисуем инлайновым SVG: никаких библиотек и внешних адресов на странице,
// закрытой от всех кроме владельца.
function funnelSvg(steps) {
  const max = Math.max(...steps.map((s) => s.value), 1);
  const rowH = 34, padL = 210, padR = 90, w = 760;
  const h = steps.length * rowH + 8;
  const bars = steps.map((s, i) => {
    const y = i * rowH + 4;
    const bw = Math.max(2, Math.round((w - padL - padR) * s.value / max));
    const cls = s.key === 'refused' ? 'fbar bad' : s.key === 'deal' ? 'fbar good' : 'fbar';
    return `
      <text class="flab" x="0" y="${y + 17}">${esc(s.title)}</text>
      <rect class="${cls}" x="${padL}" y="${y + 4}" width="${bw}" height="18" rx="3"></rect>
      <text class="fval" x="${padL + bw + 8}" y="${y + 17}">${num(s.value)}</text>
      ${s.conv === undefined ? '' :
        `<text class="fconv" x="${w - 4}" y="${y + 17}" text-anchor="end">${s.conv} %</text>`}`;
  }).join('');
  return `<svg class="funnel" viewBox="0 0 ${w} ${h}" role="img"
            aria-label="Воронка за период">${bars}</svg>`;
}

function funnelBlock(f) {
  const rows = f.steps.map((s) => `
    <tr>
      <td>${esc(s.title)}</td>
      <td class="num sum">${num(s.value)}</td>
      <td class="num">${s.conv === undefined ? '<span class="muted">—</span>' : s.conv + ' %'}</td>
      <td class="num">${delta(s.delta)}</td>
      <td class="muted">${s.note ? esc(s.note) : ''}</td>
    </tr>`).join('');
  return `
    <section class="mblock">
      <h2>Воронка</h2>
      ${funnelSvg(f.steps)}
      <table class="mtable">
        <thead><tr><th>Этап</th><th class="num">Число</th><th class="num">К предыдущему этапу</th>
          <th class="num">К прошлому периоду</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="mnote">
        Процент дозвона: <b>${f.dial_rate} %</b> ·
        сквозная конверсия «ручная проверка → сделка»: <b>${f.overall} %</b>
      </div>
    </section>`;
}

/* ── деньги ───────────────────────────────────────────────────────────── */

function moneyBlock(m) {
  const cycle = m.cycle_n
    ? `${m.cycle_days} ${plural(m.cycle_days, 'день', 'дня', 'дней')}`
    : '<span class="muted">нет данных</span>';
  return `
    <section class="mblock">
      <h2>Деньги за период</h2>
      <div class="tiles">
        ${tile('Поступило', rub(m.total), m.total_delta)}
        ${tile('По новым сделкам', rub(m.new), m.new_delta)}
        ${tile('По повторным', rub(m.repeat), m.repeat_delta)}
        ${tile('Сделок закрыто', num(m.closed), m.closed_delta)}
        ${tile('Средний чек', rub(m.avg_check), m.avg_check_delta)}
        ${tile('Доля первой оплаты', m.avg_first_share + ' %', null,
               m.low_first_share ? '<b class="warn">ниже порога договора</b>' : '')}
        ${tile('Цикл сделки', cycle, m.cycle_days_delta,
               m.cycle_n ? `медиана по ${m.cycle_n}` : '')}
        ${tile('MRR', rub(m.mrr), m.mrr_delta)}
        ${tile('Клиентов на абонентке', m.retainer_share + ' %', null)}
      </div>
      <div class="mnote muted">
        Цикл считается от первого звонка по лиду до первого поступления.
        Медиана, а не среднее: одна затянувшаяся сделка перекосила бы среднее.
      </div>
    </section>`;
}

/* ── вознаграждение ───────────────────────────────────────────────────── */

function rewardBlock(r) {
  if (!r.rows.length) {
    return `<section class="mblock"><h2>Вознаграждение продажника</h2>
      <div class="empty">За период поступлений не было — платить не с чего.</div></section>`;
  }
  const rows = r.rows.map((s) => `
    <tr>
      <td><b>${esc(s.login)}</b></td>
      <td class="num">${rub(s.paid_new)}</td>
      <td class="num sum">${rub(s.reward_new)}</td>
      <td class="num">${rub(s.paid_repeat)}</td>
      <td class="num sum">${rub(s.reward_repeat)}</td>
      <td class="num">${s.deals_bonus} из ${s.deals}</td>
      <td class="num sum">${rub(s.bonus)}</td>
      <td class="num total">${rub(s.total)}</td>
    </tr>
    ${s.bonus_next ? `<tr class="subrow"><td colspan="8" class="muted">
        ${esc(s.login)}: до премии ${rub(s.bonus_next[1])} не хватает
        ${s.bonus_next[0] - s.deals_bonus}
        ${plural(s.bonus_next[0] - s.deals_bonus, 'сделки', 'сделок', 'сделок')}
      </td></tr>` : ''}`).join('');

  const due = r.overdue
    ? `<b class="warn">срок выплаты прошёл: было до ${dayRu(r.due)}</b>`
    : r.due_soon
      ? `<b class="warn">выплатить до ${dayRu(r.due)} — осталось ${r.days_left}
         ${plural(r.days_left, 'день', 'дня', 'дней')}</b>`
      : `выплатить до ${dayRu(r.due)}`;

  return `
    <section class="mblock">
      <h2>Вознаграждение продажника</h2>
      <table class="mtable">
        <thead><tr>
          <th>Продавец</th>
          <th class="num">Поступило, новые</th><th class="num">${r.rate_new} %</th>
          <th class="num">Поступило, повторные</th><th class="num">${r.rate_repeat} %</th>
          <th class="num">В зачёт премии</th><th class="num">Премия</th>
          <th class="num">Итого</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="mnote">${due}</div>
      <div class="mnote muted">
        Вознаграждение считается от фактически поступивших сумм, а не от цены
        договора. В зачёт премии идут сделки, где доля первой оплаты не ниже
        ${r.min_share} %. Премии не суммируются — берётся наибольшая достигнутая.
      </div>
    </section>`;
}

/* ── конверты ─────────────────────────────────────────────────────────── */

function envelopeBlock(e) {
  const rows = e.rows.map((x) => `
    <tr>
      <td>${esc(x.name)}</td>
      <td class="num">${x.pct} %</td>
      <td class="num sum">${rub(x.period)}</td>
      <td class="num">${rub(x.saved)}</td>
      <td>${x.role_cost ? (x.ready
            ? '<span class="ok">хватает на 3 месяца</span>'
            : `<span class="muted">на ${x.months} мес., не хватает ${rub(x.need)}</span>`)
          : ''}</td>
    </tr>`).join('');
  return `
    <section class="mblock">
      <h2>Конверты</h2>
      ${e.share_ok ? '' :
        `<div class="mwarn">Сумма долей ${e.share_sum} % вместо 100 % —
         раскладка не сходится с выручкой. Поправьте в настройках.</div>`}
      <table class="mtable">
        <thead><tr><th>Конверт</th><th class="num">Доля</th>
          <th class="num">За период</th><th class="num">Накоплено всего</th>
          <th>Фонд найма</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr>
          <td><b>Итого</b></td><td class="num">${e.share_sum} %</td>
          <td class="num sum"><b>${rub(e.period_total)}</b></td>
          <td class="num"><b>${rub(e.ever_total)}</b></td>
          <td class="muted">${e.check_ok ? 'сходится с поступлениями' : 'расхождение'}</td>
        </tr></tfoot>
      </table>
    </section>`;
}

/* ── триггеры найма ───────────────────────────────────────────────────── */

function hiringBlock(rows) {
  const items = rows.map((r) => {
    const fund = r.fund;
    // Светофор: зелёный — можно брать, жёлтый — поток есть, но денег нет,
    // серый — рано. Деньги и поток разведены намеренно: нанимать без
    // трёхмесячного запаса нельзя, даже когда заявок хватает.
    const money = !fund || !fund.role_cost ? null : fund.ready;
    const light = r.ok ? (money === false ? 'warn' : 'ok') : 'off';
    return `
      <div class="hrole ${light}">
        <div class="hname"><i></i>${esc(r.role)}</div>
        <div class="muted">${esc(r.rule)}</div>
        <div>${esc(r.fact)}</div>
        ${fund && fund.role_cost ? `<div class="muted">
          конверт «${esc(fund.name)}»: ${rub(fund.saved)}${fund.ready
            ? ', хватает на 3 месяца'
            : `, не хватает ${rub(fund.need)}`}</div>` : ''}
      </div>`;
  }).join('');
  return `<section class="mblock"><h2>Триггеры найма</h2>
    <div class="hroles">${items}</div></section>`;
}

/* ── лимит НПД ────────────────────────────────────────────────────────── */

function npdBlock(n) {
  return `
    <section class="mblock">
      <h2>Лимит самозанятости</h2>
      <div class="npdbar"><i class="${n.warn ? 'warn' : ''}" style="width:${Math.min(n.pct, 100)}%"></i></div>
      <div class="mnote">
        За скользящие 12 месяцев поступило <b>${rub(n.got)}</b> из ${rub(n.limit)} —
        это ${n.pct} %. Осталось ${rub(n.left)}.
        ${n.rate ? `Средний темп ${rub(n.rate)} в месяц.` : ''}
        ${n.forecast ? `При нём лимит будет исчерпан к ${dayRu(n.forecast)}.` : ''}
      </div>
      ${n.warn ? `<div class="mwarn">Пройдено больше ${n.warn_at} % лимита —
        пора открывать ИП, не дожидаясь превышения: пересчёт задним числом больнее.</div>` : ''}
    </section>`;
}

/* ── команда ──────────────────────────────────────────────────────────── */

function teamBlock(rows) {
  if (!rows.length) {
    return `<section class="mblock"><h2>Активность команды</h2>
      <div class="empty">За период никто ничего не делал.</div></section>`;
  }
  const body = rows.map((s) => `
    <tr>
      <td><b>${esc(s.login)}</b></td>
      <td class="num">${num(s.leads)}</td>
      <td class="num">${num(s.calls)}</td>
      <td class="num">${num(s.answered)}</td>
      <td class="num">${s.dial_rate} %</td>
      <td class="num">${num(s.notes)}</td>
      <td class="num">${num(s.statuses)}</td>
    </tr>`).join('');
  return `
    <section class="mblock">
      <h2>Активность команды</h2>
      <table class="mtable">
        <thead><tr><th>Кто</th><th class="num">Завёл лидов</th>
          <th class="num">Звонков</th><th class="num">Дозвонов</th>
          <th class="num">Процент дозвона</th><th class="num">Заметок</th>
          <th class="num">Смен статуса</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
      <div class="mnote"><a href="/activity">Сколько времени они провели в базе →</a></div>
    </section>`;
}

/* ── настройки ────────────────────────────────────────────────────────── */

// Поля описаны здесь, а не собираются из данных: у каждой настройки свой
// смысл, и подпись «rate_new» владельцу ничего не скажет.
const SETTING_FIELDS = [
  ['rate_new', 'Ставка по новым сделкам, %', 'number'],
  ['rate_repeat', 'Ставка по повторным, %', 'number'],
  ['first_share_min', 'Порог доли первой оплаты, %', 'number'],
  ['npd_limit', 'Лимит НПД, ₽', 'number'],
  ['npd_warn', 'Предупреждать с, %', 'number'],
];

function settingsBlock(st) {
  const simple = SETTING_FIELDS.map(([key, label]) => `
    <label class="sfield">
      <span>${esc(label)}</span>
      <input type="number" data-set="${key}" value="${esc(st[key])}">
    </label>`).join('');

  const env = Object.entries(st.envelopes).map(([name, pct]) => `
    <label class="sfield">
      <span>${esc(name)}</span>
      <input type="number" data-env="${esc(name)}" value="${pct}">
    </label>`).join('');

  const bonus = st.bonus_levels.map(([need, sum], i) => `
    <label class="sfield">
      <span>Премия за ${need} ${plural(need, 'сделку', 'сделки', 'сделок')}</span>
      <input type="number" data-bonus="${i}" value="${sum}">
    </label>`).join('');

  const roles = Object.entries(st.role_costs).map(([name, cost]) => `
    <label class="sfield">
      <span>${esc(name)}, ₽/мес</span>
      <input type="number" data-role="${esc(name)}" value="${cost}">
    </label>`).join('');

  return `
    <section class="mblock">
      <h2><button class="linkbtn" id="setToggle">Настройки ▾</button></h2>
      <div id="setBox" hidden>
        <div class="mnote muted">Ставки и пороги живут в базе, а не в коде:
          договор прямо предусматривает их пересмотр.</div>
        <h3>Договор с продажником</h3>
        <div class="sgrid">${simple}</div>
        <h3>Премии</h3>
        <div class="sgrid">${bonus}</div>
        <h3>Конверты, %</h3>
        <div class="sgrid">${env}</div>
        <div class="muted" id="envSum"></div>
        <h3>Стоимость роли в месяц</h3>
        <div class="sgrid">${roles}</div>
        <div style="margin-top:12px">
          <button class="primary" id="setSave">Сохранить настройки</button>
          <span class="muted" id="setMsg"></span>
        </div>
      </div>
    </section>`;
}

function wireSettings(st) {
  $('setToggle').onclick = () => {
    const box = $('setBox');
    box.hidden = !box.hidden;
    $('setToggle').textContent = box.hidden ? 'Настройки ▾' : 'Настройки ▴';
  };

  // Сумму долей показываем сразу при вводе: узнать о расхождении при
  // сохранении — значит гадать, какое поле поправить.
  const envInputs = [...document.querySelectorAll('[data-env]')];
  const recount = () => {
    const sum = envInputs.reduce((a, el) => a + (Number(el.value) || 0), 0);
    $('envSum').innerHTML = sum === 100
      ? 'Сумма долей: 100 %'
      : `<b class="warn">Сумма долей: ${sum} % — должно быть 100 %</b>`;
  };
  envInputs.forEach((el) => (el.oninput = recount));
  recount();

  $('setSave').onclick = async () => {
    const payload = {};
    SETTING_FIELDS.forEach(([key]) => {
      payload[key] = Number(document.querySelector(`[data-set="${key}"]`).value);
    });
    payload.envelopes = {};
    envInputs.forEach((el) => (payload.envelopes[el.dataset.env] = Number(el.value)));
    payload.bonus_levels = st.bonus_levels.map(([need], i) =>
      [need, Number(document.querySelector(`[data-bonus="${i}"]`).value)]);
    payload.role_costs = {};
    document.querySelectorAll('[data-role]').forEach((el) => {
      payload.role_costs[el.dataset.role] = Number(el.value);
    });

    $('setSave').disabled = true;
    try {
      await api('/api/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      $('setMsg').innerHTML = `<b class="warn">${esc(e.message)}</b>`;
      $('setSave').disabled = false;
      return;
    }
    $('setMsg').textContent = ' сохранено, пересчитываю…';
    load();
  };
}

/* ── загрузка ─────────────────────────────────────────────────────────── */

let presets = null;

async function load() {
  const from = $('dFrom').value;
  const to = $('dTo').value;
  const q = from && to ? `?from=${from}&to=${to}` : '';
  $('body').innerHTML = '<div class="empty">Считаем…</div>';

  let d;
  try {
    d = await api('/api/metrics' + q);
  } catch (e) {
    $('body').innerHTML = `<div class="empty">Не удалось посчитать: ${esc(e.message)}</div>`;
    return;
  }
  presets = d.presets;
  $('dFrom').value = d.range.from;
  $('dTo').value = d.range.to;
  $('range').textContent =
    `${dayRu(d.range.from)} — ${dayRu(d.range.to)}, сравнение с ` +
    `${dayRu(d.range.prev_from)} — ${dayRu(d.range.prev_to)}`;

  $('body').innerHTML =
    funnelBlock(d.funnel) + moneyBlock(d.money) + rewardBlock(d.reward)
    + envelopeBlock(d.envelopes) + hiringBlock(d.hiring) + npdBlock(d.npd)
    + teamBlock(d.team) + settingsBlock(d.settings);
  wireSettings(d.settings);
}

document.querySelectorAll('.per').forEach((b) => {
  b.onclick = () => {
    if (!presets) return;
    const [from, to] = presets[b.dataset.per];
    $('dFrom').value = from;
    $('dTo').value = to;
    document.querySelectorAll('.per').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    load();
  };
});
$('apply').onclick = () => {
  document.querySelectorAll('.per').forEach((x) => x.classList.remove('on'));
  load();
};

load();
