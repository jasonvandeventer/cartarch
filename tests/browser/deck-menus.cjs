// Runs against the disposable, seeded image started by scripts/check_image.sh.
const assert = require('node:assert/strict');
const { chromium, firefox } = require('playwright');
const base = process.env.SMOKE_URL || 'http://127.0.0.1:5581';
(async () => {
  for (const [name, engine] of Object.entries({chromium, firefox})) {
    const browser = await engine.launch({firefoxUserPrefs: {'ui.primaryPointerCapabilities':6,'ui.allPointerCapabilities':6}});
    try {
      for (const width of [1400, 390]) {
        const page = await browser.newPage({viewport: {width, height: 1000}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        // The image has no external network. Browser images are deterministic too.
        await page.route('**/*', route => route.request().url().startsWith(base)
          ? route.continue() : route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="140"/>'}));
        await page.goto(base + '/login');
        await page.locator('[name=username]').fill('smoke@example.invalid');
        await page.locator('[name=password]').fill('local-smoke-password');
        await Promise.all([page.waitForURL(base + '/'), page.getByRole('button', {name:'Sign in', exact:true}).click()]);
        for (const view of ['grid', 'list']) {
          await page.goto(base + '/decks/1');
          await Promise.all([page.waitForNavigation(), page.locator(`.deck-view-toggle button[value="${view}"]`).click()]);
          if (view === 'list') {
            const sizes = await page.locator('.deck-list-row .dlr-switch-btn').evaluateAll(buttons => buttons.map(b => ({width:b.getBoundingClientRect().width,height:b.getBoundingClientRect().height})));
            assert.equal(sizes.length, 100);
            assert(sizes.every(r => r.width > 12 && r.height > 12), `${name}: multicol controls must retain usable dimensions`);
          }
          const selector = 'details[hx-get="/decks/1/rows/1/actions"]';
          const menu = page.locator(selector);
          assert.equal(await menu.count(), 1);
          assert.equal(await page.locator('.deck-actions-slot form').count(), 0, 'Menus must start unloaded');
          let requests = 0;
          const counter = request => { if (request.url() === base + '/decks/1/rows/1/actions') requests++; };
          page.on('request', counter);
          // Fail first request to prove retry remains possible.
          await page.route('**/decks/1/rows/1/actions', async route => {
            await page.unroute('**/decks/1/rows/1/actions');
            await route.fulfill({status:503, body:'Temporary test failure'});
          }, {times:1});
          const failed = page.waitForResponse(r => r.url().endsWith('/rows/1/actions') && r.status() === 503);
          await menu.locator('summary').first().click();
          await failed;
          await menu.getByRole('button', {name:'Load actions', exact:true}).click();
          await menu.locator('.basic-qty-form').waitFor();
          assert.equal(requests, 2);
          // Form ownership and hit-testing prove more than the CSS spelling.
          assert(await menu.locator('.basic-qty-form button').evaluate(button => {
            const r = button.getBoundingClientRect();
            return !!button.form && r.width > 0 && r.height > 0 &&
              r.left >= 0 && r.right <= innerWidth && document.elementFromPoint(r.x + r.width/2, r.y + r.height/2) === button;
          }), `${name}/${width}/${view}: quantity submit must be visible and hit-testable`);
          await menu.locator('summary').first().click();
          await menu.locator('summary').first().click();
          assert.equal(requests, 2, 'Reopening must reuse loaded actions');
          const quantity = menu.locator('.basic-qty-form [name=quantity]');
          const before = Number(await quantity.inputValue());
          await quantity.fill(String(before + 1));
          const changed = page.waitForResponse(r => r.url().endsWith('/rows/1/set-qty') && r.request().method() === 'POST');
          await menu.locator('.basic-qty-form button').click();
          assert.equal((await changed).status(), 200);
          await page.getByText(`${99 + before + 1} Total Cards`, {exact:true}).waitFor();
          assert.equal(await menu.locator('form').count(), 0, 'The swapped row must reload current controls');
          await menu.locator('summary').first().click();
          await menu.locator('.basic-qty-form').waitFor();
          assert.equal(await menu.locator('.basic-qty-form [name=quantity]').inputValue(), String(before + 1));
          assert.equal(requests, 3);
          // Native full-page form submission from the fetched body works too.
          await menu.getByRole('button', {name:'Move to Considering', exact:true}).click();
          await page.waitForURL(/\/decks\/1/);
          await page.locator(selector).waitFor({state:'detached'});
          // Restore through the real route for the next browser/view combination.
          const promote = page.locator('form[action="/decks/1/considering/1/promote"]');
          if (await promote.count()) {
            await Promise.all([page.waitForNavigation(), promote.locator('button[type=submit]').click()]);
          } else {
            throw Error('Considering promotion form not found');
          }
          page.off('request', counter);
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'Page must not spill horizontally');
          console.log(`PASS ${name} ${width}px ${view}: lazy load, retry, reuse, CSRF mutation, OOB totals, reopened controls, full-page form`);
        }
        assert.deepEqual(errors, []);
        await page.close();
      }
    } finally { await browser.close(); }
  }
})().catch(error => {console.error(error); process.exit(1);});
