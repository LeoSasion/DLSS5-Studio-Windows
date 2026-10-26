/* Regression tests for real workbench state transitions. No browser, GPU or packages needed.
 * Run: node --test tests/frontend-state.cjs
 * Source slices isolate transport/clock boundaries; the implementation remains in app/web.
 */
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const web = path.join(__dirname, '..', 'app', 'web');
const read = name => fs.readFileSync(path.join(web, name), 'utf8').replace(/\r\n/g, '\n');
const studio = read('studio.js');
const enhancements = read('enhancements.js');
const playerSource = read('video-player.js');

function between(source, start, end) {
  const first = source.indexOf(start);
  assert.notEqual(first, -1, `Missing source boundary: ${start}`);
  const last = source.indexOf(end, first + start.length);
  assert.notEqual(last, -1, `Missing source boundary: ${end}`);
  return source.slice(first + start.length, last);
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }

test('switching jobs during an old poll schedules and executes the new poll', async () => {
  const oldResponse = deferred();
  const calls = [], timers = [], nodes = new Map();
  const context = vm.createContext({
    state: { job: 'old', busy: false }, polling: false, pollTimer: 0,
    apiRequest(url) {
      calls.push(url);
      return url.endsWith('/old') ? oldResponse.promise : Promise.resolve({ id: 'new', status: 'queued' });
    },
    setTimeout(callback, delay) { timers.push({ callback, delay, cancelled: false }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].cancelled = true; },
    $(selector) { if (!nodes.has(selector)) nodes.set(selector, {}); return nodes.get(selector); },
    refreshControls() {}, showProgress() {},
  });
  vm.runInContext('function schedulePoll(' + between(studio,
    'function schedulePoll(', 'async function submitJob('), context);

  const inFlight = context.poll('old');
  context.state.job = 'new';
  await context.poll('new');
  assert.equal(timers.length, 1, 'The new job needs a retry even while the old request is pending');
  assert.ok(timers[0].delay > 0 && timers[0].delay <= 500);
  oldResponse.resolve({ id: 'old', status: 'done', log: 'stale result' });
  await inFlight;
  assert.equal(context.polling, false);
  assert.equal(context.state.job, 'new');
  assert.equal(nodes.get('#job-log'), undefined, 'A stale response must not update the workbench');

  await timers[0].callback();
  assert.deepEqual(calls, ['/api/jobs/old', '/api/jobs/new']);
  assert.equal(context.state.jobRecord.id, 'new');
  assert.equal(context.state.busy, true);
  assert.ok(timers.some(timer => !timer.cancelled && timer.delay === 1200), 'The new active job continues polling');
});

function makePlayer() {
  const context = vm.createContext({ window: {} });
  vm.runInContext(playerSource, context, { filename: 'app/web/video-player.js' });
  const player = Object.create(context.window.StudioVideoPlayer.prototype);
  Object.assign(player, {
    assetId: 'source', view: 'compare', frame: null, buffering: false,
    source: { paused: true, currentTime: 10, playbackRate: 1 },
    result: { readyState: 2, duration: 12, seeking: false, currentTime: 0, pause() {}, load() {} },
    resultUrl: 'clip-A', resultOffset: 8, resultFailed: false,
    exactMapping: { assetId: 'source', resultUrl: 'clip-A', sourceClock: 10, resultTime: 2 },
    paint() {},
  });
  return player;
}

test('a new result URL or offset clears an old exact-frame mapping', () => {
  const player = makePlayer();
  player.setResult('clip-B', 0, true);
  assert.equal(player.exactMapping, null);
  player.sync(true);
  assert.equal(player.result.currentTime, 10, 'The 0–12s result must show source 10s, not old clip result 2s');
  player.exactMapping = { assetId: 'source', resultUrl: 'clip-B', sourceClock: 10, resultTime: 2 };
  player.setResult('clip-B', 4, true);
  assert.equal(player.exactMapping, null, 'Changing only the result offset also invalidates mapping');
});

