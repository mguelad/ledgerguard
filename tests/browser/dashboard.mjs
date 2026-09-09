import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { chromium, expect } from '@playwright/test';

const baseURL = process.env.LG_BROWSER_URL || 'http://localhost:8000';
const username = process.env.LG_BROWSER_USERNAME || 'browser_demo';
const password = process.env.LG_BROWSER_PASSWORD;
if (!password) throw new Error('Set LG_BROWSER_PASSWORD to the local synthetic demo password.');
const output = process.env.LG_BROWSER_OUTPUT || 'test-results/browser';
await mkdir(output, { recursive: true });

const browser = await chromium.launch();
try {
  for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ baseURL, viewport });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const capture = async name => {
      await expect(page.locator('main h1')).toBeVisible();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.screenshot({ path: `${output}/${viewport.width}-${name}.png`, fullPage: true });
    };

    await page.goto('/login/');
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
    await capture('login');
    await page.getByLabel('Username').fill(username);
    await page.getByLabel('Password').fill(password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Your organizations', exact: true })).toBeVisible();
    await page.getByRole('link', { name: /Open workspace/ }).click();
    await page.waitForURL(/\/o\/[a-f0-9-]+\/$/);
    const overview = page.url();
    await expect(page.getByRole('heading', { name: 'Northwind Commerce', exact: true })).toBeVisible();
    await capture('overview');

    await page.locator('a.store-card').filter({ hasText: 'Northwind Supply' }).click();
    await page.waitForURL(/\/stores\/[a-f0-9-]+\/$/);
    await capture('store');
    await page.locator('a[href^="/findings/"]').first().click();
    await page.waitForURL(/\/findings\/[a-f0-9-]+\/$/);
    await expect(page.getByRole('heading', { name: 'Source comparison', exact: true })).toBeVisible();
    await expect(page.locator('table').first()).toContainText('Stripe');
    await capture('finding');

    if (viewport.width === 1440) {
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
        page.getByRole('button', { name: 'Acknowledge', exact: true }).click(),
      ]);
      await expect(page.locator('.metric').filter({ hasText: 'State' }).locator('strong')).toHaveText('Acknowledged');
    }

    await page.goto(overview + 'settings/');
    await capture('settings');
    if (viewport.width === 1440) {
      await page.getByLabel('Evidence retention').selectOption('180');
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
        page.getByRole('button', { name: 'Save organization policy', exact: true }).click(),
      ]);
      await expect(page.getByLabel('Evidence retention')).toHaveValue('180');
    }
    assert.deepEqual(errors, [], 'The dashboard raised a browser JavaScript error.');
    await context.close();
  }
  console.log('Desktop and mobile dashboard journeys passed.');
} finally {
  await browser.close();
}
