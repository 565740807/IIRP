// All browser fixtures run serially. Every child verifies the live DB identity.
const fs = require('node:fs/promises');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {loadFixture} = require('./common.cjs');
(async () => {
  const manifest = path.resolve(process.env.IIRP_BROWSER_MANIFEST || process.argv[2] || '');
  const output = process.env.IIRP_BROWSER_OUTPUT || process.argv[3];
  if (!output) throw Error('Usage: npm test -- <fixture.json> <new-output-directory>');
  process.env.IIRP_BROWSER_MANIFEST = manifest;
  const fixture = await loadFixture();
  const out = path.resolve(output); await fs.mkdir(out, {recursive: false});
  const cases = ['event-intents', 'storage', 'confirm', 'native-recovery', 'desktop', 'shared-controls'];
  const summary = {fixture: {database: fixture.database, validation_id: fixture.validation_id}, cases: []};
  for (const name of cases) {
    const env = {...process.env, IIRP_BROWSER_MANIFEST: manifest, IIRP_BROWSER_OUTPUT: path.join(out, name),
      IIRP_INTENT_MANIFEST: manifest, IIRP_INTENT_OUTPUT: path.join(out, name),
      IIRP_RECOVERY_MANIFEST: manifest, IIRP_UI_OUTPUT: path.join(out, name)};
    const log = await fs.open(path.join(out, name + '.log'), 'wx');
    const start = Date.now();
    const child = spawn(process.execPath, [path.join(__dirname, 'tests', name+'.cjs'), 'portable'], {
      cwd: path.resolve(__dirname, '..'), env, detached: true, stdio: ['ignore', log.fd, log.fd],
    });
    const killGroup = signal => {try {process.kill(-child.pid, signal);} catch(e) {if(e.code !== 'ESRCH') throw e;}};
    let force;
    const timeout = setTimeout(() => {killGroup('SIGTERM');force=setTimeout(()=>killGroup('SIGKILL'),5000);}, 10*60*1000);
    const code = await new Promise((resolve, reject) => {child.once('error', reject); child.once('close', resolve);});
    clearTimeout(timeout); clearTimeout(force); killGroup('SIGTERM'); await log.close();
    summary.cases.push({name, exit_code: code, seconds: (Date.now()-start)/1000});
    await fs.writeFile(path.join(out, 'summary.json'), JSON.stringify(summary, null, 2));
  }
  if (summary.cases.some(c => c.exit_code !== 0)) process.exitCode = 1;
})().catch(error => {console.error(error); process.exitCode = 1;});
