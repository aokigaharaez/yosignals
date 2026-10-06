const {chromium} = require('C:/Users/super/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const path = require('node:path');
function initData(id) {
  const pairs = {auth_date:String(Math.floor(Date.now()/1000)), user:JSON.stringify({id,first_name:'UI Test'})};
  const secret = crypto.createHmac('sha256','WebAppData').update('123456:TEST_TOKEN_NOT_REAL').digest();
  pairs.hash=crypto.createHmac('sha256',secret).update(Object.entries(pairs).sort().map(([k,v])=>k+'='+v).join('\n')).digest('hex');
  return new URLSearchParams(pairs).toString();
}
async function userPage(browser,id) {
  const context=await browser.newContext({viewport:{width:1440,height:1050}});
  await context.route('https://telegram.org/js/telegram-web-app.js',route=>route.fulfill({
    contentType:'application/javascript',
    body:'window.Telegram={WebApp:{initData:'+JSON.stringify(initData(id))+',ready(){},expand(){},setHeaderColor(){},setBackgroundColor(){}}};'
  }));
  const page=await context.newPage();
  return {page,context};
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const errors=[];
  try {
    const {page}=await userPage(browser,42);
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8766',{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#environment').textContent.includes('v2.1'));
    await page.waitForFunction(()=>/CALL|PUT/.test(document.querySelector('#signal-direction').textContent),{},{timeout:45000});
    const response=page.waitForResponse(r=>r.url().endsWith('/api/analyses')&&r.request().method()==='POST');
    await page.locator('#analyze-button').click();
    const saved=await (await response).json();
    assert.ok(saved.id && saved.probability && saved.candles.length);
    await page.locator('.signal-feed-card').first().waitFor();
    await page.locator('#forecast-quality summary').click();
    await page.screenshot({path:path.join(__dirname,'miniapp-v2.1-desktop.png'),fullPage:true});
    await page.locator('[data-page="access"]').click();
    await page.locator('.owner-row').first().waitFor();
    await page.locator('#owner-id').fill('10001');
    await page.locator('#owner-add').click();
    const added=page.locator('.owner-row').filter({hasText:'10001'});
    await added.waitFor();
    assert.equal(await page.locator('.owner-row').filter({hasText:'42 · вы'}).locator('button').count(),0);
    const second=await userPage(browser,10001);
    await second.page.goto('http://127.0.0.1:8766');
    await second.page.waitForFunction(()=>document.querySelector('#environment').textContent.includes('v2.1'));
    await page.screenshot({path:path.join(__dirname,'miniapp-v2.1-access.png'),fullPage:true});
    await added.locator('button').click();
    await added.waitFor({state:'detached'});
    await second.page.reload();
    await second.page.waitForFunction(()=>document.querySelector('#chart-empty-text').textContent.includes('Нет доступа'));
    await second.context.close();
    await page.locator('[data-page="overview"]').first().click();
    const watchResponse=page.waitForResponse(r=>r.url().endsWith('/api/watch')&&r.request().method()==='PUT');
    await page.locator('#watch-toggle').click();
    assert.equal((await (await watchResponse).json()).enabled,true);
    await page.locator('#watch-toggle').click();
    await page.waitForFunction(()=>document.querySelector('#watch-toggle').getAttribute('aria-checked')==='false');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(__dirname,'miniapp-v2.1-mobile.png'),fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.ok(await page.evaluate(()=>document.querySelector('.signal-panel').getBoundingClientRect().top<document.querySelector('.market-panel').getBoundingClientRect().top));
    await page.locator('[data-page="access"]').click();
    await page.screenshot({path:path.join(__dirname,'miniapp-v2.1-access-mobile.png'),fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,checks:['real-market-analysis','saved-signal-feed','grant-access','second-owner-login','revoke-existing-session','miniapp-autoscan','mobile-layout'],signal:{direction:saved.direction,quality:saved.quality,chance:saved.probability.value,method:saved.probability.method,provider:saved.provider},browserErrors:errors}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
