'use strict';

// Отрисовка записей журнала. Один и тот же вид нужен и в карточке объекта,
// и в общей ленте, поэтому живёт отдельно от обеих страниц.
window.HistView = (() => {

  const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  const cut = (s, n) => (s.length > n ? s.slice(0, n) + '…' : s);

  const label = (field) => (window.CFG?.field_labels || {})[field] || field || '';

  // Значения хранятся как есть — журнал должен уметь вернуть их обратно.
  // Человеческий вид собираем только при показе.
  function value(field, raw) {
    const v = String(raw ?? '');
    if (v === '') return '<i class="hempty">пусто</i>';
    if (field === 'hidden') return v === '1' ? 'скрыт' : 'в списке';
    if (field === 'status') return esc((window.CFG?.statuses || {})[v] || v);
    if (field === 'reason_code') return esc((window.CFG?.reasons || {})[v] || v);
    if (field === 'ai_is_open') {
      return { true: 'работает', false: 'закрылся' }[v] || esc(v);
    }
    if (field === 'site_status') {
      return { ok: 'работает', dead: 'не открывается', none: 'сайта нет',
               parked: 'сайта компании нет', blocked: 'закрыт защитой' }[v] || esc(v);
    }
    return esc(cut(v, 120));
  }

  function row(e, opts) {
    opts = opts || {};
    const lead = opts.withLead && e.lead_id
      ? `<a class="hlead" href="/?lead=${e.lead_id}">${esc(e.lead_name || '№' + e.lead_id)}</a>`
      : '';

    const body = e.field
      ? `<b>${esc(label(e.field))}</b>: ${value(e.field, e.old)}
         <span class="harrow">→</span> ${value(e.field, e.new)}`
      : (e.new ? `${esc(e.action_text)}: ${esc(cut(String(e.new), 120))}`
               : esc(e.action_text));

    const undo = (opts.canUndo && e.undoable)
      ? `<button class="hundo" data-undo="${e.id}" title="Вернуть прежнее значение">вернуть</button>`
      : (e.reverted ? '<span class="hdone">возвращено</span>' : '');

    return `
      <div class="hrow${e.reverted ? ' reverted' : ''}">
        <div class="hmeta">
          <span class="hwhen">${esc(e.when)}</span>
          <span class="hwho${e.by_system ? ' sys' : ''}">${esc(e.login)}</span>
          ${lead}
          ${e.field ? `<span class="haction">${esc(e.action_text)}</span>` : ''}
        </div>
        <div class="hbody">${body}</div>
        ${undo}
      </div>`;
  }

  // Общий обработчик кнопок «вернуть»: после отката вызывает after().
  function wireUndo(box, api, after) {
    box.querySelectorAll('[data-undo]').forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        b.textContent = 'возвращаю…';
        try {
          await api(`/api/history/${b.dataset.undo}/revert`, { method: 'POST' });
          await after();
        } catch (err) {
          b.disabled = false;
          b.textContent = 'вернуть';
          alert('Не получилось: ' + err.message);
        }
      };
    });
  }

  return { esc, label, value, row, wireUndo };
})();
