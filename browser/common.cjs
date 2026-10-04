const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const expected = require('./browser-lock.json');

function validateManifest(f) {
  assert.equal(f.synthetic, true);
  assert.equal(f.no_external_provider_calls, true);
  assert.match(f.database, /^iirp_v1_test_[a-z0-9_]+$/);
  assert.match(f.validation_id, /^[a-f0-9]{32}$/);
  const u = new URL(f.base);
  assert.equal(u.protocol, 'http:');
  assert.equal(u.hostname, '127.0.0.1');
  assert.notEqual(u.port, '18081');
  assert(+u.port >= 1024 && +u.port <= 65535);
  assert.equal(u.origin, f.base);
  assert.equal(u.username, ''); assert.equal(u.password, '');
  return f;
}
async function verifyFixture(f, fetcher = fetch) {
  validateManifest(f);
  const response = await fetcher(f.base + '/__iirp_test_identity__', {
    signal: AbortSignal.timeout(3000), redirect: 'error',
  });
  assert(response.ok, 'Live synthetic identity endpoint is required before any app request');
  const identity = await response.json();
  for (const key of ['validation_id', 'database', 'synthetic', 'no_external_provider_calls'])
    assert.equal(identity[key], f[key], 'Live synthetic identity mismatch: ' + key);
  return f;
}
async function loadFixture() {
  assert(process.env.IIRP_BROWSER_MANIFEST, 'Set IIRP_BROWSER_MANIFEST');
  return verifyFixture(JSON.parse(await fs.readFile(process.env.IIRP_BROWSER_MANIFEST, 'utf8')));
}
const chromium = {
  async launch(options = {}) {
    assert(!options.executablePath && !options.channel, 'Use the pinned bundled browser');
    process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(__dirname, '.browsers');
    const pkg = require('./node_modules/playwright-core/package.json');
    assert.equal(pkg.version, expected.version);
    const browsers = require('./node_modules/playwright-core/browsers.json');
    const target = browsers.browsers.find(b => b.name === expected.browser);
    assert.equal(target.revision, expected.revision);
    assert.equal(target.browserVersion, expected.browserVersion);
    const browser = await require('./node_modules/playwright-core').chromium.launch(options);
    assert.equal(browser.version(), expected.browserVersion);
    return browser;
  },
};
module.exports = { chromium, loadFixture, verifyFixture, validateManifest };