test('sync ignores a mapping owned by another result even for the same asset and source time', () => {
  const player = makePlayer();
  player.resultUrl = 'clip-B';
  player.resultOffset = 0;
  player.sync(true);
  assert.equal(player.result.currentTime, 10);
});

test('an asynchronously prepared HDR display source preserves selection, result and pending time', () => {
  const player=makePlayer();
  Object.assign(player,{displayUrl:null,sourceLength:12,pendingSourceTime:null,view:'original',locked:false,
    selection:{start:2,end:8},pause(){this.source.paused=true},paintTransport(){},clearFrame(){}});
  Object.assign(player.source,{readyState:0,duration:NaN,currentTime:0,load(){this.loaded=true},removeAttribute(){}});
  player.setSource({id:'source',url:'/original-hdr',duration:12,preview_status:'preparing'});
  assert.equal(player.sourcePreparing,true);
  player.seek(5);
  assert.equal(player.pendingSourceTime,5);
  assert.equal(player.source.currentTime,0);
  player.setSource({id:'source',url:'/original-hdr',preview_url:'/sdr-display-proxy',duration:12,preview_status:'ready'});
  assert.equal(player.source.src,'/sdr-display-proxy');
  assert.equal(player.pendingSourceTime,5);
  assert.deepEqual(player.selection,{start:2,end:8});
  assert.equal(player.resultUrl,'clip-A');
  assert.equal(player.sourcePreparing,false);
});

function frameStepper() {
  const requests = [], seeks = [];
  const context = vm.createContext({
    state: { mode: 'video', asset: { id: 'source-A' }, output: { id: 'job-A' } },
    stepRevision: 0,
    videoPlayer: {
      source: { currentTime: 10 }, fps: 30, pause() {},
      seek(time) { seeks.push(time); this.source.currentTime = time; },
    },
    apiRequest(url) { const response = deferred(); requests.push({ url, response }); return response.promise; },
  });
  const body = between(enhancements, "  document.addEventListener('keydown',async event=>{", '\n  });\n  const coverage');
  const handler = vm.runInContext('(async event=>{' + body + '\n})', context);
  const event = () => ({ altKey: true, key: 'ArrowRight', target: { closest: () => null }, preventDefault() {} });
  return { context, requests, seeks, step: () => handler(event()) };
}

test('an old frame-step response cannot seek a replacement source or result', async () => {
  for (const changed of ['asset', 'result']) {
    const run = frameStepper();
    const pending = run.step();
    if (changed === 'asset') run.context.state.asset = { id: 'source-B' };
    else run.context.state.output = { id: 'job-B' };
    run.requests[0].response.resolve({ frames: [{ source_time: 10.0333333 }] });
    await pending;
    assert.deepEqual(run.seeks, [], `Stale ${changed} frame table must be ignored`);
  }
});

test('the latest Alt-frame request wins when responses arrive out of order', async () => {
  const run = frameStepper();
  const first = run.step(), second = run.step();
  run.requests[1].response.resolve({ frames: [{ source_time: 10.04 }] });
  await second;
  run.requests[0].response.resolve({ frames: [{ source_time: 10.02 }] });
  await first;
  assert.equal(run.seeks.length, 1);
  assert.ok(Math.abs(run.seeks[0] - 10.04001) < 1e-8, 'Only the latest mapping may move the source');
});

const pendingSource = '  const pendingKey=' + between(enhancements, '  const pendingKey=', '  const clamp=');
function batchContext({ storage = new Map(), request, immediateTimers = true } = {}) {
  const writes = [], notices = [], timers = [], events = [];
  const foreground = { job: 'foreground-job', busy: true, pendingPayload: { request_id: 'foreground-request' } };
  const context = vm.createContext({
    state: foreground,
    localStorage: {
      getItem(key) { return storage.get(key) ?? null; },
      setItem(key, value) { storage.set(key, value); writes.push({ key, value }); },
    },
    apiRequest: request,
    showNotice(text) { notices.push(text); },
    setTimeout(callback, delay) {
      timers.push({ callback, delay });
      if (immediateTimers) queueMicrotask(callback);
      return timers.length;
    },
    batchBusy: false, refresh() {},
    window: { dispatchEvent(event) { events.push(event.type); } },
    Event: class Event { constructor(type) { this.type = type; } },
  });
  vm.runInContext(pendingSource, context, { filename: 'app/web/enhancements.js:pending-queue' });
  return { context, storage, writes, notices, timers, events, foreground,
    key: vm.runInContext('pendingKey', context),
    pending: () => plain(vm.runInContext('pendingBatch', context)),
    enqueue: payload => context.enqueueConfirmed(payload),
  };
}

