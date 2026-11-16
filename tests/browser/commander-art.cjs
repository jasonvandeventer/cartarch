const assert = require('node:assert/strict');
const {chromium, firefox} = require('playwright');
(async () => {
  const browser = await ({chromium, firefox}[process.env.BROWSER]).launch();
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const images = [];
    await page.route('https://**/*', route => {
      if (route.request().resourceType() === 'image') {
        images.push(route.request().url());
        return route.fulfill({contentType:'image/svg+xml', body:'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="450"><rect width="600" height="450" fill="teal"/></svg>'});
      }
      return route.abort();
    });
    for (const status of ['created', 'in_progress']) {
      images.length = 0;
      await page.goto(process.env.AUDIT_BROWSER_URL + `/commander-${status}.html`);
      await page.waitForFunction(() => {
        const el = document.querySelector('[data-background-art]');
        return el?.style.getPropertyValue('--cmp-art').includes('version=art_crop');
      });
      assert(images.some(url => url.includes('version=art_crop')));
      assert(!images.some(url => url.includes('/normal.') || url.includes('version=normal')));
    }
    console.log(`${process.env.BROWSER}: phone commander backgrounds load art crops`);
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
