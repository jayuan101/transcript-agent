const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto('http://127.0.0.1:7860', { waitUntil: 'load', timeout: 15000 });
  await page.waitForTimeout(2000);

  // Screenshot 1: top of page (already done as run_screenshot.png)

  // Screenshot 2: scroll down to see full page
  await page.evaluate(() => window.scrollTo(0, 500));
  await page.waitForTimeout(800);
  await page.screenshot({ path: 'C:/Users/young/Documents/DEMO/Transcript/run_s2_mid.png' });

  // Screenshot 3: open AI Provider dropdown
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(500);
  const providerDropdown = page.locator('text=Claude (Anthropic)').first();
  await providerDropdown.click();
  await page.waitForTimeout(800);
  await page.screenshot({ path: 'C:/Users/young/Documents/DEMO/Transcript/run_s3_provider.png' });
  await page.keyboard.press('Escape');

  // Screenshot 4: switch Dark mode
  await page.waitForTimeout(500);
  const darkBtn = page.locator('#ta-btn-dark').first();
  await darkBtn.click({ force: true });
  await page.waitForTimeout(1000);
  await page.screenshot({ path: 'C:/Users/young/Documents/DEMO/Transcript/run_s4_dark.png' });

  await browser.close();
  console.log('All screenshots saved');
})();
