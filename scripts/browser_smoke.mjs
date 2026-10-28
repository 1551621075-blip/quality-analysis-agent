#!/usr/bin/env node
/** Original, dependency-free Chrome/CDP smoke. SyntheticProvider is NOT LLM acceptance.
 * Run with node scripts/browser_smoke.mjs
 * Owns only temporary fixture/profile directories and its two child process groups.
 * No HTTP interception, request mocks, external endpoints, user profile, or downloads.
 */
import {spawn} from 'node:child_process';
import {mkdtemp, readdir, readFile, rm} from 'node:fs/promises';
import {rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createInterface} from 'node:readline';
import {isDeepStrictEqual} from 'node:util';

const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PYTHON = join(PROJECT, '.venv/bin/python');
const START = Date.now();
const results = [];
const children = [];
const secrets = [];
let ownedRoot, chrome, server, cdp, tab, bootstrap, origin, stopped = false;
let controlId = 0;
const controlPending = new Map();
const responses = [];
const requests = new Map();
const browserFaults = [];
const foreignRequests = new Set();
let acceptingDelete = false;
let preparedOperator, firstOperator, followedOperator, analystCitation;
let adminQuestion = '';

const XSS = '<img id="qa-smoke-xss" src="/smoke-never-an-image" onerror="window.__qaSmokeXSS=73">';
const QUESTION = `原创合成浏览器检查：比较产线缺陷率，不作因果结论。字面文本 ${XSS}`;
const MARKER = '锆箍霓雾校验';
const DOC_ID = 'BROWSER-ORIGINAL-LITERAL';
const DOC_TEXT = `原创合成报告，不是生产数据。${MARKER}：灯笼夹具在虚构的两组各24次练习中分别出现3次和1次偏移。未随机化，不能推断因果；下一步需要平衡抽样。下面是报告原文中的字面标记，不是HTML：${XSS}。保留原文用于引用哈希、ACL与删除失效检查。`;
const scope = {plant_id: 'PLANT-A', product_id: 'WIDGET-A', start_date: '2025-01-01', end_date: '2025-02-01', metric_id: 'defect_rate', line_ids: [], min_group_n: 20};

