/* Run via core.test_partial_navigation: all mutations use the isolated test DB. */
const {chromium} = require(process.env.AIZY_PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.AIZY_CHROMIUM, args:['--no-sandbox']});
  const context = await browser.newContext();
  const base = process.env.AIZY_TEST_URL;
  const sessionCookie = process.env.AIZY_TEST_SESSION_COOKIE || 'sessionid';
  await context.addCookies([{name:sessionCookie, value:process.env.AIZY_TEST_SESSION, url:base}]);
  const page = await context.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type()==='error' && /Refused to (display|frame)/i.test(message.text())) errors.push(message.text());
  });
  const waitIdle = () => page.waitForFunction(() => !document.documentElement.classList.contains('is-navigating'));
  const click = async selector => { await page.locator(selector).click(); await waitIdle(); };
  const openMenu = async () => { await click('#home-menu-link'); await page.waitForSelector('.home-menu-grid'); };
  // Supporting destinations stay reachable through native, keyboard-operable
  // disclosure blocks, without competing with the five direct actions.
  for (const width of [1440, 390, 360]) {
    await page.setViewportSize({width, height:1000});
    await page.goto(base + '/tableau-de-bord/');
    await page.waitForFunction(() => !!window.Aizy);
    assert.equal(await page.locator('.group-daily .home-menu-link').count(), 4);
    assert.equal(await page.locator('.group-documents .home-menu-link').count(), 1);
    const folds = page.locator('.home-menu-fold');
    assert.equal(await folds.count(), 1);
    assert.equal(await page.locator('.home-menu-fold[open]').count(), 0);
    assert.equal(await page.locator('.group-reference .home-menu-link').first().isVisible(), false);
    for (const [section, background] of [
      ['price_review','rgb(255, 223, 206)'], ['reconciliation_queue','rgb(217, 237, 255)'],
      ['delivery_issue_review','rgb(255, 234, 177)'], ['supplier_claims','rgb(247, 219, 234)'],
    ]) {
      assert.equal(await page.locator('.menu-' + section + ' .home-task-count').evaluate(el => getComputedStyle(el).backgroundColor), background);
    }
    assert.equal(await page.locator('.profile-connection .profile-session-icon').isVisible(), true);
    assert.equal(await page.locator('.profile-connection').evaluate(el => el.nextElementSibling.tagName), 'FORM');
    assert.equal(await page.locator('.home-fold-chevron').count(), 0);
    assert.match(await page.locator('link[href*="/css/interface.css"]').getAttribute('href'), /v=home-pastel-green-trim-1/);
    assert.equal(await page.locator('.home-primary-link .home-card-heading').count(), 5);
    assert.equal(await page.locator('.home-primary-link').first().evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(255, 255, 255)');
    assert.equal(await page.locator('.home-card-heading strong').first().evaluate(el => getComputedStyle(el).color), 'rgb(23, 108, 98)');
    if (process.env.AIZY_TEST_SCREENSHOT_DIR) {
      const fs = require('node:fs');
      fs.mkdirSync(process.env.AIZY_TEST_SCREENSHOT_DIR, {recursive:true});
      await page.screenshot({path:process.env.AIZY_TEST_SCREENSHOT_DIR + '/admin-' + width + '.png', fullPage:true});
    }
    await folds.locator('summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await folds.evaluate(el => el.open), true);
    for (const group of ['reference','history','admin']) {
      assert.equal(await page.locator('.group-' + group + ' .home-menu-link').first().isVisible(), true);
    }
    assert.equal(await page.locator('.group-admin a[href="/utilisateurs/"]').isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1), false);
    if (process.env.AIZY_TEST_SCREENSHOT_DIR && width===1440) {
      await page.screenshot({path:process.env.AIZY_TEST_SCREENSHOT_DIR + '/admin-expanded.png', fullPage:true});
    }
    await click('.group-reference a[href="/catalogues/"]');
    assert.equal(new URL(page.url()).pathname, '/catalogues/');
    // Reproduce an open page holding the previous stylesheet address.
    // Partial navigation must load the version declared by the fresh HTML.
    await page.evaluate(() => new Promise(resolve => {
      const stylesheet = document.querySelector('link[href*="/css/interface.css"]');
      stylesheet.addEventListener('load', resolve, {once:true});
      stylesheet.href = stylesheet.href.split('?')[0] + '?v=previous-interface';
    }));
    await openMenu();
    assert.match(await page.locator('link[href*="/css/interface.css"]').getAttribute('href'), /v=home-pastel-green-trim-1/);
    assert.equal(await page.locator('.home-menu-fold[open]').count(), 0);
  }
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height:900});
    await page.goto(base + '/revue-prix/');
    await page.waitForFunction(() => !!window.Aizy);
    assert.equal(await page.locator('.app-page-heading h1').textContent(),'Votre revue des prix');
    await click('.proof-panel summary');
    const invoiceFrame = page.locator('iframe.review-document');
    assert.equal(await invoiceFrame.isVisible(), true);
    const invoiceResponse = await context.request.get(base + await invoiceFrame.getAttribute('src'));
    assert.equal(invoiceResponse.status(), 200);
    assert.equal(invoiceResponse.headers()['x-frame-options'], 'SAMEORIGIN');
    assert.match(invoiceResponse.headers()['content-type'], /application\/pdf/);
    await page.keyboard.press('Escape');
    await page.evaluate(() => { window.navigationMarker = 'retained'; });
    await click('.mobile-brand');
    assert.equal(await page.locator('.app-page-heading').isVisible(),false);
    assert.ok(await page.locator('.home-menu-grid').count());
    assert.equal(await page.locator('#mobile-menu').count(),0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1),false);
    await click('.home-menu a[href="/revue-prix/"]');
    assert.equal(await page.locator('.app-page-heading h1').textContent(),'Votre revue des prix');
    assert.equal(await page.evaluate(() => window.navigationMarker),'retained');
    const first = await page.locator('[data-review-line]').getAttribute('data-review-line');
    const filterTop = await page.locator('.review-filterbar').evaluate(el=>el.getBoundingClientRect().top);
    const jumpBottom = await page.locator('.review-jump').evaluate(el=>el.getBoundingClientRect().bottom);
    assert.ok(filterTop >= jumpBottom, 'Filters must sit below the jump form');
    assert.equal(await page.locator('.review-price').first().evaluate(el=>getComputedStyle(el).borderTopWidth),'2px');
    assert.equal(await page.locator('.review-price.expected strong').evaluate(el=>getComputedStyle(el).color),'rgb(56, 156, 22)');
    // Move forward, back and with keyboard: the original JS context survives.
    if (width === 1440) {
      const next = page.locator('.review-peek.next');
      const initialTransform = await next.evaluate(el=>getComputedStyle(el).transform);
      await next.hover();
      await page.waitForFunction(initial=>getComputedStyle(document.querySelector('.review-peek.next')).transform!==initial,initialTransform);
      await page.waitForTimeout(250);
      assert.equal(await next.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(237, 245, 242)');
      await page.mouse.move(0,0);
    }
    await click('.review-progress a[href*="position=2"]');
    assert.notEqual(await page.locator('[data-review-line]').getAttribute('data-review-line'), first);
    await page.goBack(); await waitIdle();
    assert.equal(await page.locator('[data-review-line]').getAttribute('data-review-line'), first);
    await page.locator('h2').first().focus(); await page.keyboard.press('ArrowRight'); await waitIdle();
    assert.notEqual(await page.locator('[data-review-line]').getAttribute('data-review-line'), first);
    await click('.review-progress a[href*="position=1"]');
    assert.equal(await page.evaluate(() => window.navigationMarker), 'retained');
    // A filter GET and direct jump also stay in the current document.
    await page.locator('.review-jump input[name="position"]').fill('2');
    await click('.review-jump button');
    assert.notEqual(await page.locator('[data-review-line]').getAttribute('data-review-line'), first);
    await click('.review-progress a[href*="position=1"]');
    // Price editing uses the same button, kept in place, without an initial POST.
    let pricePosts = 0;
    const watchPrice = request => { if (request.method() === 'POST' && request.url().includes('/action/')) pricePosts++; };
    page.on('request', watchPrice);
    const priceButton = page.locator('[data-price-confirm]');
    const before = await priceButton.evaluate(el => {window.originalPriceButton=el;return el.getBoundingClientRect().top+scrollY;});
    await click('[data-price-confirm]');
    assert.equal(pricePosts,0);
    assert.equal(await priceButton.textContent(),'Modifier le tarif');
    assert.equal(await priceButton.evaluate(el=>el===window.originalPriceButton),true);
    const after = await priceButton.evaluate(el=>el.getBoundingClientRect().top+scrollY);
    assert.ok(Math.abs(after-before)<2, 'The original button must stay in place');
    assert.equal(await page.locator('[data-price-fields]').isVisible(),true);
    await page.locator('input[name="price"]').fill('-1');
    await click('[data-price-confirm]'); assert.equal(pricePosts,0);
    await click('[data-price-cancel]');
    assert.equal(await priceButton.textContent(),'Modifier le tarif');
    await click('[data-price-confirm]');
    assert.equal(await page.locator('input[name="price"]').inputValue(),'8.84');
    await page.locator('[data-price-inline] input[name="fingerprint"]').evaluate(el=>{el.value='stale';});
    await click('[data-price-confirm]');
    assert.match(await page.locator('.app-messages').textContent(),/changé/);
    assert.match(await page.locator('#navigation-notice').textContent(),/changé/);
    assert.equal(await page.locator('#navigation-notice').isVisible(),true);
    assert.equal(await priceButton.textContent(),'Modifier le tarif');
    assert.equal(await page.locator('[data-price-fields]').isVisible(),true);
    assert.equal(await page.locator('input[name="price"]').inputValue(),'8.84');
    pricePosts=0;
    if (process.env.AIZY_SCREENSHOT_DIR) await page.screenshot({path:process.env.AIZY_SCREENSHOT_DIR+'/inline-price-'+width+'.png',fullPage:true});
    await click('[data-price-confirm]'); assert.equal(pricePosts,1);
    await click('form[action="/revue-prix/annuler/"] button');
    // Missing products follow the identical in-place creation interaction.
    await click('.review-progress a[href*="position=3"]');
    assert.equal(await priceButton.textContent(),'Créer le produit');
    await click('[data-price-confirm]');
    assert.equal(await priceButton.textContent(),'Créer le produit');
    assert.equal(pricePosts,1);
    await click('[data-price-confirm]'); assert.equal(pricePosts,2);
    await click('form[action="/revue-prix/annuler/"] button');
    await click('.review-progress a[href*="position=1"]');
    page.off('request',watchPrice);
    // Network failure must keep inputs and must never auto-retry a POST.
    await click('.defer-panel summary');
    await page.locator('textarea[name="reason"]').fill('Vérification réseau');
    let failedPosts = 0;
    const failPost = async route => { if (route.request().method() === 'POST') { failedPosts++; await route.abort(); } else await route.continue(); };
    await page.route('**/revue-prix/lignes/*/action/', failPost);
    await click('.defer-panel button');
    assert.equal(failedPosts, 1);
    assert.match(await page.locator('#navigation-notice').textContent(), /Connexion interrompue/);
    assert.equal(await page.locator('textarea[name="reason"]').inputValue(), 'Vérification réseau');
    assert.equal(await page.locator('.defer-panel button').isEnabled(), true);
    await page.unroute('**/revue-prix/lignes/*/action/', failPost);
    // Double click results in exactly one real POST, then undo without reloading.
    let posts = 0;
    const delayPost = async route => { if (route.request().method() === 'POST') { posts++; await new Promise(r=>setTimeout(r,180)); } await route.continue(); };
    await page.route('**/revue-prix/lignes/*/action/', delayPost);
    await page.locator('.defer-panel button').evaluate(button => { button.click(); button.click(); });
    await waitIdle(); assert.equal(posts, 1);
    await page.unroute('**/revue-prix/lignes/*/action/', delayPost);
    assert.notEqual(await page.locator('[data-review-line]').getAttribute('data-review-line'), first);
    await click('form[action="/revue-prix/annuler/"] button');
    assert.equal(await page.locator('[data-review-line]').getAttribute('data-review-line'), first);
    // Business validation error remains on the card with an open panel.
    await click('.defer-panel summary');
    await page.locator('textarea[name="reason"]').fill('   ');
    await click('.defer-panel button');
    assert.match(await page.locator('.app-messages').textContent(), /motif/);
    assert.equal(await page.locator('.defer-panel').evaluate(el=>el.open),true);
    // A real draft claim and undo exercise amount/CSRF/fingerprint handling.
    await click('.claim-panel button');
    assert.match(await page.locator('.app-messages').textContent(), /réclamation|brouillon/i);
    assert.match(await page.locator('#navigation-notice').textContent(), /réclamation|brouillon/i);
    await click('form[action="/revue-prix/annuler/"] button');
    assert.equal(await page.evaluate(() => window.navigationMarker), 'retained');
    // Page-specific behavior works after switching sections (no upload is sent).
    await click('.claim-panel button');
    await openMenu(); await click('.home-menu a[href="/reclamations/"]');
    assert.equal(await page.locator('.app-page-heading h1').textContent(),'Réclamations');
    assert.equal(await page.locator('.claim-item.review-card').count(),1);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1),false);
    assert.ok(await page.locator('.review-progress a').count());
    if (await page.locator('.review-progress a[href="?position=2"]').count()) {
      await click('.review-progress a[href="?position=2"]');
      await click('.review-progress a[href="?position=1"]');
      assert.equal(await page.evaluate(() => window.navigationMarker),'retained');
    }
    if (process.env.AIZY_SCREENSHOT_DIR) await page.screenshot({path:process.env.AIZY_SCREENSHOT_DIR+'/claim-cards-'+width+'.png',fullPage:true});
    const cancellable = page.locator('.claim-item').filter({has:page.locator('.claim-withdraw')});
    await cancellable.locator('.claim-withdraw summary').click();
    await click('.claim-withdraw button');
    assert.match(await page.locator('.app-messages').textContent(), /Réclamation annulée/);
    assert.equal(await page.locator('.claim-withdraw').count(),0);
    assert.equal(await page.locator('.claim-delete').count(),0);
    await openMenu(); await click('.home-menu a[href="/revue-prix/"]');
    assert.equal(await page.locator('[data-review-line]').getAttribute('data-review-line'),first);
    assert.equal(await page.evaluate(() => window.navigationMarker), 'retained');
    await openMenu(); await click('.home-menu a[href="/documents/deposer/"]');
    assert.equal(await page.locator('label[for="deposit-camera"]').isVisible(),true);
    assert.equal(await page.locator('label[for="id_files"]').isVisible(),true);
    for (const [selector, inputId] of [['label[for="deposit-camera"]','deposit-camera'],['label[for="id_files"]','id_files']]) {
      const chooserEvent = page.waitForEvent('filechooser');
      await page.locator(selector).click();
      const chooser = await chooserEvent;
      assert.equal(await chooser.element().getAttribute('id'),inputId);
    }
    await page.locator('#id_files').setInputFiles({name:'test.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=','base64')});
    assert.equal(await page.locator('[data-selection-count]').textContent(), '1 document prêt');
    await page.locator('#id_fournisseur').selectOption({index:1});
    if (await page.locator('select#id_restaurant').count()) await page.locator('#id_restaurant').selectOption({index:1});
    let releaseUpload;
    const uploadGate = new Promise(resolve => { releaseUpload = resolve; });
    const upload = async route => {
      if (route.request().method() !== 'POST') return route.continue();
      await uploadGate;
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({received:1,supplier:'Bianchi'})});
    };
    await page.route('**/documents/deposer/', upload);
    await page.locator('[data-deposit-submit]').click();
    await page.waitForSelector('[data-deposit-busy="true"]');
    await page.goBack();
    assert.equal(await page.locator('[data-deposit-busy="true"]').count(),1);
    releaseUpload();
    await page.waitForSelector('.home-menu-grid'); await waitIdle();
    await page.unroute('**/documents/deposer/', upload);
    await click('.home-menu a[href="/revue-prix/"]');
    assert.equal(await page.evaluate(() => window.navigationMarker), 'retained');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1), false);
    // A server refusal leaves the current interface intact and gives a clear error.
    await page.route('**/revue-prix/?**', route => route.fulfill({status:403,body:'Forbidden'}));
    await click('.review-progress a[href*="position=2"]');
    assert.match(await page.locator('#navigation-notice').textContent(), /Accès refusé/);
    await page.unroute('**/revue-prix/?**');
  }
  // A dynamic BL feedback form must post to its declared endpoint. Its
  // `name="action"` buttons intentionally exercise native form-property clobbering.
  await page.goto(base + '/documents/deposer/?feedback=' + process.env.AIZY_TEST_DELIVERY);
  await page.waitForSelector('[data-delivery-feedback-form]');
  const feedbackForm = page.locator('[data-delivery-feedback-form]');
  const feedbackPath = `/documents/bons-livraison/${process.env.AIZY_TEST_DELIVERY}/commentaires/`;
  assert.equal(await feedbackForm.getAttribute('action'), feedbackPath);
  await page.locator('.delivery-product-select').click();
  await page.locator('[data-feedback-comment] textarea').fill('Produit abîmé pendant la livraison.');
  assert.equal(await page.locator('[data-feedback-save]').isEnabled(), true);
  const feedbackPostPromise = page.waitForRequest(request => request.method() === 'POST');
  await page.locator('[data-feedback-save]').click();
  const feedbackPost = await feedbackPostPromise;
  assert.equal(new URL(feedbackPost.url()).pathname, feedbackPath);
  const feedbackResponse = await feedbackPost.response();
  assert.equal(feedbackResponse.status(), 200);
  assert.match(feedbackResponse.headers()['content-type'] || '', /application\/json/);
  await page.waitForFunction(() => document.querySelector('[data-receipt-title]')?.textContent.includes('Commentaires enregistrés'));
  assert.match(await page.locator('[data-feedback-status]').textContent(), /problème.*transmis/i);
  // Confirming the received defect adds it to a supplier draft; its BL and
  // comments remain visible in both the review history and downloadable letter.
  await openMenu(); await click('.menu-delivery_issue_review');
  const tabCount = context.pages().length;
  await click('.delivery-issue-proof summary');
  const deliveryPhoto = page.locator('.delivery-issue-proof img.review-document');
  await deliveryPhoto.waitFor({state:'visible'});
  await page.waitForFunction(() => {
    const photo = document.querySelector('.delivery-issue-proof img');
    return photo.complete && photo.naturalWidth > 0;
  });
  assert.equal(await deliveryPhoto.evaluate(el => !!el.closest('.review-card')), true);
  assert.equal(context.pages().length, tabCount);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+1), false);
  await click('.delivery-issue-proof summary');
  assert.equal(await deliveryPhoto.isVisible(), false);
  await page.locator('#delivery-review-note').fill('Remplacement demandé au fournisseur.');
  await click('.delivery-issue-actions button[value="confirm"]');
  assert.match(await page.locator('.app-messages').textContent(), /ajouté au brouillon/);
  await click('.delivery-issue-tabs a[href*="state=history"]');
  await click('.delivery-issue-links a[href*="/reclamations/"]');
  await page.locator('.claim-card-body > details').first().locator('summary').click();
  const defect = page.locator('.claim-line').filter({hasText:'Produit livré'});
  assert.equal(await defect.count(), 1);
  assert.match(await defect.textContent(), /BL-BROWSER/);
  assert.match(await defect.textContent(), /Produit abîmé pendant la livraison/);
  assert.match(await defect.textContent(), /Remplacement demandé au fournisseur/);
  assert.match(await defect.textContent(), /À déterminer/);
  await click('.claim-card-body footer .action-button');
  const letterLink = page.locator('.claim-card-body footer a[href*="/document/"]');
  const letter = await context.request.get(base + await letterLink.getAttribute('href'));
  assert.equal(letter.status(), 200);
  assert.match(await letter.text(), /BL-BROWSER/);
  assert.match(await letter.text(), /Produit abîmé pendant la livraison/);
  const preview = page.frameLocator('.claim-document-preview');
  await preview.locator('h1').waitFor();
  assert.match(await preview.locator('body').textContent(), /BL-BROWSER/);
  assert.match(await page.locator('a[href^="mailto:"]').getAttribute('href'), /^mailto:reclamations.bianchi@example.invalid\?/);
  const mailUrl = new URL(await page.locator('a[href^="mailto:"]').getAttribute('href'));
  assert.match(mailUrl.searchParams.get('body'), /Produit abîmé pendant la livraison/);
  assert.equal(await page.locator('a[href*="/mail/"]').count(), 0);
  assert.equal(await page.locator('.claim-mail-options').textContent().then(text => /pièce jointe|\.eml/.test(text)), false);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  // With JavaScript disabled, server forms and links still work normally.
  const native = await browser.newContext({javaScriptEnabled:false});
  await native.addCookies([{name:sessionCookie,value:process.env.AIZY_TEST_SESSION,url:base}]);
  const fallback = await native.newPage(); await fallback.goto(base + '/revue-prix/');
  assert.equal(await fallback.locator('[data-price-fields]').isVisible(),true);
  assert.equal(await fallback.locator('[data-price-confirm]').textContent(),'Modifier le tarif');
  await fallback.locator('.review-peek.next').click(); assert.match(fallback.url(), /position=2/);
  await native.close();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({viewports:[1440,390],errors,checks:['inline-price','inline-product-creation','navigation','history','keyboard','filter','offline','double-click','post','undo','validation','claim','page-initialization','upload-history','403','delivery-feedback','no-js']}));
  await browser.close();
})().catch(error => {console.error(error); process.exit(1);});
