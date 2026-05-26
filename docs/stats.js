// GoatCounter visitor counter inline-строка для подвала.
// Подтягивает три счётчика (сегодня / 30 дней / всего) и рендерит их
// одной строкой на языке локали страницы. Локаль берётся из
// <html lang="...">. Если fetch не прошёл — оставляем плейсхолдеры "—".
(function () {
  var SITE = "https://zohar-translator.goatcounter.com";

  // Шаблоны на 13 языков. Плейсхолдеры {today}, {month}, {total}
  // подменяются на числа в формате локали. Слово после {total} —
  // существительное "просмотров/views/..." в нужной форме.
  var T = {
    ru: "Сегодня {today} · за 30 дней {month} · всего {total} просмотров",
    en: "Today {today} · 30 days {month} · total {total} views",
    zh: "今日 {today} · 30 天 {month} · 总计 {total} 次浏览",
    es: "Hoy {today} · 30 días {month} · total {total} vistas",
    pt: "Hoje {today} · 30 dias {month} · total {total} visualizações",
    ja: "今日 {today} · 30 日 {month} · 累計 {total} ビュー",
    de: "Heute {today} · 30 Tage {month} · gesamt {total} Aufrufe",
    ko: "오늘 {today} · 30일 {month} · 누적 {total} 조회",
    fr: "Aujourd'hui {today} · 30 jours {month} · total {total} vues",
    it: "Oggi {today} · 30 giorni {month} · totale {total} visualizzazioni",
    tr: "Bugün {today} · 30 gün {month} · toplam {total} görüntüleme",
    lt: "Šiandien {today} · 30 dienų {month} · iš viso {total} peržiūrų",
    he: "היום {today} · 30 ימים {month} · סך הכול {total} צפיות"
  };

  var lang = (document.documentElement.lang || "en").slice(0, 2).toLowerCase();
  var tpl = T[lang] || T.en;
  var el = document.querySelector(".stats-inline");
  if (!el) return;

  function iso(d) { return d.toISOString().slice(0, 10); }
  var now = new Date();
  var today = iso(now);
  var monthAgo = iso(new Date(now.getTime() - 30 * 86400000));

  // Числовой формат подбирается по lang страницы — для he/ru/de/fr
  // получаются разные разделители разрядов.
  function fmt(n) {
    try { return Number(n).toLocaleString(lang); }
    catch (_) { return Number(n).toLocaleString("en"); }
  }
  function span(v) { return '<span class="num">' + v + "</span>"; }

  // Один fetch на каждый счётчик; рендерим только когда все три пришли,
  // чтобы строка не дёргалась "—" → число → "—" → число.
  function get(url) {
    return fetch(url, { credentials: "omit" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  var base = SITE + "/counter/TOTAL.json";
  Promise.all([
    get(base + "?start=" + today),
    get(base + "?start=" + monthAgo),
    get(base)
  ]).then(function (results) {
    var vals = results.map(function (d) {
      return d && d.count != null ? span(fmt(d.count)) : "—";
    });
    el.innerHTML = tpl
      .replace("{today}", vals[0])
      .replace("{month}", vals[1])
      .replace("{total}", vals[2]);
  });
})();
