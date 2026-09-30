const assert = require('node:assert/strict');
const {chromium, firefox} = require('playwright');
const base = process.env.SMOKE_URL || 'http://127.0.0.1:5581';
(async () => {
  for (const [name, engine] of Object.entries({chromium, firefox})) {
    const browser = await engine.launch();
    try {
      for (const width of [390,1400]) {
        const page = await browser.newPage({viewport:{width,height:1000}});
        async function openSaved() {
          if (!await page.locator('.collection-sidebar-details').evaluate(e=>e.open))
            await page.locator('.collection-sidebar-summary').click();
          if (!await page.locator('.collection-saved-views').evaluate(e=>e.open))
            await page.locator('.collection-saved-views > summary').click();
        }
        const errors=[];
        page.on('pageerror',e=>errors.push(e.message));
        await page.route('**/*',r=>r.request().url().startsWith(base)?r.continue():r.fulfill({body:''}));
        await page.goto(base+'/login');
        await page.getByLabel('Email or username',{exact:true}).fill('smoke@example.invalid');
        await page.getByLabel('Password',{exact:true}).fill('local-smoke-password');
        await Promise.all([page.waitForURL(base+'/'),page.getByRole('button',{name:'Sign in',exact:true}).click()]);
        await page.goto(base+'/collection?search=Test+Artifact+001&sort=name&direction=asc&view=rows');
        await openSaved();
        await page.getByLabel('View name',{exact:true}).fill('My saved filter');
        await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Save current view',exact:true}).click()]);
        await page.getByText('View saved.',{exact:true}).waitFor();
        await page.goto(base+'/collection');
        await openSaved();
        await page.getByRole('link',{name:'My saved filter',exact:true}).click();
        await page.waitForURL(u=>u.pathname==='/collection' && u.searchParams.get('search')==='Test Artifact 001');
        assert.equal(new URL(page.url()).searchParams.get('view'),'rows');
        await openSaved();
        await page.getByText('Manage My saved filter',{exact:true}).click();
        await page.getByLabel('Name',{exact:true}).fill('Renamed filter');
        await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Rename',exact:true}).click()]);
        await page.getByRole('link',{name:'Renamed filter',exact:true}).waitFor();
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        const savedBox=await page.locator('.collection-saved-views').boundingBox();
        assert(savedBox.x>=0 && savedBox.x+savedBox.width<=width,`${name}/${width}: saved views extend beyond viewport`);
        if(process.env.FEATURE_SCREENSHOTS) await page.locator('.collection-saved-views').screenshot({path:`${process.env.FEATURE_SCREENSHOTS}/saved-${name}-${width}.png`});
        await page.getByText('Manage Renamed filter',{exact:true}).click();
        await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'Delete saved view',exact:true}).click()]);
        assert.equal(await page.getByRole('link',{name:'Renamed filter',exact:true}).count(),0);
        for(const view of ['grid','list']) {
          await page.goto(base+'/decks/3');
          await Promise.all([page.waitForNavigation(),page.locator(`.deck-view-toggle button[value="${view}"]`).click()]);
          const controls = page.locator('.shared-card-controls');
          await controls.locator('summary').click();
          const body=controls.locator('.collection-row-kebab-body');
          await body.waitFor({state:'visible'});
          if(view==='list') await page.waitForFunction(()=>document.querySelector('.shared-card-controls .collection-row-kebab-body').style.left!=='');
          const box=await body.boundingBox();
          assert(box.x>=0 && box.x+box.width<=width+1,`${name}/${width}/${view}: ${JSON.stringify(box)}`);
          assert.equal(await controls.getByRole('link',{name:'Open source deck'}).getAttribute('href'),'/decks/1');
          const rowId=await controls.locator('[name=inventory_row_id]').inputValue();
          const csrf=await controls.locator('[name=csrf_token]').inputValue();
          if(process.env.FEATURE_SCREENSHOTS) await page.screenshot({path:`${process.env.FEATURE_SCREENSHOTS}/shared-${name}-${width}-${view}.png`});
          await Promise.all([page.waitForNavigation(),controls.getByRole('button',{name:'Remove from this list',exact:true}).click()]);
          await page.getByRole('status').getByText('Open source deck',{exact:true}).waitFor();
          assert.equal(await page.locator('.shared-card-controls').count(),0);
          await page.getByRole('status').getByRole('link',{name:'Open source deck'}).click();
          await page.waitForURL(base+'/decks/1');
          await page.getByText('Test Artifact 099',{exact:true}).first().waitFor();
          const restore=await page.request.post(base+'/decks/1/share-card',{form:{csrf_token:csrf,inventory_row_id:rowId,target_deck_id:'3'}});
          assert.equal(restore.status(),200);
        }
        assert.deepEqual(errors,[]);
        console.log(`PASS saved/shared ${name} ${width}px: save, apply, rename, delete; both shared layouts, source link, unshare feedback, physical copy retained`);
        await page.close();
      }
    } finally {await browser.close();}
  }
})().catch(e=>{console.error(e);process.exit(1)});
