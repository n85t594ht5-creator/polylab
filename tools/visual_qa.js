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
const PAGES = ['home','health','coverage','dna','signals','arena','perf','latency','moves','book','corr','forward','storage'];

(async () => {
  const browser = await chromium.launch();
  const issues = [];
  fs.mkdirSync('qa-screens', { recursive: true });

  for (const w of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: w, height: 900 }, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    page.on('pageerror', e => issues.push({ vw: w, page: 'load', kind: 'JS_ERROR', detail: String(e).slice(0, 160) }));
    await page.goto(URL, { waitUntil: 'networkidle', timeout: 60000 });
    await page.waitForTimeout(1200);

    for (const p of PAGES) {
      await page.evaluate(k => window.go && window.go(k), p);
      await page.waitForTimeout(350);

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
        return out;
      }, `${p}@${w}`);

      found.forEach(f => issues.push({ vw: w, page: p, ...f }));
      if (w === 375 || w === 1000)
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
      total_issues: issues.length, by_kind: byKind, issues: issues.slice(0, 200) }, null, 1));

  console.log(`вьюпортов: ${VIEWPORTS.length}, страниц: ${PAGES.length}, проблем: ${issues.length}`);
  Object.entries(byKind).forEach(([k, v]) => console.log(`  ${k}: ${v}`));
  issues.slice(0, 40).forEach(i => console.log(`  ${i.vw}px ${i.page}: ${i.kind} — ${i.detail}`));
  process.exit(0);
})();
