/**
 * Визуальная проверка опубликованного дашборда настоящим браузером.
 * Запускается в GitHub Actions (в песочнице разработки github.io недоступен).
 *
 * Проверяет на каждом вьюпорте и каждой странице:
 *  - горизонтальное переполнение страницы;
 *  - элементы, вылезающие за свой контейнер;
 *  - обрезанный текст;
 *  - слишком мелкий шрифт;
 *  - налезающие вкладки;
 *  - графики шире контейнера;
 *  - пустые карточки.
 * Скриншоты сохраняются артефактом.
 */
const { chromium } = require('playwright');
const fs = require('fs');

const URL = process.env.QA_URL || 'https://n85t594ht5-creator.github.io/polylab/';
const VIEWPORTS = [320, 375, 390, 430, 600, 1000, 1440];
const PAGES = ['home','health','coverage','dna','signals','arena','perf','backtest','latency','moves','book','corr','forward','storage'];

(async () => {
  const browser = await chromium.launch();
  const issues = [];
  const checked = [];
  fs.mkdirSync('qa-screens', { recursive: true });

  for (const w of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: w, height: 900 }, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    page.on('pageerror', e => issues.push({ vw: w, page: 'load', kind: 'JS_ERROR', detail: String(e).slice(0, 160) }));
    await page.goto(URL, { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForTimeout(1200);

    for (const p of PAGES) {
      await page.evaluate(k => { if (window.go) window.go(k); }, p);
      await page.waitForTimeout(350);

      // Самопроверка: без неё "0 проблем" ничего не значит — можно 13 раз
      // проверить одну и ту же страницу и не заметить этого.
      const shown = await page.evaluate(() => {
        const v = document.querySelector('.view:not([hidden])');
        return v ? { id: v.id, cards: v.querySelectorAll('.card').length,
                     text: (v.textContent || '').trim().length } : null;
      });
      if (!shown || shown.id !== 'v_' + p)
        issues.push({ vw: w, page: p, kind: 'PAGE_NOT_SWITCHED',
                      detail: `ожидалась v_${p}, показана ${shown ? shown.id : 'нет видимой'}` });
      else if (shown.cards === 0)
        issues.push({ vw: w, page: p, kind: 'NO_CARDS', detail: 'на странице нет карточек' });
      else if (shown.text < 40)
        issues.push({ vw: w, page: p, kind: 'PAGE_ALMOST_EMPTY', detail: `${shown.text} символов` });
      checked.push(`${p}@${w}:${shown ? shown.id : '-'}:${shown ? shown.cards : 0}`);

      const found = await page.evaluate((ctxInfo) => {
        const out = [];
        const de = document.documentElement;
        if (de.scrollWidth > de.clientWidth + 1)
          out.push({ kind: 'PAGE_OVERFLOW', detail: `${de.scrollWidth} > ${de.clientWidth}` });

        const view = document.querySelector('.view:not([hidden])');
        if (!view) return [{ kind: 'NO_VISIBLE_VIEW', detail: ctxInfo }];

        // элементы шире своего контейнера (кроме тех, что сами прокручиваются)
        view.querySelectorAll('.card, table, svg, .kpi, .row, .filters').forEach(el => {
          const par = el.parentElement;
          if (!par) return;
          const scrollable = el.closest('.tw') || getComputedStyle(par).overflowX === 'auto';
          if (!scrollable && el.getBoundingClientRect().width > par.getBoundingClientRect().width + 2)
            out.push({ kind: 'ELEMENT_OVERFLOW', detail: `${el.className || el.tagName} шире родителя` });
        });

        // обрезанный текст без многоточия
        view.querySelectorAll('.n, .big, .k, .v, th, td').forEach(el => {
          const cs = getComputedStyle(el);
          if (el.scrollWidth > el.clientWidth + 2 && cs.textOverflow !== 'ellipsis' && cs.overflow !== 'hidden')
            out.push({ kind: 'TEXT_CLIPPED', detail: (el.textContent || '').slice(0, 30) });
        });

        // слишком мелкий текст
        view.querySelectorAll('*').forEach(el => {
          if (!el.textContent || !el.textContent.trim()) return;
          const fs2 = parseFloat(getComputedStyle(el).fontSize);
          if (fs2 && fs2 < 9.5) out.push({ kind: 'TINY_TEXT', detail: `${fs2}px: ${(el.textContent||'').slice(0,24)}` });
        });

        // svg шире контейнера
        view.querySelectorAll('svg').forEach(s => {
          const par = s.parentElement;
          if (par && s.getBoundingClientRect().width > par.getBoundingClientRect().width + 2)
            out.push({ kind: 'CHART_OVERFLOW', detail: 'svg шире контейнера' });
        });

        // пустая карточка (только заголовок)
        view.querySelectorAll('.card').forEach(c => {
          if (c.hasAttribute('hidden')) return;
          const h = c.querySelector('h2');
          const body = c.textContent.replace(h ? h.textContent : '', '').trim();
          if (!body) out.push({ kind: 'EMPTY_CARD', detail: h ? h.textContent : '?' });
        });

        // блок нулевой/схлопнутой высоты — выглядит как сломанная страница
        view.querySelectorAll('.card, .kpi, .blank, svg').forEach(el => {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height < 8)
            out.push({ kind: 'COLLAPSED_BLOCK', detail: `${el.className || el.tagName} h=${r.height.toFixed(0)}` });
        });

        // график без данных, но и без объяснения
        view.querySelectorAll('svg').forEach(s => {
          if (!s.querySelector('polyline, rect, circle, text'))
            out.push({ kind: 'EMPTY_CHART', detail: 'svg без содержимого' });
        });

        // налезание соседних карточек друг на друга
        const cards = [...view.querySelectorAll('.card')].filter(c => !c.hasAttribute('hidden'));
        for (let i = 0; i < cards.length; i++)
          for (let j = i + 1; j < cards.length; j++) {
            const a = cards[i].getBoundingClientRect(), b = cards[j].getBoundingClientRect();
            const ov = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (ov > 4 && oy > 4)
              out.push({ kind: 'CARDS_OVERLAP', detail: `${i}/${j} пересечение ${ov.toFixed(0)}×${oy.toFixed(0)}` });
          }

        // элементы, вышедшие за пределы окна
        view.querySelectorAll('.card, table, .kpi>div, .row').forEach(el => {
          const r = el.getBoundingClientRect();
          if (r.left < -2 || r.right > window.innerWidth + 2) {
            if (!el.closest('.tw'))
              out.push({ kind: 'OUT_OF_VIEWPORT', detail: `${el.className || el.tagName} ${r.left.toFixed(0)}..${r.right.toFixed(0)}` });
          }
        });

        // текст, залезающий на соседнюю колонку в .row (частый дефект)
        view.querySelectorAll('.row').forEach(r => {
          const kids = [...r.children];
          for (let i = 0; i + 1 < kids.length; i++) {
            const a = kids[i].getBoundingClientRect(), b = kids[i + 1].getBoundingClientRect();
            if (a.right > b.left + 2)
              out.push({ kind: 'ROW_COLUMN_OVERLAP', detail: (kids[i].textContent || '').slice(0, 20) });
          }
        });

        // пустое состояние обязано объяснять причину
        view.querySelectorAll('.blank').forEach(b => {
          if ((b.textContent || '').trim().length < 25)
            out.push({ kind: 'BLANK_WITHOUT_REASON', detail: (b.textContent || '').slice(0, 30) });
        });
        return out;
      }, `${p}@${w}`);

      found.forEach(f => issues.push({ vw: w, page: p, ...f }));
      if (w === 320 || w === 375 || w === 1000)
        await page.screenshot({ path: `qa-screens/${p}-${w}.png`, fullPage: true });
    }

    // проверка навигации на узких экранах
    const navOverflow = await page.evaluate(() => {
      const n = document.querySelector('nav');
      return n ? { scroll: n.scrollWidth, client: n.clientWidth,
                   scrollable: getComputedStyle(n).overflowX === 'auto' } : null;
    });
    if (navOverflow && navOverflow.scroll > navOverflow.client && !navOverflow.scrollable)
      issues.push({ vw: w, page: 'nav', kind: 'NAV_OVERFLOW', detail: JSON.stringify(navOverflow) });

    await ctx.close();
  }
  await browser.close();

  const byKind = {};
  issues.forEach(i => { byKind[i.kind] = (byKind[i.kind] || 0) + 1; });
  fs.writeFileSync('research/VISUAL_QA.json', JSON.stringify(
    { url: URL, checked_at: new Date().toISOString(), viewports: VIEWPORTS, pages: PAGES,
      checks_performed: checked.length, sample_checks: checked.slice(0, 15),
      total_issues: issues.length, by_kind: byKind, issues: issues.slice(0, 200) }, null, 1));

  console.log(`вьюпортов: ${VIEWPORTS.length}, страниц: ${PAGES.length}, проверок: ${checked.length}, проблем: ${issues.length}`);
  console.log('образцы проверок:', checked.slice(0, 8).join(' | '));
  Object.entries(byKind).forEach(([k, v]) => console.log(`  ${k}: ${v}`));
  issues.slice(0, 40).forEach(i => console.log(`  ${i.vw}px ${i.page}: ${i.kind} — ${i.detail}`));
  process.exit(0);
})();
