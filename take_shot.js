const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto('http://127.0.0.1:7860', { waitUntil: 'load', timeout: 15000 });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: 'C:/Users/young/Documents/DEMO/Transcript/run_screenshot.png', fullPage: false });
  await browser.close();
  console.log('Screenshot saved');
})();
