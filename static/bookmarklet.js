/* Кнопка «В leadgen» для закладок браузера.

   Работает на открытой карточке организации в Яндекс Картах или 2ГИС:
   читает с экрана название, телефоны, сайт, соцсети, адрес и рубрику и
   открывает leadgen с заполненной формой «+ Объект». Ничего не отправляет
   сама: данные уходят в адрес новой вкладки после «#», а он до сервера не
   доходит — сохраняет объект человек, нажав «Добавить».

   Одна карточка — одно нажатие. Это ускоренное ручное копирование, а не
   сбор каталога: обходить списки и листать выдачу кнопка не умеет и не
   должна.

   Страница /bookmarklet превращает этот файл в ссылку javascript:, поэтому
   комментарии здесь только блочные: строчные при сборке не вырезаются. */
(function (ORIGIN) {
  var out = { name: '', phones: [], website: '', address: '', telegram: '', vk: '',
              whatsapp: '', email: '', rubric: '', source: '', map_url: '' };
  var clean = function (s) { return String(s || '').replace(/\s+/g, ' ').trim(); };
  var push = function (list, v) { v = clean(v); if (v && list.indexOf(v) < 0) list.push(v); };

  /* Ссылка из карточки: соцсеть раскладываем по полям, остальное — сайт */
  var link = function (href) {
    var u;
    try { u = new URL(href); } catch (e) { return; }
    var h = u.hostname.replace(/^www\./, '');
    if (/^(t\.me|telegram\.me)$/.test(h)) {
      if (!out.telegram) out.telegram = u.pathname.split('/')[1] || '';
    } else if (/^(vk\.com|vk\.ru|m\.vk\.com)$/.test(h)) {
      if (!out.vk) out.vk = u.pathname.split('/')[1] || '';
    } else if (/^(wa\.me|api\.whatsapp\.com|whatsapp\.com)$/.test(h)) {
      var n = (u.pathname.split('/')[1] || u.searchParams.get('phone') || '').replace(/\D/g, '');
      if (!out.whatsapp && n) out.whatsapp = '+' + n;
    } else if (/(^|\.)(ok\.ru|instagram\.com|youtube\.com|youtu\.be|dzen\.ru|max\.ru|viber\.com|facebook\.com|rutube\.ru)$/.test(h)) {
      /* соцсети, для которых в leadgen нет поля */
    } else if (!out.website && !/(^|\.)(yandex|2gis)\./.test(h)) {
      /* Метки рекламы и счётчиков: без них адрес годится для поиска дублей */
      Array.from(u.searchParams.keys()).forEach(function (k) {
        if (/^(utm_|yclid$|ysclid$|gclid$|fbclid$|_openstat$|from$)/.test(k)) u.searchParams.delete(k);
      });
      out.website = u.origin + (u.pathname === '/' && !u.search ? '' : u.pathname + u.search);
    }
  };

  var host = location.hostname;

  if (/(^|\.)yandex\.[a-z]+$/.test(host)) {
    /* У Яндекса карточка размечена для поисковиков (schema.org
       LocalBusiness) — это надёжнее классов оформления, которые меняются.
       Берём только свойства самой организации, не отзывов внутри неё. */
    var cards = document.querySelectorAll('[itemscope][itemtype*="LocalBusiness"]');
    var card = document.querySelector('.business-card-view[itemscope]') || cards[cards.length - 1];
    if (!card) {
      alert('Откройте карточку организации — нажмите на неё в списке или на карте.');
      return;
    }
    var prop = function (name) {
      return Array.prototype.filter.call(card.querySelectorAll('[itemprop="' + name + '"]'),
        function (e) { return e.closest('[itemscope]') === card; });
    };
    var val = function (e) {
      return e.getAttribute('content') || e.getAttribute('href') || e.textContent;
    };
    out.source = 'yandex';
    out.name = clean(prop('name').map(val)[0]);
    prop('telephone').forEach(function (e) { push(out.phones, val(e)); });
    prop('url').forEach(function (e) { link(val(e)); });
    prop('sameAs').forEach(function (e) { link(val(e)); });
    var addr = card.querySelector('.business-contacts-view__address-link');
    out.address = clean(addr ? addr.textContent : (prop('address').map(val)[0] || '').split('·')[0]);
    var rubrics = [];
    /* Полная карточка и карточка сбоку от поиска размечают рубрики по-разному */
    card.querySelectorAll('.orgpage-categories-info-view__link, .business-categories-view__category')
      .forEach(function (e) { push(rubrics, e.textContent); });
    out.rubric = rubrics.join(', ');
    var og = document.querySelector('meta[property="og:url"]');
    var m = (location.pathname.match(/\/org\/[^/]+\/\d+/) ||
             (og && og.content.match(/\/org\/[^/]+\/\d+/)) || [])[0];
    if (m) out.map_url = 'https://' + host + '/maps' + m + '/';

  } else if (/(^|\.)2gis\.[a-z]+$/.test(host)) {
    /* 2ГИС не размечает карточку для поисковиков. Название — заголовок
       карточки, телефоны и почта — ссылки tel: и mailto:, адрес — ссылка на
       здание. Внешние ссылки 2ГИС пропускает через link.2gis.ru, а настоящий
       адрес лежит в нём в base64. */
    var h1 = document.querySelector('h1');
    if (!h1 || !/\/firm\/\d+/.test(location.pathname)) {
      alert('Откройте карточку организации — нажмите на неё в списке или на карте.');
      return;
    }
    var box = h1;
    for (var i = 0; i < 14 && box.parentElement; i++) {
      box = box.parentElement;
      if (box.querySelector('a[href^="tel:"]') && box.textContent.length > 1500) break;
    }
    out.source = '2gis';
    out.name = clean(h1.textContent);

    /* Сначала номера из блока контактов (в тексте ссылки видны цифры),
       потом кнопки вроде «Записаться» — за ними бывает номер для рекламы */
    var tels = Array.prototype.slice.call(box.querySelectorAll('a[href^="tel:"]'));
    tels.sort(function (a, b) { return /\d/.test(b.textContent) - /\d/.test(a.textContent); });
    tels.forEach(function (a) { push(out.phones, a.getAttribute('href').slice(4)); });

    var mail = box.querySelector('a[href^="mailto:"]');
    if (mail) out.email = mail.getAttribute('href').slice(7).split('?')[0];
    var geo = box.querySelector('a[href*="/geo/"]');
    if (geo) out.address = clean(geo.textContent);

    box.querySelectorAll('a[href]').forEach(function (a) {
      var href = a.href;
      var enc = href.match(/link\.2gis\.[a-z]+\/[^/]+\/[^/]+\/([^?#]+)/);
      if (enc) {
        var b = enc[1].replace(/-/g, '+').replace(/_/g, '/');
        b += '===='.slice(0, (4 - b.length % 4) % 4);
        try {
          var bytes = Uint8Array.from(atob(b), function (c) { return c.charCodeAt(0); });
          href = new TextDecoder().decode(bytes).split(/\s/)[0];
        } catch (e) { return; }
      }
      if (/^https?:/.test(href) && !/(^|\.)2gis\.(ru|com|kz)\//.test(href.replace(/^https?:\/\//, ''))) {
        link(href);
      }
    });

    /* Рубрика — в заголовке страницы: «Название, описание на карте,
       Рубрика, Город — 2ГИС» */
    var t = document.title.replace(/\s+[—-]\s+2ГИС\s*$/, '');
    var at = t.indexOf(' на карте');
    if (at > 0) {
      var desc = clean(t.slice(0, at).slice(out.name.length).replace(/^,/, ''));
      var tail = t.slice(at + ' на карте'.length).replace(/^,\s*/, '').split(', ');
      tail.pop();
      out.rubric = [tail.join(', '), desc].filter(Boolean).join('; ');
    }
    var f = location.pathname.match(/^\/([^/]+)\/.*?firm\/(\d+)/);
    if (f) out.map_url = 'https://' + host + '/' + f[1] + '/firm/' + f[2];

  } else {
    alert('Кнопка работает на карточке организации в Яндекс Картах или 2ГИС.');
    return;
  }

  if (!out.name) {
    alert('Не нашёл название организации. Откройте её карточку и нажмите ещё раз.');
    return;
  }
  var w = window.open(ORIGIN + '/#add=' + encodeURIComponent(JSON.stringify(out)), '_blank');
  if (!w) alert('Браузер не дал открыть вкладку. Разрешите всплывающие окна для этого сайта.');
})