test('accepted-but-lost batch responses retry the same UUID and clear durable confirmation state', async () => {
  const accepted = new Map(), requests = [];
  let run;
  run = batchContext({ request: async (url, options) => {
    assert.equal(url, '/api/jobs');
    const payload = JSON.parse(options.body);
    requests.push(payload);
    assert.deepEqual(JSON.parse(run.storage.get(run.key)), [payload], 'Unconfirmed submission is durable before HTTP starts');
    if (!accepted.has(payload.request_id)) accepted.set(payload.request_id, { id: 'accepted-job' });
    if (requests.length === 1) throw new TypeError('The server accepted the POST but its response was lost');
    return accepted.get(payload.request_id);
  } });
  const original = plain(run.foreground);
  const payload = { asset_id: 'batch-asset', request_id: 'stable-batch-uuid', job_kind: 'full', intensity: 1.5 };
  const result = await run.enqueue(payload);
  assert.equal(result.id, 'accepted-job');
  assert.equal(accepted.size, 1);
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[0], requests[1], 'Retry must retain the complete original payload and request UUID');
  assert.ok(run.timers[0].delay > 0);
  assert.deepEqual(run.pending(), []);
  assert.deepEqual(JSON.parse(run.storage.get(run.key)), []);
  assert.deepEqual(plain(run.foreground), original, 'Batch confirmation never takes over the foreground task');
  assert.equal(JSON.parse(run.writes[0].value)[0].request_id, payload.request_id);
});

test('page reload runs real pending-batch recovery and confirms the previously accepted UUID', async () => {
  const payload = { asset_id: 'batch-asset', request_id: 'accepted-before-reload', job_kind: 'full' };
  const bootstrap = batchContext({ request: async () => ({ id: 'unused' }) });
  const storage = new Map([[bootstrap.key, JSON.stringify([payload])]]);
  const requests = [];
  const run = batchContext({ storage, immediateTimers: false, request: async (url, options) => {
    requests.push(JSON.parse(options.body));
    return { id: 'already-accepted-job' };
  } });
  assert.deepEqual(run.pending(), [payload]);
  const start = enhancements.indexOf('  if(pendingBatch.length)setTimeout(async()=>{');
  const endMarker = '\n  },0);';
  const end = enhancements.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, 'Real startup recovery block is present');
  vm.runInContext(enhancements.slice(start, end + endMarker.length), run.context);
  assert.equal(run.timers.length, 1);
  assert.equal(run.timers[0].delay, 0);
  await run.timers[0].callback();
  assert.deepEqual(requests, [payload]);
  assert.deepEqual(run.pending(), []);
  assert.deepEqual(JSON.parse(storage.get(run.key)), []);
  assert.equal(run.context.batchBusy, false);
  assert.deepEqual(run.events, ['studio-jobs-changed']);
  assert.equal(run.foreground.job, 'foreground-job');
});

test('a definite batch validation rejection removes its pending record without retrying forever', async () => {
  let requests = 0;
  const error = Object.assign(new Error('invalid settings'), { status: 400 });
  const run = batchContext({ request: async () => { requests++; throw error; } });
  await assert.rejects(run.enqueue({ asset_id: 'asset', request_id: 'rejected-uuid' }), value => value === error);
  assert.equal(requests, 1);
  assert.equal(run.timers.length, 0);
  assert.deepEqual(run.pending(), []);
  assert.deepEqual(JSON.parse(run.storage.get(run.key)), []);
});
