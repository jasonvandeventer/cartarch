const assert = require('node:assert/strict');
const {chromium, firefox} = require('playwright');
const browserName = process.env.BROWSER || 'chromium';
const base = process.env.AUDIT_BROWSER_URL || 'http://127.0.0.1:5582';
(async () => {
  const browser = await ({chromium, firefox}[browserName]).launch({headless: true});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', route => route.request().url().startsWith(base)
      ? route.continue() : route.fulfill({body: ''}));
    for (const width of [1400, 390]) {
      await page.setViewportSize({width, height: 900});
      await page.goto(base + '/ratings.html');
      assert.equal(await page.locator('#player-ratings tbody tr').count(), 4);
      assert.match(await page.locator('#player-ratings').innerText(), /1790/);
      assert.equal(await page.locator('input[name="players"]:checked').count(), 4);
      assert.equal(await page.locator('input[name="selection"]').inputValue(), '1');
      await page.locator('select[name="game_format"]').selectOption('Commander');
      const geometry = await page.evaluate(() => {
        const table = document.querySelector('#player-ratings table').getBoundingClientRect();
        const button = document.querySelector('form.stack-form button[type="submit"]');
        const r = button.getBoundingClientRect();
        return {spill: document.documentElement.scrollWidth > innerWidth, tableWidth: table.width, buttonWidth: r.width};
      });
      assert.equal(geometry.spill, false, `${browserName} ${width}: horizontal overflow`);
      assert(geometry.tableWidth > 0 && geometry.buttonWidth > 0);
      if (process.env.RATING_SCREENSHOT_DIR) {
        await page.screenshot({path: `${process.env.RATING_SCREENSHOT_DIR}/ratings-${browserName}-${width}.png`, fullPage: true});
      }
      await page.goto(base + '/record.html');
      const record = page.locator('.playgroup-record-table');
      for (const [name, score] of [['Patryk', '1790'], ['Mason', '1504'], ['Jason V', '1206']]) {
        const row = record.locator('tbody tr').filter({hasText: name});
        assert.equal((await row.locator('[data-label="Rating"]').innerText()).trim(), score);
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.RATING_SCREENSHOT_DIR) {
        await record.screenshot({path: `${process.env.RATING_SCREENSHOT_DIR}/record-${browserName}-${width}.png`});
      }
      await page.goto(base + '/deck-ratings.html');
      assert.equal(await page.locator('#deck-ratings tbody tr').count(), 2);
      assert.match(await page.locator('#deck-ratings').innerText(), /1832/);
      assert.equal(await page.locator('select[name="rank_by"]').inputValue(), 'decks');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.RATING_SCREENSHOT_DIR) {
        await page.screenshot({path: `${process.env.RATING_SCREENSHOT_DIR}/deck-ratings-${browserName}-${width}.png`, fullPage: true});
      }
      await page.locator('select[name="rank_by"]').selectOption('players');
      await page.getByRole('button', {name: 'Update standings', exact: true}).click();
      await page.waitForURL(/rank_by=players/);
      assert.equal(new URL(page.url()).searchParams.getAll('players').length, 4);
    }
    assert.deepEqual(errors, []);
    console.log(`${browserName}: ratings render at 1400/390px; filters and standings fit.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