function assert(condition, message) { if (!condition) throw new Error(message); }
function sanitize(value) {
  let text = String(value);
  for (const secret of secrets) text = text.split(secret).join('[REDACTED]');
  return text.slice(0, 1400);
}
function record(name, status, detail) { results.push({name, status, ...(detail ? {detail: sanitize(detail)} : {})}); }
async function test(name, work) {
  if (stopped) return false;
  try { await work(); record(name, 'pass'); return true; }
  catch (error) { record(name, 'fail', error.message); return false; }
}
function skip(name, reason) { record(name, 'not_tested', reason); }
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
async function deadline(promise, milliseconds, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(label)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}
async function until(work, label, timeout = 10000) {
  const end = Math.min(Date.now() + timeout, START + 164000);
  while (!stopped && Date.now() < end) {
    const value = await work();
    if (value) return value;
    await delay(35);
  }
  throw new Error(`Timed out: ${label}`);
}
function cleanEnv() {
  // Do not inherit model endpoints, API keys, proxy settings, or user configuration.
  return {PATH: `${join(PROJECT, '.venv/bin')}:/usr/bin:/bin:/usr/sbin:/sbin`,
    HOME: ownedRoot, TMPDIR: ownedRoot, LANG: 'en_US.UTF-8',
    PYTHONPATH: join(PROJECT, 'src'), PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1'};
}
function child(executable, args, stdio) {
  const proc = spawn(executable, args, {cwd: PROJECT, env: cleanEnv(), stdio, detached: true});
  children.push(proc);
  proc.on('error', () => { proc.launchFailed = true; });
  // Never forward subprocess output: neither tracebacks nor Chrome diagnostics are a token log.
  proc.stderr?.resume();
  return proc;
}
function killGroup(proc, signal) {
  if (!proc?.pid) return;
  try { process.kill(-proc.pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
}

// This Python program is passed in argv; only the random fixture path is in argv.
// Bootstrap tokens are created in the child, sent over fd 3, and held in memory only.
// The app stores its normal SHA-256 token hashes, never the bearer tokens themselves.
const PY_SERVER = String.raw`
import json, os, socket, sys, threading
from pathlib import Path
import uvicorn
from quality_analysis.api import create_app
from quality_analysis.business import seed_business, example_scope
from quality_analysis.documents import original_reports
from quality_analysis.provider import SyntheticProvider
from quality_analysis.schemas import DocumentInput

app = create_app(Path(sys.argv[1]), provider=SyntheticProvider())
store = app.state.store
seed_business(store, seed=913)
tokens = {role: store.provision_user('browser-' + role, role, ['PLANT-A'])
          for role in ('operator', 'analyst', 'admin')}
admin = store.authenticate(tokens['admin'])
for document in original_reports('PLANT-A'):
    app.state.documents.ingest(document, admin)
# No fourth token: temporarily grant the bootstrap admin B for this seed through
# actual stored authorization, then restrict it to A before opening the HTTP server.
with store.transaction() as db:
    db.execute("UPDATE users SET plants=? WHERE actor_id='browser-admin'", ('["PLANT-A","PLANT-B"]',))
foreign_actor = store.authenticate(tokens['admin'])
foreign = DocumentInput(document_id='BROWSER-FOREIGN', title='原创合成隔域报告', plant_id='PLANT-B',
    text='原创合成隔域报告，铪环隔域笔记只属于工厂B。虚构夹具练习没有生产来源，也不能证明因果。此材料用于浏览器工厂授权边界的实际检索测试。',
    summaries={key: '铪环隔域笔记：原创合成材料，不是生产记录。' for key in ('phenomenon','context','hypothesis','evidence','action')})
app.state.documents.ingest(foreign, foreign_actor)
with store.transaction() as db:
    db.execute("UPDATE users SET plants=? WHERE actor_id='browser-admin'", ('["PLANT-A"]',))
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('127.0.0.1', 0))
sock.listen(128)
channel = os.fdopen(3, 'w', buffering=1)
channel.write(json.dumps({'type':'ready', 'port':sock.getsockname()[1], 'tokens':tokens,
    'scope':example_scope().model_dump(mode='json')}) + '\n')
def control():
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
            command = message['command']
            if command == 'snapshot':
                with store.connect() as db:
                    counts = {r[0]: r[1] for r in db.execute('SELECT kind,COUNT(*) FROM entities GROUP BY kind')}
                value = {'entities': counts}
            elif command == 'change_operator_acl':
                with store.transaction() as db:
                    db.execute("UPDATE users SET plants=?,acl_version=acl_version+1 WHERE actor_id='browser-operator'", ('["PLANT-B"]',))
                value = {'changed': True}
            elif command == 'revoke_operator':
                with store.transaction() as db:
                    db.execute("UPDATE users SET active=0 WHERE actor_id='browser-operator'")
                value = {'revoked': True}
            else:
                raise ValueError('unsupported fixture control')
            channel.write(json.dumps({'id':message['id'], 'value':value}) + '\n')
        except Exception:
            channel.write(json.dumps({'id':message.get('id'), 'error':'fixture_control_failed'}) + '\n')
threading.Thread(target=control, daemon=True).start()
uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=sock.getsockname()[1],
    log_level='critical', access_log=False, log_config=None)).run(sockets=[sock])
`;

class PipeCDP {
  constructor(proc) {
    this.proc = proc;
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = new Set();
    let buffer = Buffer.alloc(0);
    proc.stdio[4].on('data', (part) => {
      buffer = Buffer.concat([buffer, part]);
      let end;
      while ((end = buffer.indexOf(0)) >= 0) {
        const raw = buffer.subarray(0, end).toString('utf8');
        buffer = buffer.subarray(end + 1);
        if (!raw) continue;
        let value;
        try { value = JSON.parse(raw); } catch { this.close(); return; }
        const pending = this.pending.get(value.id);
        if (pending) {
          this.pending.delete(value.id); clearTimeout(pending.timer);
          if (value.error) pending.reject(new Error(`CDP ${pending.method} failed (${value.error.code})`));
          else pending.resolve(value.result);
        } else {
          for (const handler of this.listeners) {
            Promise.resolve(handler(value)).catch(() => browserFaults.push('CDP observer failed'));
          }
        }
      }
    });
    proc.stdio[3].on('error', () => this.close());
    proc.on('exit', () => this.close());
  }
  send(method, params = {}, sessionId) {
    if (stopped) return Promise.reject(new Error('Smoke stopped'));
    return new Promise((resolve, reject) => {
      const id = ++this.nextId;
      const timer = setTimeout(() => {
        this.pending.delete(id); reject(new Error(`CDP ${method} timeout`));
      }, 8000);
      this.pending.set(id, {resolve, reject, timer, method});
      this.proc.stdio[3].write(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}) + '\0');
    });
  }
  close() {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer); pending.reject(new Error(`CDP closed during ${pending.method}`));
    }
    this.pending.clear();
  }
}
const cmd = (method, params = {}) => cdp.send(method, params, tab);
async function evaluate(expression) {
  const result = await cmd('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  assert(!result.exceptionDetails, 'Browser evaluation raised an exception (details withheld)');
  return result.result?.value;
}
const lit = (value) => JSON.stringify(value);
async function click(selector) {
  const point = await evaluate(`(() => {
    const e = document.querySelector(${lit(selector)});
    if (!e || e.disabled || e.closest('[hidden]')) return null;
    e.scrollIntoView({block:'center', inline:'center'});
    const r = e.getBoundingClientRect();
    return r.width && r.height ? {x:r.x+r.width/2,y:r.y+r.height/2} : null;
  })()`);
  assert(point, `Not a visible enabled click target: ${selector}`);
  await cmd('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...point});
  await cmd('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...point});
}
async function fill(selector, value) {
  assert(await evaluate(`(() => {
    const e = document.querySelector(${lit(selector)});
    if (!e || e.closest('[hidden]')) return false;
    e.scrollIntoView({block:'center'}); e.focus(); e.select(); return true;
  })()`), `Missing input: ${selector}`);
  await cmd('Input.insertText', {text: String(value)});
}
async function idle() {
  await until(() => evaluate(`![...document.querySelectorAll('#workbench button')].some(b => b.disabled)`), 'UI idle');
}
async function responseAfter(mark, path, method = 'POST') {
  return until(() => responses.slice(mark).find(r => r.path === path && r.method === method && r.done), `${method} ${path}`);
}
async function submit(selector, path, method = 'POST') {
  const mark = responses.length;
  await click(selector);
  const response = await responseAfter(mark, path, method);
  await idle();
  return response;
}
function ok(response, label) {
  assert(response.status === 200, `${label}: HTTP ${response.status}; code=${response.body?.error?.code || 'none'}`);
  return response.body;
}
async function login(role) {
  await fill('#token', bootstrap.tokens[role]);
  ok(await submit('#login-form button[type=submit]', '/api/me', 'GET'), `${role} login`);
  await until(() => evaluate(`!document.getElementById('workbench').hidden && document.getElementById('identity').textContent.includes(${lit('browser-' + role)})`), `${role} identity`);
  await idle();
  assert(await evaluate(`document.getElementById('token').value === ''`), 'Token input was not cleared after login');
}
async function exampleScope() {
  await click('#example');
  const actual = await evaluate(`(() => {const v=id=>document.getElementById(id).value; return {plant_id:v('plant'),product_id:v('product'),start_date:v('start'),end_date:v('end'),metric_id:v('metric'),line_ids:v('lines')?v('lines').split(','):[],min_group_n:Number(v('min-n'))}})()`);
  assert(JSON.stringify(actual) === JSON.stringify(scope), 'Explicit example UI selected an unexpected scope');
}
async function browserProbe(path, role, method = 'GET', body) {
  // Authorization negatives have no UI route: real browser-origin fetch, not mocked routing.
  const token = role ? bootstrap.tokens[role] : '';
  return evaluate(`(async () => {
    const r = await fetch(${lit(path)}, {method:${lit(method)}, credentials:'omit', cache:'no-store',
      headers:${lit({...token ? {Authorization: 'Bearer ' + token} : {}, ...body === undefined ? {} : {'Content-Type':'application/json'}})},
      ${body === undefined ? '' : `body:${lit(JSON.stringify(body))},`}});
    return {status:r.status,body:await r.json()};
  })()`);
}
async function control(command) {
  const id = ++controlId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {controlPending.delete(id); reject(new Error('Fixture control timeout'));}, 5000);
    controlPending.set(id, {resolve, reject, timer});
    server.stdin.write(JSON.stringify({id, command}) + '\n');
  });
}
async function xssCheck() {
  assert(await evaluate(`!window.__qaSmokeXSS && !document.querySelector('#qa-smoke-xss') && !document.querySelector('#sessions img, #session-scope img, #search-results img, #report img')`), 'XSS-shaped text became a DOM node or executed');
  assert(!responses.some(r => r.path === '/smoke-never-an-image'), 'XSS-shaped image requested a resource');
}
async function storageCheck() {
  const snapshot = await evaluate(`(async () => ({
    local:Object.entries(localStorage), session:Object.entries(sessionStorage), cookies:document.cookie,
    databases:await indexedDB.databases(), caches:await caches.keys(),
    dom:document.documentElement.outerHTML, url:location.href
  }))()`);
  const cookies = await cdp.send('Storage.getCookies');
  const serialized = JSON.stringify({snapshot, cookies});
  assert(!secrets.some(secret => serialized.includes(secret)), 'Bearer token found in DOM, URL or browser storage');
  assert(snapshot.local.length === 0 && snapshot.session.length === 0 && snapshot.cookies === '' &&
    snapshot.databases.length === 0 && snapshot.caches.length === 0 && cookies.cookies.length === 0,
  'Fresh browser gained local/sessionStorage, cookies, IndexedDB or CacheStorage entries');
}
async function cleared() {
  assert(await evaluate(`document.getElementById('workbench').hidden && document.getElementById('token').value === '' &&
    ['sessions','report','versions','core-plan','extended-plan','followup-plan','trace','search-results','audit','calls'].every(id=>document.getElementById(id).textContent==='') &&
    ['question','followup-question','document-text','search-query','plant','product'].every(id=>document.getElementById(id).value==='')`), 'Identity or sensitive page state survived clearing');
}
function evidenceIds(value) { return value.report.sections.flatMap(s => s.evidence.map(e => e.evidence_id)); }
async function assertReport(value, sqIds, unexecuted) {
  assert(value.status === 'completed', `Analysis did not complete: status=${value.status}; code=${value.last_error?.code || 'none'}`);
  assert(value.report && !value.report.stale, 'Current report is missing or stale');
  assert(isDeepStrictEqual(value.report.scope, scope) && isDeepStrictEqual(value.report.scope, bootstrap.scope), 'Report scope differs from explicit UI scope');
  assert(JSON.stringify(value.report.sections.map(s => s.sq_id).sort()) === JSON.stringify([...sqIds].sort()), 'Unexpected executed report sections');
  assert(JSON.stringify(value.report.unexecuted.map(s => s.sq_id).sort()) === JSON.stringify([...unexecuted].sort()), 'Unexpected unexecuted SQs');
  for (const section of value.report.sections) {
    assert(section.revision_id && section.evidence.length, 'Report section lacks current revision or evidence');
    assert(section.evidence.every(e => e.evidence_id && e.source && !e.source.startsWith('synthetic-test') && Object.keys(e.payload).length), 'Report evidence is empty or not real tool output');
  }
  const dom = await evaluate(`({text:document.getElementById('report').textContent,
    sections:[...document.querySelectorAll('#report article')].map(a=>({heading:a.querySelector('h4').textContent,revisions:a.querySelector('p').textContent,evidence:[...a.querySelectorAll('details')].map(d=>({id:d.querySelector('summary').textContent,payload:JSON.parse(d.querySelector('pre').textContent)}))})),
    trace:[...document.getElementById('trace').children].map(e=>e.textContent)})`);
  assert(dom.text.includes('未执行项（不是分析结论）'), 'Report lacks an explicit unexecuted section');
  assert(dom.sections.length === value.report.sections.length, 'Report DOM section count differs from service');
  for (let i=0; i<dom.sections.length; i++) {
    const actual = value.report.sections[i], shown = dom.sections[i];
    assert(shown.heading.includes(actual.sq_id) && shown.revisions.includes(actual.revision_id), 'DOM did not render current revision');
    assert(shown.evidence.length === actual.evidence.length, 'DOM evidence count differs from service');
    shown.evidence.forEach((e, n) => assert(e.id.includes(actual.evidence[n].evidence_id) && JSON.stringify(e.payload) === JSON.stringify(actual.evidence[n].payload), 'DOM evidence differs from service output'));
  }
  assert(JSON.stringify(dom.trace) === JSON.stringify(value.trace), 'Displayed trace differs from real executed nodes');
  assert(['hypothesizer','verifier','report_renderer'].every(n => value.trace.includes(n)), 'Execution trace lacks required nodes');
  await click('#report article details > summary');
  assert(await evaluate(`document.querySelector('#report article details').open`), 'Evidence details could not be expanded');
  await xssCheck();
}
async function prepare(question) {
  await exampleScope(); await fill('#question', question);
  return ok(await submit('#analyze-form button[type=submit]', '/api/sessions'), 'Prepare plan');
}
async function coreRun(question) {
  const prepared = await prepare(question);
  assert(prepared.status === 'awaiting_confirmation', `Plan not confirmable: ${prepared.status}`);
  await click('#core-plan input[value="core-rate"]');
  const value = ok(await submit('#confirm-form button[type=submit]', `/api/sessions/${prepared.session_id}/confirm`), 'Confirm core');
  await assertReport(value, ['core-rate'], ['lot-strata', 'orders']);
  return value;
}
async function search(query = MARKER) {
  await fill('#search-query', query);
  return ok(await submit('#search-form button[type=submit]', '/api/documents/search'), 'Search');
}
async function openSession(question) {
  const selector = await evaluate(`(() => {const buttons=[...document.querySelectorAll('#sessions button')];const n=buttons.findIndex(b=>b.textContent.startsWith(${lit(question)}));return n<0?null:'#sessions li:nth-child('+(n+1)+') button'})()`);
  assert(selector, 'Expected owned session missing from UI list');
  const mark = responses.length; await click(selector); await idle();
  return until(() => responses.slice(mark).find(r => /^\/api\/sessions\/[^/]+$/.test(r.path) && r.method === 'GET' && r.done), 'Open session');
}
async function staleAdminReport(question) {
  await submit('#refresh-sessions', '/api/me', 'GET');
  const current = ok(await openSession(question), 'Reload old report');
  assert(current.report?.stale && current.report.sections.length === 0 && current.plan === null, 'Changed index exposed old report/plan');
  assert(await evaluate(`document.querySelectorAll('#report article').length===0 && document.getElementById('followup-form').hidden && document.getElementById('confirm-form').hidden`), 'Stale report left evidence or execution controls visible');
}
async function invalidatedViews() {
  assert(await evaluate(`['report','versions','search-results','core-plan','extended-plan','followup-plan','trace','audit','sessions','calls'].every(id=>document.getElementById(id).textContent==='') && document.getElementById('followup-form').hidden && document.getElementById('confirm-form').hidden`), 'Document mutation did not clear old views');
}

