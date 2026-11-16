const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium, firefox} = require('playwright');
(async () => {
  for (const [name, engine] of Object.entries({chromium, firefox})) {
    const browser = await engine.launch();
    try {
      const page = await browser.newPage();
      page.setDefaultTimeout(10000);
      await page.route('**/*', route => route.abort());
      await page.setContent(fs.readFileSync(process.argv[2], 'utf8'));
      const mason = await page.locator('#user-sel-1 option').filter({hasText: /^Mason$/}).getAttribute('value');
      const patryk = await page.locator('#user-sel-1 option').filter({hasText: /^Patryk$/}).getAttribute('value');
      await page.selectOption('#user-sel-1', mason);
      assert.equal(await page.inputValue('#name-1'), 'Mason');
      await page.selectOption('#user-sel-1', patryk);
      assert.equal(await page.inputValue('#name-1'), 'Patryk');
      await page.selectOption('#user-sel-2', mason);
      const deck = await page.locator('#deck-sel-1 option').filter({hasText: /^Test Deck$/}).getAttribute('value');
      await page.selectOption('#deck-sel-1', deck);
      await page.fill('#cmd-2', 'The Jolly Balloon Man');
      // The real playgroup change listener rebuilds every seat row.
      await page.locator('select[name=playgroup_id]').dispatchEvent('change');
      assert.equal(await page.inputValue('#name-1'), 'Patryk');
      assert.equal(await page.inputValue('#name-2'), 'Mason');
      assert.equal(await page.inputValue('#deck-sel-1'), deck);
      assert.equal(await page.inputValue('#cmd-2'), 'The Jolly Balloon Man');
      // Browser-restored form values can arrive without input/change events.
      await page.locator('#user-sel-2').evaluate((el, id) => { el.value = id; }, patryk);
      await page.locator('#name-2').evaluate(el => { el.value = 'Patryk'; });
      await page.locator('select[name=playgroup_id]').dispatchEvent('change');
      assert.equal(await page.inputValue('#user-sel-2'), patryk);
      assert.equal(await page.inputValue('#name-2'), 'Patryk');
      await page.fill('#name-1', 'Custom pilot');
      await page.selectOption('#user-sel-1', mason);
      assert.equal(await page.inputValue('#name-1'), 'Custom pilot');
      await page.locator('select[name=playgroup_id]').dispatchEvent('change');
      const data = await page.locator('#name-1').evaluate(el => {
        const form = new FormData(el.form);
        return {names:form.getAll('player_names'), users:form.getAll('user_ids')};
      });
      assert.equal(data.names[1], 'Custom pilot');
      assert.equal(data.users[1], mason);
      assert.equal(data.names[2], 'Patryk');
      assert.equal(data.users[2], patryk);
      console.log(`${name}: game setup names and selections survive redraws`);
    } finally { await browser.close(); }
  }
})().catch(error => {console.error(error); process.exitCode=1;});
