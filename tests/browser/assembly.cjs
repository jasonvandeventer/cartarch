// Real authenticated confirmations, including alternate copies and a replay.
const assert = require('node:assert/strict');
const {chromium, firefox} = require('playwright');
const base = process.env.SMOKE_URL || 'http://127.0.0.1:5581';
(async () => {
  let pulled = 0;
  for (const [name, engine] of Object.entries({chromium, firefox})) {
    const browser = await engine.launch();
    try {
      for (const width of [390, 1400]) {
        const page = await browser.newPage({viewport:{width,height:900}});
        const errors = [];
        page.on('pageerror', e => errors.push(e.message));
        await page.route('**/*', route => route.request().url().startsWith(base)
          ? route.continue() : route.fulfill({body:''}));
        await page.goto(base+'/login');
        await page.getByLabel('Email or username',{exact:true}).fill('smoke@example.invalid');
        await page.getByLabel('Password',{exact:true}).fill('local-smoke-password');
        await Promise.all([page.waitForURL(base+'/'), page.getByRole('button',{name:'Sign in',exact:true}).click()]);
        await page.goto(base+'/decks/2');
        await page.getByRole('link',{name:'Assembly checklist',exact:true}).click();
        await page.waitForURL(base+'/decks/2/assemble');
        const url = page.url();
        if (pulled === 0) {
          const csrf = await page.locator('.assembly-pull [name=csrf_token]').first().inputValue();
          const preview = await page.request.post(base+'/import/reconcile-preview', {form:{
            csrf_token:csrf, target_location_id:'3', line_number:'1',
            scryfall_id:'assembly-smoke-0', quantity:'2', finish:'normal',
            set_code:'one', collector_number:'0', location:''
          }});
          assert.equal(preview.status(),200,await preview.text());
          await page.setContent('<form>'+await preview.text()+'</form>');
          await page.getByRole('button',{name:'Plan all for assembly',exact:true}).click();
          assert.equal(await page.locator('[name=reconcile_action]').inputValue(),'plan_assembly');
          assert.equal(await page.locator('[name=reconcile_move_qty]').inputValue(),'0');
          assert.equal(await page.locator('[name=reconcile_new_qty]').inputValue(),'2');
          assert(await page.locator('[name=reconcile_action]').evaluate(e=>!!e.form));
          await page.goto(url);
        }
        for (let step=0; step<2; step++) {
          const form = page.locator('.assembly-pull').first();
          await form.waitFor({state:'visible'});
          const options = await form.locator('select option').allTextContents();
          assert(options.some(s=>s.includes('ja')), 'alternate language must be visible');
          if (step === 0) {
            await form.locator('select').selectOption({index:1});
            assert((await form.locator('.assembly-copy').textContent()).includes('ja'));
          }
          await form.getByRole('spinbutton').fill('1');
          assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
          const bounds = await form.getByRole('button').boundingBox();
          assert(bounds.width>=44 && bounds.height>=30);
          assert(await form.getByRole('button').evaluate(e=>e.form===e.closest('form')));
          const data = await form.evaluate(e=>Object.fromEntries(new FormData(e)));
          if (pulled === 0) {
            const rejected = await page.request.post(url,{form:{...data,csrf_token:''}});
            assert.equal(rejected.status(),403);
          }
          await Promise.all([page.waitForNavigation(),form.getByRole('button',{name:'Confirm pull'}).click()]);
          pulled++;
          await page.getByText(`${pulled} in deck`,{exact:true}).waitFor();
          assert(await page.locator('p[role=status]').isVisible());
          const replay = await page.request.post(url,{form:data});
          assert((await replay.text()).includes('That pull could not be confirmed'));
          await page.reload();
          await page.getByText(`${pulled} in deck`,{exact:true}).waitFor();
          if (pulled === 8) {
            await page.getByText('No placeholders left to fill.',{exact:true}).waitFor();
            assert.equal(await page.locator('.assembly-pull').count(),0);
          }
        }
        if (process.env.ASSEMBLY_SCREENSHOTS) await page.screenshot({path:`${process.env.ASSEMBLY_SCREENSHOTS}/${name}-${width}.png`,fullPage:true});
        assert.deepEqual(errors,[]);
        console.log(`PASS assembly ${name} ${width}px: alternate copy, confirm, replay, resume (${pulled}/8)`);
        await page.close();
      }
    } finally {await browser.close();}
  }
})().catch(e=>{console.error(e);process.exit(1)});
