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

const post = (url, body) => api(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

let DATA = { niches: [] };

// 1 объект, 2 объекта, 5 объектов
const plural = (n, one, few, many) => `${n} ${n % 10 === 1 && n % 100 !== 11 ? one
  : [2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100) ? few : many}`;
const objects = (n) => plural(n, 'объект', 'объекта', 'объектов');

// Все действия возвращают свежий справочник целиком — просто перерисовываем
async function act(promise) {
  try {
    DATA = await promise;
    render();
  } catch (e) {
    alert(e.message);
  }
}

function actions(kind, item, first, last) {
  // Нишу, куда пишет сбор, сервер удалить не даст — кнопку и не показываем
  const fixed = kind === 'niche' && item.code === 'booking';
  return `
    <div class="nacts">
      <button data-act="up" data-kind="${kind}" data-id="${item.id}" ${first ? 'disabled' : ''}
              title="Выше">↑</button>
      <button data-act="down" data-kind="${kind}" data-id="${item.id}" ${last ? 'disabled' : ''}
              title="Ниже">↓</button>
      <button data-act="rename" data-kind="${kind}" data-id="${item.id}">Переименовать</button>
      <button data-act="merge" data-kind="${kind}" data-id="${item.id}">Объединить…</button>
      ${fixed ? '' : `<button data-act="delete" data-kind="${kind}" data-id="${item.id}"
              class="danger" title="Удалить можно только пустую">Удалить</button>`}
    </div>`;
}

function render() {
  const all = DATA.niches;
  $('summary').textContent = plural(all.length, 'ниша', 'ниши', 'ниш') + ' · ' +
    plural(all.reduce((s, n) => s + n.categories.length, 0),
           'категория', 'категории', 'категорий');

  $('list').innerHTML = all.map((n, i) => `
    <div class="nblock">
      <div class="nhead">
        <h2>${esc(n.title)}</h2>
        <span class="ncount">${objects(n.count)}${
          n.no_category ? `, без категории ${n.no_category}` : ''}${
          n.code === 'booking' ? ' · сюда пишет сбор' : ''}</span>
        <span class="spacer"></span>
        ${actions('niche', n, i === 0, i === all.length - 1)}
      </div>
      <ul class="ncats">
        ${n.categories.map((c, j) => `
          <li>
            <span class="ctitle">${esc(c.title)}</span>
            <span class="ncount">${objects(c.count)}</span>
            ${actions('cat', c, j === 0, j === n.categories.length - 1)}
          </li>`).join('') || '<li class="muted">Категорий пока нет</li>'}
      </ul>
      <div class="nadd">
        <input type="text" data-newcat="${n.id}" placeholder="Новая категория" autocomplete="off">
        <button data-addcat="${n.id}">Добавить</button>
      </div>
    </div>`).join('');

  $('list').querySelectorAll('[data-act]').forEach((b) => {
    b.onclick = () => handle(b.dataset.act, b.dataset.kind, Number(b.dataset.id));
  });
  $('list').querySelectorAll('[data-addcat]').forEach((b) => {
    const input = $('list').querySelector(`[data-newcat="${b.dataset.addcat}"]`);
    const add = async () => {
      const title = input.value.trim();
      if (!title) return;
      await act(post(`/api/niches/${b.dataset.addcat}/categories`, { title }));
    };
    b.onclick = add;
    input.onkeydown = (e) => { if (e.key === 'Enter') add(); };
  });
}

function find(kind, id) {
  for (const n of DATA.niches) {
    if (kind === 'niche' && n.id === id) return { item: n, niche: n };
    const c = n.categories.find((x) => x.id === id);
    if (kind === 'cat' && c) return { item: c, niche: n };
  }
  return {};
}

function handle(action, kind, id) {
  const { item, niche } = find(kind, id);
  if (!item) return;
  const base = kind === 'niche' ? `/api/niches/${id}` : `/api/categories/${id}`;

  if (action === 'up' || action === 'down') {
    return act(post(base, { move: action === 'up' ? -1 : 1 }));
  }
  if (action === 'rename') {
    const title = (prompt('Новое название', item.title) || '').trim();
    if (!title || title === item.title) return;
    return act(post(base, { title }));
  }
  if (action === 'delete') {
    if (item.count || (kind === 'niche' && niche.no_category)) {
      alert('Здесь есть объекты. Сначала объедините с другой — объекты переедут туда.');
      return;
    }
    if (!confirm(`Удалить «${item.title}»?`)) return;
    return act(api(base, { method: 'DELETE' }));
  }
  if (action === 'merge') openMerge(kind, item, niche, base);
}

function openMerge(kind, item, niche, base) {
  // Категории объединяются только внутри своей ниши: перенос между нишами —
  // это объединение самих ниш.
  const targets = kind === 'niche'
    ? DATA.niches.filter((n) => n.id !== item.id)
    : niche.categories.filter((c) => c.id !== item.id);
  if (!targets.length) {
    alert(kind === 'niche' ? 'Других ниш нет' : 'В этой нише нет других категорий');
    return;
  }
  $('mergeTitle').textContent = `Объединить «${item.title}»`;
  $('mergeText').textContent = kind === 'niche'
    ? `${objects(item.count)} и все категории переедут в выбранную нишу, одноимённые категории склеятся. Ниша «${item.title}» исчезнет.`
    : `${objects(item.count)} переедут в выбранную категорию, а «${item.title}» исчезнет.`;
  $('mergeTarget').innerHTML = targets.map((t) =>
    `<option value="${t.id}">${esc(t.title)}</option>`).join('');
  $('mergeErr').classList.remove('on');
  $('mergeGo').onclick = async () => {
    try {
      DATA = await post(base, { merge_into: Number($('mergeTarget').value) });
      $('mergeDialog').close();
      render();
    } catch (e) {
      $('mergeErr').textContent = e.message;
      $('mergeErr').classList.add('on');
    }
  };
  $('mergeDialog').showModal();
}

$('mergeCancel').onclick = () => $('mergeDialog').close();

const addNiche = async () => {
  const title = $('newNiche').value.trim();
  if (!title) return;
  await act(post('/api/niches', { title }));
  $('newNiche').value = '';
};
$('addNiche').onclick = addNiche;
$('newNiche').onkeydown = (e) => { if (e.key === 'Enter') addNiche(); };

act(api('/api/niches'));
