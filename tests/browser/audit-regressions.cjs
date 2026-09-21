/* Run after AUDIT_BROWSER_FIXTURES=/tmp/cartarch-audit-browser pytest -q
 * tests/test_audit_regressions.py::test_printing_modal_renders_hover_fallback.
 * Serve that directory with a static/ symlink to app/static.
 * PLAYWRIGHT_MODULE and CHROMIUM_EXECUTABLE may select an installed runtime.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../..');
const base = process.env.AUDIT_BROWSER_URL || 'http://127.0.0.1:5581';
const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="red"/></svg>';
function extract(file, name) {
  const source = fs.readFileSync(path.join(root, file), 'utf8');
  const start = source.indexOf('function ' + name + '(');
  assert(start >= 0, name);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i++) {
    if (source[i] === '{') depth++;
    if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw Error(name);
}
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE});
  let checks = 0;
  try {
    async function page(width = 1280, touch = false) {
      const p = await browser.newPage({viewport:{width, height:900}, hasTouch:touch, isMobile:touch});
      p.setDefaultTimeout(5000);
      await p.route('**/*', r => {
        const url = r.request().url();
        if (url.startsWith(base)) return r.continue();
        if (r.request().resourceType() !== 'image') return r.fulfill({body:''});
        if (p.url().endsWith('/deck.html') && !url.includes('/back/')) return r.fulfill({contentType:'image/svg+xml', body:svg});
        if (url.includes('api.scryfall.com')) return r.fulfill({contentType:'image/svg+xml', body:svg});
        return r.abort(); // Every mirror misses, including responsive srcset candidates.
      });
      return p;
    }
    for (const width of [1280, 390]) {
      const p = await page(width);
      await p.goto(base + '/deck.html');
      const button = p.locator('[data-card-flip]').first();
      const media = button.locator('..');
      const img = media.locator('img');
      assert(await img.getAttribute('srcset'), 'Fixture must retain responsive front candidates');
      await button.click();
      await p.waitForFunction(() => {
        const b = document.querySelector('[data-card-flip]');
        const image = b.parentElement.querySelector('img');
        return image.naturalWidth > 0 && image.currentSrc === b.dataset.backAlt;
      });
      assert.equal(await img.getAttribute('srcset'), null);
      await button.click();
      await p.waitForFunction(() => {
        const b = document.querySelector('[data-card-flip]');
        const image = b.parentElement.querySelector('img');
        return image.naturalWidth > 0 && image.currentSrc === b.dataset.front;
      });
      checks += 2;
      // The actual route-rendered modal must feed the hover engine's contract.
      await p.goto(base + '/printing.html');
      await p.evaluate(() => {
        document.body.dataset.cardHover = '';
        document.body.dataset.cardHoverTarget = '.switch-printing-row';
      });
      await p.addScriptTag({path:path.join(root, 'app/static/card-hover.js')});
      await p.locator('.switch-printing-row').first().dispatchEvent('mouseover', {clientX:50,clientY:50});
      await p.waitForFunction(() => [...document.images].some(i => i.naturalWidth > 0 && i.src.includes('api.scryfall.com')));
      checks++;
      await p.close();
    }
    for (const [file, fn, id, cls] of [
      ['game_detail.html','initMomirHoverZoom','momir-hover-zoom','tc-momir-token'],
      ['game_companion.html','initCmpZoom','cmp-hover-zoom','cmp-token']
    ]) {
      for (const touch of [false, true]) {
        const p = await page(touch ? 390 : 1280, touch);
        await p.setContent(`<img id="${id}"><div class="${cls}" data-sid="one">One</div><div class="${cls}" data-sid="two">Two</div>`);
        await p.addScriptTag({content:"const MIRROR_LARGE='https://audit.test/missing/__SID__';\n" + extract('app/templates/'+file, fn) + `;${fn}();`});
        for (const sid of ['one', 'two', 'one']) {
          if (touch) {
            await p.locator(`.${cls}[data-sid=${sid}]`).dispatchEvent('touchstart', {touches:[{identifier:1,clientX:50,clientY:50}]});
          } else {
            await p.locator(`.${cls}[data-sid=${sid}]`).dispatchEvent('mouseover');
          }
          await p.waitForFunction(({id,sid}) => {
            const img = document.getElementById(id);
            return img.naturalWidth > 0 && img.src.includes('api.scryfall.com/cards/'+sid);
          }, {id,sid});
          if (touch) await p.locator(`.${cls}[data-sid=${sid}]`).dispatchEvent('touchend');
          checks++;
        }
        await p.close();
      }
    }
    for (const companion of [false,true]) {
      const p = await page(companion ? 390 : 1280);
      const ids = companion ? ['cmp-planechase','cmp-archenemy'] : ['planechase-controls','planechase-center','archenemy-controls','archenemy-center'];
      await p.setContent(ids.map(id => `<div id="${id}"></div>`).join(''));
      const planeFn = companion ? 'renderCompanionPlanechase' : 'renderPlanechase';
      const schemeFn = companion ? 'renderCompanionArchenemy' : 'renderArchenemy';
      const file = 'app/templates/' + (companion ? 'game_companion.html' : 'game_detail.html');
      await p.addScriptTag({content:`const MIRROR_LARGE='https://audit.test/missing/__SID__';const TABLE_TOKEN='test';const seatDefs=[];const escHtml=String;const MY_SEAT_ID=1;let PC_HIDDEN=false;const state={planechase:true,currentPlane:{scryfall_id:'plane',name:'Plane'},archenemy:true,schemeCurrent:{scryfall_id:'current',name:'Current'},ongoingSchemes:[{scryfall_id:'ongoing',name:'Ongoing'}]};const st=state;` + extract(file,planeFn) + extract(file,schemeFn) + `;window.renderVariants=()=>{${planeFn}();${schemeFn}();};renderVariants();`});
      await p.waitForFunction(() => document.images.length === 3 && [...document.images].every(i => i.naturalWidth > 0));
      await p.evaluate(() => {
        const images = [...document.images];
        for (let i=0;i<10;i++) {state.lives={1:40-i};renderVariants();}
        if (!images.every((image,i) => image === document.images[i] && image.naturalWidth > 0)) throw Error('Recovered images recreated on unchanged variant state');
        state.currentPlane={scryfall_id:'next',name:'Next'};renderVariants();
        if (images[0] === document.images[0]) throw Error('Changed plane did not render');
      });
      await p.waitForFunction(() => [...document.images].every(i => i.naturalWidth > 0));
      checks += 4;
      await p.close();
    }
    console.log(`${checks} browser assertions passed (desktop and mobile; forced mirror failures, including repeat selections).`);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
