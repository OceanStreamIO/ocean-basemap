// Unlock the MERIT Hydro share with its password, and list its packages.
//
//   node session.js <playwright module> <out dir>
//
// MERIT_HYDRO_PASSWORD comes from the environment (the registration email).
// Writes <out>/manifest.json (name, bytes, link per package of the grids in
// MERIT_GRIDS) and <out>/cookies.txt, the unlocked session in Netscape format.
// The cookies are a credential: run.sh keeps them in a temporary directory and
// hands them to the download Job as a Secret, never to the repository.
//
// A browser rather than plain HTTP: Dropbox unlocks a password-protected link
// with a request whose content id the page's own JavaScript supplies. The
// listing itself is plain: list_shared_link_folder_entries, paged by voucher,
// called here with the page's own form so the fields stay whatever Dropbox
// currently sends.
const { chromium } = require(process.argv[2]);
const fs = require('fs');
const OUT = process.argv[3];
const SHARE = 'https://www.dropbox.com/scl/fo/fw8qrf73zhzmo6p92gbxv/AIlVRfnQVEWOyXEimsK2qKk?rlkey=a0be5opyb2kpkf0ssbvau8y9q&st=t8rtowe9&dl=0';
const VERSION_FOLDER = 'v1.0.1';
const GRIDS = (process.env.MERIT_GRIDS || 'dir upa wth').split(/\s+/).filter(Boolean);
const UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

(async () => {
  if (!process.env.MERIT_HYDRO_PASSWORD) throw new Error('MERIT_HYDRO_PASSWORD is not set');
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const context = await browser.newContext({ userAgent: UA });
  const page = await context.newPage();
  let listForm = null;
  page.on('request', (req) => {
    if (!listForm && req.method() === 'POST'
        && req.url().split('?')[0].endsWith('/list_shared_link_folder_entries')) listForm = req.postData();
  });

  await page.goto(SHARE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const password = page.locator('input[type="password"]');
  await password.waitFor({ timeout: 30000 });
  await password.fill(process.env.MERIT_HYDRO_PASSWORD);
  await password.press('Enter');
  await page.waitForTimeout(8000);
  if (await password.isVisible().catch(() => false)) throw new Error('the password was not accepted');

  const folder = await page.$$eval('a[href*="/scl/fo/"]',
    (as, name) => as.filter((a) => a.textContent.trim() === name).map((a) => a.href), VERSION_FOLDER);
  if (!folder.length) throw new Error(`no ${VERSION_FOLDER} folder in the share`);
  await page.goto(folder[0], { waitUntil: 'domcontentloaded', timeout: 60000 });
  for (let i = 0; i < 20 && !listForm; i++) { await page.mouse.wheel(0, 2000); await page.waitForTimeout(1000); }
  if (!listForm) throw new Error('the page never asked for the folder listing');

  // From the first page: without a voucher the listing starts at the top.
  const listing = await page.evaluate(async (form) => {
    const params = new URLSearchParams(form);
    params.delete('voucher');
    const entries = [];
    for (let i = 0; i < 100; i++) {
      const r = await fetch('/list_shared_link_folder_entries', {
        method: 'POST', body: params, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
      const d = await r.json();
      for (const e of d.entries || []) entries.push({ name: e.filename, bytes: e.bytes, href: e.href });
      if (!d.has_more_entries || !d.next_request_voucher) return { entries, total: d.total_num_entries };
      params.set('voucher', d.next_request_voucher);
    }
    return { entries, total: -1 };
  }, listForm);
  if (listing.entries.length !== listing.total) {
    throw new Error(`listed ${listing.entries.length} of ${listing.total} entries`);
  }

  const wanted = listing.entries.filter((e) => GRIDS.includes(e.name.split('_')[0]) && e.name.endsWith('.tar'));
  fs.writeFileSync(`${OUT}/manifest.json`, JSON.stringify(wanted, null, 1));
  const cookies = (await context.cookies()).map((c) => [
    c.domain, c.domain.startsWith('.') ? 'TRUE' : 'FALSE', c.path, c.secure ? 'TRUE' : 'FALSE',
    String(c.expires > 0 ? Math.floor(c.expires) : 0), c.name, c.value].join('\t'));
  fs.writeFileSync(`${OUT}/cookies.txt`, ['# Netscape HTTP Cookie File', ...cookies, ''].join('\n'), { mode: 0o600 });
  await browser.close();

  const byGrid = {};
  for (const e of wanted) {
    const g = e.name.split('_')[0];
    byGrid[g] = byGrid[g] || { n: 0, bytes: 0 };
    byGrid[g].n += 1; byGrid[g].bytes += e.bytes;
  }
  for (const [g, s] of Object.entries(byGrid)) console.log(`${g} ${s.n} packages ${(s.bytes / 1e9).toFixed(1)} GB`);
})().catch((e) => { console.error(String(e).slice(0, 400)); process.exit(1); });
