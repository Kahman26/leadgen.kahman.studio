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

// Объективная часть: то же, что аудит показывает по лидам
function facts(r) {
  if (r.site_status && r.site_status !== 'ok') {
    return `<span class="chip off">сайт не отвечает (${esc(r.site_status)})</span>`;
  }
  const out = [];
  out.push(r.mobile_ready
    ? '<span class="chip">адаптив есть</span>'
    : '<span class="chip off">без адаптива</span>');

  if (r.booking_type === 'engine') {
    out.push(`<span class="chip">бронь онлайн${r.booking_engine ? ' — ' + esc(r.booking_engine) : ''}</span>`);
  } else if (r.booking_type === 'request') {
    out.push('<span class="chip off">только заявка</span>');
  } else if (r.booking_type) {
    out.push('<span class="chip off">без брони</span>');
  }

  if (r.cms) out.push(`<span class="chip">${esc(r.cms)}</span>`);
  if (r.load_ms) {
    const slow = r.load_ms > 3000;
    out.push(`<span class="chip${slow ? ' off' : ''}">${(r.load_ms / 1000).toFixed(1)} с</span>`);
  }
  if (!r.https) out.push('<span class="chip off">без HTTPS</span>');
  return out.join(' ');
}

function card(r) {
  return `
    <div class="refcard" data-id="${r.id}">
      <div class="refhead">
        <div>
          <a class="refname" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)} ↗</a>
          <div class="muted" style="font-size:12px">
            ${esc(r.city || '')}${r.city && r.page_title ? ' · ' : ''}${esc((r.page_title || '').slice(0, 60))}
          </div>
        </div>
        <div class="refacts">
          <button data-act="recheck" title="Перепроверить сайт">↻</button>
          <button data-act="delete" class="danger" title="Убрать из референсов">✕</button>
        </div>
      </div>

      ${r.note ? `<div class="refnote">${esc(r.note)}</div>` : ''}

      ${(r.strengths || []).length ? `<ul class="missing" style="margin-top:8px">${
        r.strengths.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}

      <div class="chips" style="margin-top:10px">${facts(r)}</div>
    </div>`;
}

async function load() {
  const cat = $('fCat').value;
  const data = await api('/api/refs' + (cat ? '?category=' + encodeURIComponent(cat) : ''));

  const names = Object.keys(data.groups);
  $('groups').innerHTML = names.map((name) => `
    <div class="refgroup">
      <h2>${esc(name)} <span class="muted">— ${data.groups[name].length}</span></h2>
      <div class="refgrid">${data.groups[name].map(card).join('')}</div>
    </div>`).join('');

  $('empty').hidden = data.total > 0;
  $('count').textContent = data.total ? `${data.total} референсов` : '';

  // список типов заполняем один раз, из того, что реально есть
  const sel = $('fCat');
  if (sel.options.length <= 1 && !cat) {
    const list = $('refCatList');
    names.forEach((n) => { sel.add(new Option(n, n)); list.appendChild(new Option(n)); });
  }

  document.querySelectorAll('[data-act]').forEach((b) => b.onclick = async (e) => {
    const id = e.target.closest('.refcard').dataset.id;
    b.disabled = true;
    try {
      if (b.dataset.act === 'recheck') {
        await api(`/api/refs/${id}/recheck`, { method: 'POST' });
      } else if (confirm('Убрать этот референс?')) {
        await api(`/api/refs/${id}`, { method: 'DELETE' });
      }
      await load();
    } catch (err) {
      alert(err.message);
      b.disabled = false;
    }
  });
}

const FIELDS = ['refName', 'refUrl', 'refCat', 'refCity', 'refNote', 'refStrengths'];

function init() {
  $('fCat').onchange = load;

  $('btnAddRef').onclick = () => {
    FIELDS.forEach((id) => { $(id).value = ''; });
    $('refErr').classList.remove('on');
    $('refDialog').showModal();
    $('refName').focus();
  };
  $('refCancel').onclick = () => $('refDialog').close();

  $('refGo').onclick = async () => {
    $('refErr').classList.remove('on');
    $('refGo').disabled = true;
    $('refGo').textContent = 'Проверяю сайт…';
    try {
      await api('/api/refs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: $('refName').value, url: $('refUrl').value,
          category: $('refCat').value, city: $('refCity').value,
          note: $('refNote').value, strengths: $('refStrengths').value,
        }),
      });
      $('refDialog').close();
      await load();
    } catch (e) {
      $('refErr').textContent = e.message;
      $('refErr').classList.add('on');
    }
    $('refGo').disabled = false;
    $('refGo').textContent = 'Добавить';
  };

  load();
}

init();