async function run() {
  ownedRoot = await mkdtemp(join(tmpdir(), 'quality-original-browser-'));
  server = child(PYTHON, ['-u', '-c', PY_SERVER, join(ownedRoot, 'app')], ['pipe','ignore','ignore','pipe']);
  const ready = new Promise((resolve, reject) => {
    createInterface({input:server.stdio[3]}).on('line', (line) => {
      let message;
      try { message = JSON.parse(line); } catch { reject(new Error('Invalid bootstrap protocol')); return; }
      if (message.type === 'ready') { resolve(message); return; }
      const pending = controlPending.get(message.id);
      if (pending) {
        clearTimeout(pending.timer); controlPending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error)); else pending.resolve(message.value);
      }
    });
    server.once('exit', () => reject(new Error('Synthetic app exited before bootstrap')));
    server.once('error', () => reject(new Error('Could not launch project .venv Python')));
  });
  bootstrap = await deadline(ready, 20000, 'Synthetic app bootstrap timeout');
  secrets.push(...Object.values(bootstrap.tokens));
  origin = `http://127.0.0.1:${bootstrap.port}`;
  chrome = child(CHROME, ['--headless=new', '--remote-debugging-pipe', `--user-data-dir=${join(ownedRoot, 'profile')}`,
    '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--disable-component-update',
    '--disable-default-apps', '--disable-sync', '--disable-breakpad', '--disable-crash-reporter',
    '--disable-domain-reliability', '--safebrowsing-disable-auto-update', '--disable-client-side-phishing-detection',
    '--metrics-recording-only', '--password-store=basic', '--use-mock-keychain', '--no-proxy-server',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter,Translate,PasswordLeakDetection',
    '--window-size=1440,1100', 'about:blank'], ['ignore','ignore','ignore','pipe','pipe']);
  cdp = new PipeCDP(chrome);
  const version = await cdp.send('Browser.getVersion');
  record('actual_browser', 'pass', `${version.product}; ${version.userAgent}; JavaScript ${version.jsVersion}`);
  const target = await cdp.send('Target.createTarget', {url:'about:blank'});
  tab = (await cdp.send('Target.attachToTarget', {targetId:target.targetId, flatten:true})).sessionId;
  cdp.listeners.add(async (event) => {
    if (event.sessionId !== tab) return;
    const p = event.params;
    if (event.method === 'Runtime.exceptionThrown') browserFaults.push('Uncaught browser runtime exception');
    if (event.method === 'Runtime.consoleAPICalled') {
      if (secrets.some(secret => JSON.stringify(p.args).includes(secret))) browserFaults.push('Bearer token in browser console');
    }
    if (event.method === 'Page.javascriptDialogOpening') {
      const expected = acceptingDelete && p.type === 'confirm' && p.message.includes(DOC_ID);
      if (!expected) browserFaults.push('Unexpected JavaScript dialog');
      await cmd('Page.handleJavaScriptDialog', {accept:expected});
    }
    if (event.method === 'Network.requestWillBeSent') {
      const url = new URL(p.request.url);
      if (url.protocol === 'http:' || url.protocol === 'https:') {
        if (url.origin !== origin) foreignRequests.add(url.origin);
        if (secrets.some(secret => p.request.url.includes(secret))) browserFaults.push('Bearer token in request URL');
      }
      requests.set(p.requestId, {path:url.pathname, method:p.request.method});
    }
    if (event.method === 'Network.responseReceived') {
      const request = requests.get(p.requestId);
      if (!request) return;
      const entry = {...request, requestId:p.requestId, status:p.response.status,
        noStore:Object.entries(p.response.headers).some(([k,v]) => k.toLowerCase()==='cache-control' && String(v).includes('no-store')),
        fromCache:Boolean(p.response.fromDiskCache || p.response.fromServiceWorker), done:false};
      responses.push(entry);
    }
    if (event.method === 'Network.loadingFinished') {
      const entry = responses.find(r => r.requestId === p.requestId && !r.done);
      if (!entry) return;
      if (entry.path.startsWith('/api/')) {
        try {
          const payload = await cmd('Network.getResponseBody', {requestId:p.requestId});
          const raw = payload.base64Encoded ? Buffer.from(payload.body, 'base64').toString() : payload.body;
          if (secrets.some(secret => raw.includes(secret))) browserFaults.push('Bearer token in API response');
          entry.body = JSON.parse(raw);
        } catch { entry.body = null; }
      }
      entry.done = true;
    }
  });
  await cmd('Page.enable'); await cmd('Runtime.enable'); await cmd('Network.enable');
  await cmd('Page.navigate', {url:origin});
  await until(() => evaluate(`document.readyState === 'complete' && document.getElementById('provider-label')?.textContent.includes('synthetic-test')`), 'Actual app initial DOM');

  await test('unauthenticated_UI_and_explicit_synthetic_provider', async () => {
    await cleared();
    assert((await browserProbe('/api/me')).status === 401, 'Unauthenticated endpoint was accessible');
    assert(await evaluate(`document.getElementById('provider-label').textContent.includes('不是本地LLM/BGE验收')`), 'Synthetic provider not visibly disclaimed');
  });
  const loggedIn = await test('operator_real_DOM_login_role_gates_and_storage', async () => {
    await login('operator');
    assert(await evaluate(`document.getElementById('document-editor').hidden && document.getElementById('document-admin').hidden`), 'Operator has document mutation UI');
    await storageCheck();
  });
  const planned = loggedIn && await test('explicit_scope_XSS_literal_plan_without_execution', async () => {
    preparedOperator = await prepare(QUESTION);
    assert(preparedOperator.status === 'awaiting_confirmation' && preparedOperator.authorized.length === 0 && preparedOperator.tools === 1 && !preparedOperator.report, 'Preparing plan executed SQ work or failed');
    assert(!preparedOperator.trace.includes('hypothesizer') && !preparedOperator.trace.includes('verifier'), 'Preparation trace contains execution');
    const snapshot = await control('snapshot');
    assert(!snapshot.entities.Hypothesis && !snapshot.entities.Evidence, 'Preparation persisted hypothesis/evidence');
    assert(await evaluate(`document.querySelector('#sessions button').textContent.startsWith(${lit(QUESTION)}) && JSON.parse(document.getElementById('session-scope').textContent).question===${lit(QUESTION)} &&
      document.querySelectorAll('#core-plan input:checked').length===0 && document.querySelectorAll('#extended-plan input').length===0 && document.getElementById('followup-form').hidden`), 'Plan literal rendering or explicit selection controls failed');
    assert(preparedOperator.plan.subquestions.find(sq=>sq.sq_id==='core-rate').group_by==='line_id' &&
      preparedOperator.plan.subquestions.find(sq=>sq.sq_id==='lot-strata').group_by==='material_lot', 'Plan lacks explicit SQ grouping');
    assert(await evaluate(`document.getElementById('core-plan').textContent.includes('SQL分组授权：line_id') &&
      document.getElementById('extended-plan').textContent.includes('SQL分组授权：material_lot') &&
      document.getElementById('extended-plan').textContent.includes('总体（不分组）')`), 'Grouping authorization is not visible before confirmation');
    await xssCheck();
  });
  if (planned) await test('empty_core_confirmation_does_not_send_request', async () => {
    const count = responses.filter(r=>r.path.endsWith('/confirm')).length;
    await click('#confirm-form button[type=submit]'); await idle();
    assert(await evaluate(`!document.getElementById('error').hidden && document.getElementById('error').textContent.includes('至少一个核心')`), 'Empty core choice was not rejected');
    assert(responses.filter(r=>r.path.endsWith('/confirm')).length === count, 'Empty selection sent confirm request');
  });
  const confirmed = planned && await test('operator_explicit_core_confirm_current_evidence_revision_unexecuted_trace', async () => {
    await click('#core-plan input[value="core-rate"]');
    firstOperator = ok(await submit('#confirm-form button[type=submit]', `/api/sessions/${preparedOperator.session_id}/confirm`), 'Operator core confirmation');
    await assertReport(firstOperator, ['core-rate'], ['lot-strata', 'orders']);
    assert(await evaluate(`!document.getElementById('followup-form').hidden && document.querySelectorAll('#followup-plan input:checked').length===0`), 'Followup must require a new explicit selection');
  });
  if (confirmed) {
    await test('extended_requires_explicit_followup_new_round_new_evidence', async () => {
      const path = `/api/sessions/${firstOperator.session_id}/followup`;
      const count = responses.filter(r=>r.path===path).length;
      await fill('#followup-question', `原创合成追问：只授权材料批次分层及核心依赖。${XSS}`);
      await click('#followup-form button[type=submit]'); await idle();
      assert(await evaluate(`!document.getElementById('error').hidden && document.getElementById('error').textContent.includes('勾选本次追问')`), 'Empty followup selection not rejected');
      assert(responses.filter(r=>r.path===path).length === count, 'Empty followup choice sent a request');
      await click('#followup-plan input[value="lot-strata"]');
      followedOperator = ok(await submit('#followup-form button[type=submit]', path), 'Authorized followup');
      await assertReport(followedOperator, ['core-rate', 'lot-strata'], ['orders']);
      assert(followedOperator.round_id !== firstOperator.round_id && evidenceIds(followedOperator).every(id=>!evidenceIds(firstOperator).includes(id)), 'Followup reused a previous round/evidence identity');
      assert(followedOperator.report.sections.every(s=>!firstOperator.report.sections.some(old=>old.revision_id===s.revision_id)), 'Followup reused revision identities');
      assert([...followedOperator.authorized].sort().join(',') === 'core-rate,lot-strata', 'Followup expanded its authorization');
    });
    await test('real_audit_DOM_and_no_token_storage_after_report', async () => {
      const audit = ok(await submit('#audit-button', `/api/sessions/${firstOperator.session_id}/audit`, 'GET'), 'Audit');
      const actions = audit.audit.map(row=>row.action);
      assert(['analysis_requested','plan_confirmed', ...(followedOperator ? ['followup_authorized'] : [])].every(a=>actions.includes(a)), 'Actual audit lacks expected actions');
      assert(await evaluate(`JSON.parse(document.getElementById('audit').textContent).length === ${audit.audit.length}`), 'Audit DOM differs from response');
      await storageCheck();
    });
  } else skip('followup_report_and_audit', 'Core confirmation did not pass; not bypassed');

  if (loggedIn) await test('unauthorized_plant_analysis_via_actual_UI', async () => {
    await fill('#plant', 'PLANT-B'); await fill('#question', '原创合成越权检查：不应分析未授权工厂');
    const result = await submit('#analyze-form button[type=submit]', '/api/sessions');
    assert(result.status === 403, `Other-plant analysis returned ${result.status}`);
    assert(await evaluate(`!document.getElementById('error').hidden`), 'Plant denial was not shown');
    await fill('#plant', 'PLANT-A');
  });
  await test('analyst_other_owner_UI_isolation_and_browser_origin_404s', async () => {
    await login('analyst');
    assert(await evaluate(`!document.getElementById('document-editor').hidden && document.getElementById('document-admin').hidden && document.querySelectorAll('#sessions button').length===0 && document.getElementById('report').textContent===''`), 'Analyst role or owner UI isolation failed');
    assert(preparedOperator, 'No operator session available for owner probes');
    for (const suffix of ['', '/audit', '/calls']) assert((await browserProbe(`/api/sessions/${preparedOperator.session_id}${suffix}`, 'analyst')).status === 404, 'Other owner could access session resource');
    await exampleScope();
    assert(!(await search('铪环隔域笔记')).hits.some(hit=>hit.document_id==='BROWSER-FOREIGN'), 'Different-plant seeded document leaked into authorized search');
    await fill('#plant', 'PLANT-B');
    const result = await submit('#search-form button[type=submit]', '/api/documents/search');
    assert(result.status === 403, 'Different-plant document search was not denied');
    await fill('#plant', 'PLANT-A');
  });

  const ingested = await test('analyst_real_UI_ingest_search_literal_text_and_citation_drill', async () => {
    await login('analyst'); await exampleScope();
    await click('#document-editor > summary');
    for (const [selector, value] of Object.entries({'#document-id':DOC_ID, '#document-title':`原创合成 ${MARKER} ${XSS}`, '#document-plant':'PLANT-A', '#document-text':DOC_TEXT})) await fill(selector, value);
    for (const key of ['phenomenon','context','hypothesis','evidence','action']) await fill(`#summary-${key}`, `${MARKER}：原创合成参考，非因果结论。${XSS}`);
    ok(await submit('#document-form button[type=submit]', '/api/documents/ingest'), 'Analyst ingest');
    await invalidatedViews();
    const found = await search();
    const hitIndex = found.hits.findIndex(h=>h.document_id===DOC_ID);
    assert(hitIndex >= 0, 'New literal-text report was not retrieved');
    analystCitation = found.hits[hitIndex];
    const resultSelector = `#search-results details:nth-of-type(${hitIndex+1})`;
    await click(`${resultSelector} > summary`);
    assert(await evaluate(`document.querySelector(${lit(resultSelector+' pre')}).textContent.includes(${lit(XSS)})`), 'Search did not render literal original text');
    const path = `/api/documents/${DOC_ID}/chunks/${analystCitation.chunk_id}`;
    const drilled = ok(await submit(`${resultSelector} button`, path, 'GET'), 'Citation drill');
    assert(drilled.text === analystCitation.text && drilled.content_hash === analystCitation.content_hash, 'Citation drill text/hash differs from issued hit');
    await xssCheck(); await storageCheck();
    for (const method of ['PATCH','DELETE']) assert((await browserProbe(`/api/documents/${DOC_ID}${method==='PATCH'?'/acl':''}`, 'analyst', method, method==='PATCH'?{shared_with:['browser-admin']}:undefined)).status===403, 'Analyst could mutate admin-only document controls');
  });

  const aclChanged = ingested && await test('admin_actual_UI_ACL_clears_views_and_old_report', async () => {
    await login('admin');
    assert(await evaluate(`!document.getElementById('document-admin').hidden && document.querySelectorAll('#sessions button').length===0`), 'Admin role or owner isolation failed');
    adminQuestion = '原创合成管理员分析：ACL变更前验证当前报告';
    await coreRun(adminQuestion);
    assert((await search()).hits.some(h=>h.document_id===DOC_ID), 'Admin could not retrieve report before ACL update');
    await click('#document-admin > summary'); await fill('#acl-id', DOC_ID); await fill('#acl-shared', 'browser-admin');
    ok(await submit('#acl-form button[type=submit]', `/api/documents/${DOC_ID}/acl`, 'PATCH'), 'Admin ACL');
    await invalidatedViews(); await staleAdminReport(adminQuestion);
  });
  if (aclChanged) await test('ACL_removes_analyst_search_and_invalidates_old_citation', async () => {
    await login('analyst'); await exampleScope();
    assert(!(await search()).hits.some(h=>h.document_id===DOC_ID), 'Restricted document still appears for analyst');
    const denied = await browserProbe(`/api/documents/${DOC_ID}/chunks/${analystCitation.chunk_id}`, 'analyst');
    assert([404,409].includes(denied.status), `Old citation survived ACL update: ${denied.status}`);
  });
  if (aclChanged) await test('admin_UI_delete_confirmation_clears_views_report_and_citation', async () => {
    await login('admin');
    const question = '原创合成管理员分析：删除前使用当前索引重新取证';
    await coreRun(question);
    const found = await search();
    const hit = found.hits.find(h=>h.document_id===DOC_ID);
    assert(hit, 'Admin could not search its ACL-restricted report');
    if (!await evaluate(`document.getElementById('document-admin').open`)) await click('#document-admin > summary');
    await fill('#acl-id', DOC_ID);
    acceptingDelete = true;
    try { ok(await submit('#delete-document', `/api/documents/${DOC_ID}`, 'DELETE'), 'Admin delete'); }
    finally { acceptingDelete = false; }
    await invalidatedViews(); await staleAdminReport(question);
    assert(!(await search()).hits.some(h=>h.document_id===DOC_ID), 'Deleted report still appears in search');
    const denied = await browserProbe(`/api/documents/${DOC_ID}/chunks/${hit.chunk_id}`, 'admin');
    assert([404,409].includes(denied.status), 'Deleted citation still resolves');
  });
  if (!ingested || !aclChanged) skip('dependent_document_ACL_delete_checks', 'Preceding real document UI operation failed; no fixture bypass');

  await test('server_changed_identity_and_revoked_token_clear_actual_UI', async () => {
    await login('operator');
    await control('change_operator_acl');
    ok(await submit('#refresh-sessions', '/api/me', 'GET'), 'Read changed identity');
    await cleared();
    assert(await evaluate(`document.getElementById('notice').textContent.includes('身份或授权已变化')`), 'Changed server identity was not rejected by UI');
    await login('operator');
    assert(await evaluate(`document.getElementById('identity').textContent.includes('PLANT-B')`), 'Reauthentication did not use current authorization');
    await control('revoke_operator');
    const rejected = await submit('#refresh-sessions', '/api/me', 'GET');
    assert(rejected.status === 401, 'Revoked token still authenticates');
    await cleared(); await storageCheck();
  });
  await test('identity_input_replacement_logout_and_reload_clear_state', async () => {
    await login('admin'); await exampleScope(); await search('valve seals');
    await fill('#token', bootstrap.tokens.analyst);
    assert(await evaluate(`document.getElementById('workbench').hidden && document.getElementById('search-results').textContent==='' && document.getElementById('sessions').textContent===''`), 'Starting identity replacement left old data visible');
    ok(await submit('#login-form button[type=submit]', '/api/me', 'GET'), 'Replacement login');
    await exampleScope(); await fill('#question', QUESTION);
    await click('#logout'); await cleared(); await storageCheck();
    await login('admin'); await exampleScope(); await search('valve seals');
    await cmd('Page.reload', {ignoreCache:false});
    await until(() => evaluate(`document.readyState==='complete' && document.getElementById('provider-label')?.textContent.includes('synthetic-test')`), 'Reload completed');
    await cleared(); await storageCheck();
    assert((await browserProbe('/api/me')).status === 401, 'Reload retained usable authentication');
  });
  await test('actual_API_no_store_no_HTTP_cache_no_console_token_or_external_requests', async () => {
    await storageCheck(); await xssCheck();
    const apiResponses = responses.filter(r=>r.path.startsWith('/api/'));
    assert(apiResponses.length > 10 && apiResponses.every(r=>r.noStore && !r.fromCache), 'API responses lack no-store or came from cache');
    assert(browserFaults.length === 0, browserFaults.join('; '));
    assert(foreignRequests.size === 0, 'Page made a non-loopback-origin request');
  });
  skip('local_LLM_BGE_and_production_acceptance', 'Explicit SyntheticProvider and original synthetic seeds only; no model/production endpoints');
  skip('untested_business_scopes_and_features', 'Only PLANT-A/WIDGET-A, [2025-01-01,2025-02-01), defect_rate, all lines, min_group_n=20. Other metrics/date/product/line scopes, uploads, calibration, retry/reconciliation and historical-version panel not exercised. PLANT-B is denial/identity-only.');
  skip('identity_inflight_race', 'Stored ACL change, revocation, input replacement, logout and reload tested; no artificially delayed response or request mock');
}

