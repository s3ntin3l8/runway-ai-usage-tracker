import { test, expect } from '@playwright/test';

const local = 'http://127.0.0.1:18765';
const network = 'http://127.0.0.1:18766';
const sidecar = 'http://127.0.0.1:18767';
const attacker = 'http://127.0.0.1:18768';
const admin = { 'X-Admin-Key': 'synthetic-browser-admin' };

test('hostile browser origin cannot mutate local dashboard state', async ({ page, request }) => {
  const before = await (await request.get(`${local}/api/v1/system/dashboard-layout`)).json();
  await page.goto(attacker);
  await page.evaluate(async (url) => {
    try { await fetch(`${url}/api/v1/system/dashboard-layout`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider_order: ['attack'] }) }); } catch { /* browser rejection is expected */ }
  }, local);
  const after = await (await request.get(`${local}/api/v1/system/dashboard-layout`)).json();
  expect(after).toEqual(before);
  // Exercise the server boundary too, independently of browser preflight policy.
  expect((await request.put(`${local}/api/v1/system/dashboard-layout`, { headers: { Origin: attacker }, data: {} })).status()).toBe(403);
});

test('sidecar settings omit the key and reject hostile simple POSTs', async ({ page, request }) => {
  await page.goto(sidecar);
  expect(await page.content()).not.toContain('synthetic-fleet-key');
  const before = await (await request.get(`${attacker}/state`)).json();
  await page.goto(attacker);
  await page.evaluate(async (url) => {
    try { await fetch(`${url}/save`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'api_url=https%3A%2F%2Fattacker.test&api_key=changed' }); } catch { /* unreadable response */ }
  }, sidecar);
  expect(await (await request.get(`${attacker}/state`)).json()).toEqual(before);
  // Legitimate same-origin use still works.
  await page.goto(sidecar);
  expect(await page.evaluate(async () => (await fetch('/save', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'api_url=http%3A%2F%2Flocalhost%3A8765&api_key=' })).status)).toBe(200);
});

test('loopback Host boundary rejects rebinding names', async ({ request }) => {
  for (const base of [local, sidecar]) {
    expect((await request.get(base, { headers: { Host: 'attacker.test' } })).status()).toBe(403);
  }
});

test('network private reads and shared mutations require credentials', async ({ page, request }) => {
  await page.goto(`${network}/api/v1/system/health`);
  expect(await page.evaluate(async () => (await fetch('/api/v1/system/dashboard-layout')).status)).toBe(403);
  expect((await request.get(`${network}/api/v1/system/dashboard-layout`, { headers: admin })).status()).toBe(200);
  expect((await request.put(`${network}/api/v1/system/dashboard-layout`, { data: {} })).status()).toBe(403);
  expect((await request.put(`${network}/api/v1/system/dashboard-layout`, { headers: admin, data: {} })).status()).toBe(200);
});

test('remote Vite cannot inherit backend loopback trust', async ({ request }) => {
  const url = 'http://127.0.0.1:18769/api/v1/system/dashboard-layout';
  expect((await request.get(url, { headers: { 'X-Runway-Dev-Remote': '0' } })).status()).toBe(403);
  expect((await request.get(url, { headers: admin })).status()).toBe(200);
});
