// Fixture pages come from real route tests; assets served from the repository.
const {spawn, spawnSync} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'cartarch-browser-'));
const python = process.env.PYTHON || 'python';
const port = process.env.AUDIT_BROWSER_PORT || '5582';
const env = {...process.env, AUDIT_BROWSER_FIXTURES: directory, AUDIT_BROWSER_URL: `http://127.0.0.1:${port}`};
let server;
(async () => {
  const seeded = spawnSync(python, ['-m','pytest','-q','tests/test_audit_regressions.py::test_printing_modal_renders_hover_fallback'], {env, stdio:'inherit'});
  if (seeded.status !== 0) throw Error('Browser fixture rendering failed');
  fs.symlinkSync(path.resolve('app/static'), path.join(directory,'static'));
  server = spawn(python, ['-m','http.server',port,'--bind','127.0.0.1','--directory',directory], {stdio:'ignore'});
  let ready = false;
  for (let i=0;i<100;i++) {
    try { if ((await fetch(env.AUDIT_BROWSER_URL + '/deck.html')).ok) { ready=true; break; } } catch {}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  if (!ready) throw Error('Fixture server did not start');
  for (const browser of ['chromium','firefox']) {
    const result = spawnSync(process.execPath,['tests/browser/audit-regressions.cjs'], {env:{...env,BROWSER:browser},stdio:'inherit'});
    if (result.status !== 0) throw Error(`${browser} audit regressions failed`);
  }
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{
  server?.kill('SIGTERM'); fs.rmSync(directory,{recursive:true,force:true});
});