async function stopChildren() {
  for (const proc of children) killGroup(proc, 'SIGTERM');
  await Promise.race([Promise.all(children.map(proc => proc.exitCode !== null || proc.signalCode !== null || proc.launchFailed ? Promise.resolve() : new Promise(r=>proc.once('exit', r)))), delay(1500)]);
  for (const proc of children) killGroup(proc, 'SIGKILL');
  await delay(200);
  assert(children.every(proc=>proc.exitCode!==null || proc.signalCode!==null || proc.launchFailed), 'Owned child process did not exit');
}
async function scanOwnedFiles(directory) {
  let files = 0;
  const needles = secrets.flatMap(s=>[Buffer.from(s), Buffer.from(s,'utf16le')]);
  async function walk(path) {
    for (const entry of await readdir(path, {withFileTypes:true})) {
      const file = join(path, entry.name);
      if (entry.isSymbolicLink()) continue; // Never follow Chrome lock symlinks outside our root.
      if (entry.isDirectory()) await walk(file);
      else if (entry.isFile()) {
        const bytes = await readFile(file); files++;
        assert(!needles.some(n=>bytes.includes(n)), 'Plaintext bootstrap bearer found in owned app/profile files');
      }
    }
  }
  await walk(directory);
  return files;
}

let emergency = false;
function emergencyCleanup(reason) {
  if (emergency) return;
  emergency = true; stopped = true;
  for (const proc of children) { try {killGroup(proc,'SIGKILL');} catch {} }
  try {if (ownedRoot) rmSync(ownedRoot,{recursive:true,force:true,maxRetries:1});} catch {}
  process.stdout.write(JSON.stringify({status:'fail',reason,elapsed_seconds:(Date.now()-START)/1000,results})+'\n');
  process.exit(1);
}
const hardTimer = setTimeout(()=>emergencyCleanup('180s absolute deadline'), 180000);
for (const signal of ['SIGINT','SIGTERM','SIGHUP']) process.once(signal,()=>emergencyCleanup(`Interrupted by ${signal}`));
let runTimer;
try {
  await Promise.race([run(),new Promise((_,reject)=>{runTimer=setTimeout(()=>reject(new Error('165s browser workflow deadline')),165000);})]);
} catch (error) { record('browser_workflow', 'fail', error.message); }
finally {
  clearTimeout(runTimer);
  stopped = true;
  cdp?.close();
  for (const pending of controlPending.values()) {clearTimeout(pending.timer); pending.reject(new Error('Smoke cleanup'));}
  controlPending.clear();
  try {await stopChildren(); record('owned_process_cleanup','pass');}
  catch (error) {record('owned_process_cleanup','fail',error.message);}
  if (ownedRoot) {
    try {const count=await scanOwnedFiles(ownedRoot); record('no_plaintext_tokens_in_owned_profile_app_or_disk_cache','pass',`${count} owned files scanned after child shutdown; raw bearer bytes and UTF-16 checked, no content logged`);}
    catch (error) {record('no_plaintext_tokens_in_owned_profile_app_or_disk_cache','fail',error.message);}
    try {await rm(ownedRoot,{recursive:true,force:true,maxRetries:3,retryDelay:100}); record('temporary_app_and_profile_cleanup','pass');}
    catch {record('temporary_app_and_profile_cleanup','fail','Could not remove owned temporary root');}
  }
  clearTimeout(hardTimer);
}
const failed = results.filter(r=>r.status==='fail');
process.stdout.write(JSON.stringify({status:failed.length?'fail':'pass',mode:'real Chrome DOM + real loopback service; explicit SyntheticProvider, NOT LLM acceptance',
  elapsed_seconds:Number(((Date.now()-START)/1000).toFixed(2)),scope,
  counts:{pass:results.filter(r=>r.status==='pass').length,fail:failed.length,not_tested:results.filter(r=>r.status==='not_tested').length},results},null,2)+'\n');
process.exitCode = failed.length ? 1 : 0;
