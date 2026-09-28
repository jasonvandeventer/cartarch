// Local/CI regression checks for the usability walkthrough.
const assert = require('node:assert/strict');
const { chromium, firefox } = require('playwright');
const base = process.env.SMOKE_URL || 'http://127.0.0.1:5581';
(async () => {
  for (const [name, engine] of Object.entries({chromium, firefox})) {
    const browser = await engine.launch();
    try {
      for (const width of [390, 1400]) {
        const page = await browser.newPage({viewport:{width,height:900}});
        const errors=[];
        page.on('pageerror', e=>errors.push(e.message));
        await page.route('**/*', route => route.request().url().startsWith(base)
          ? route.continue() : route.fulfill({contentType:'image/svg+xml', body:'<svg xmlns="http://www.w3.org/2000/svg"/>'}));
        await page.goto(base + '/login');
        await page.getByLabel('Email or username', {exact:true}).fill('smoke@example.invalid');
        await page.getByLabel('Password', {exact:true}).fill('local-smoke-password');
        await Promise.all([page.waitForURL(base+'/'),page.getByRole('button',{name:'Sign in',exact:true}).click()]);
        await page.goto(base+'/collection');
        const field = page.locator('.collection-search-input');
        const height=await field.evaluate(e=>e.getBoundingClientRect().height);
        assert(height>=30 && height<70, `search height ${height}`);
        assert.equal(await page.locator('.collection-sidebar-details').evaluate(e=>e.open), width>980);
        const cardTop=await page.locator('main article').first().evaluate(e=>e.getBoundingClientRect().top+scrollY);
        assert(cardTop<1400, `Cards buried at ${cardTop}`);
        await page.locator('.collection-sidebar-summary').click();
        if(width===390) assert(await page.getByRole('button',{name:'Apply filters',exact:true}).isVisible());
        await field.fill('NothingMatchesThis');
        await Promise.all([page.waitForNavigation(),field.press('Enter')]);
        assert.equal(await page.getByText('Welcome to Cartarch',{exact:true}).count(),0);
        await page.getByText('No matching inventory rows for the current filters.',{exact:true}).waitFor();
        await page.getByRole('link',{name:'Clear all',exact:true}).click();
        await page.locator('main article').first().waitFor();
        await page.goto(base+'/import');
        assert.equal(await page.locator('.import-method[open]').count(),0);
        await page.getByText('Paste Card List',{exact:true}).click();
        await page.getByRole('textbox',{name:'Card list',exact:true}).fill('2 Forest');
        await page.getByText('Search by Name',{exact:true}).click();
        assert.equal(await page.locator('.import-method[open]').count(),1);
        await page.getByRole('textbox',{name:'Card name',exact:true}).fill('Forest');
        await page.getByText('Paste Card List',{exact:true}).click();
        assert.equal(await page.getByRole('textbox',{name:'Card list',exact:true}).inputValue(),'2 Forest');
        await page.getByText('Advanced: exact printing',{exact:true}).click();
        for(const label of ['Scryfall ID','Set Code','Collector Number','Finish','Language','Quantity'])
          assert.equal(await page.getByLabel(label,{exact:true}).count(),1);
        assert(await page.getByRole('button',{name:'Preview',exact:true}).evaluate(e=>!!e.form));
        await page.goto(base+'/decks/1');
        assert.equal(await page.locator('.deck-review-details[open]').count(),0);
        const controlsTop=await page.getByRole('tablist',{name:'Deck search'}).evaluate(e=>e.getBoundingClientRect().top+scrollY);
        assert(controlsTop<1400,`Deck controls buried at ${controlsTop}`);
        assert.equal(await page.getByText('Bello',{exact:false}).count(),0);
        await page.getByText('Analysis and deck health',{exact:true}).click();
        assert(await page.getByRole('heading',{name:'§ Deck Health',exact:true}).isVisible());
        await page.getByText('Analysis and deck health',{exact:true}).click();
        await page.getByText('Piloting notes',{exact:true}).click();
        await page.locator('.pp-editor-details > summary').click();
        await page.locator('.pp-editor [name=primary_plan]').fill('Local review plan');
        await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Save profile',exact:true}).click()]);
        assert.equal(await page.locator('#deck-piloting').evaluate(e=>e.open),true);
        await page.getByText('Local review plan',{exact:true}).first().waitFor();
        await page.getByText('Manage deck',{exact:true}).click();
        assert(await page.getByRole('button',{name:'Delete Deck',exact:true}).evaluate(e=>!!e.form));
        await page.getByRole('button',{name:'Bulk Move',exact:true}).click();
        await page.locator('#bulk-move-modal').waitFor({state:'visible'});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        assert.deepEqual(errors,[]);
        console.log(`PASS usability ${name} ${width}px: search ${height}px, first card ${Math.round(cardTop)}px, deck controls ${Math.round(controlsTop)}px; filters, recovery, import methods, labels, profile save, management`);
        await page.close();
      }
    } finally { await browser.close(); }
  }
})().catch(e=>{console.error(e);process.exit(1)});
