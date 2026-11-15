/* 「响应头一到达就入库」回归测试（node 直跑，chrome.* 用桩）。
 *
 * 2026-11-15 实测：7.9MB 的 MP3 直链，响应头 3.1s 到达、整段请求完成要 52.1s。
 * 旧实现只在 webRequest.onCompleted 入库 ⇒ 用户播放到 0:23 时弹窗仍显示
 * 「当前页面没有嗅探到媒体流」，旁边那个扩展在响应头阶段就列出了，体感慢一大截。
 * 本测试钉住：早捕已注册 / 非媒体不早捕 / onCompleted 幂等不重复 /
 * 早捕不删 headerMeta（Cookie、Referer 留给完成阶段）。
 *
 * 运行：node extension/tests/test_early_capture.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let passes = 0, failures = 0;
function eq(actual, expect, msg) {
  if (actual === expect) { passes++; return; }
  failures++;
  console.error('FAIL: ' + msg + '（期望 ' + JSON.stringify(expect) +
    '，实际 ' + JSON.stringify(actual) + '）');
}
function ok(cond, msg) { eq(!!cond, true, msg); }

const L = {};
globalThis.fetch = () => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve({ ok: true, state: 'idle' }),
});
globalThis.chrome = {
  webRequest: {
    onSendHeaders: { addListener: (fn) => { L.sendHeaders = fn; } },
    onResponseStarted: { addListener: (fn) => { L.responseStarted = fn; } },
    onCompleted: { addListener: (fn) => { L.completed = fn; } },
    onErrorOccurred: { addListener: (fn) => { L.errorOccurred = fn; } },
  },
  storage: {
    local: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); }, remove: (_k, cb) => { if (cb) cb(); } },
    session: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); } },
  },
  action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {} },
  tabs: {
    query: (q, cb) => {
      const cb2 = typeof q === 'function' ? q : cb;
      if (cb2) cb2([{ id: 33, url: 'https://other-bj.kuwo.cn/x.mp3', title: 'x.mp3' }]);
    },
    get: (id, cb) => cb({ id: id, title: 'x.mp3', url: 'https://other-bj.kuwo.cn/x.mp3' }),
    create: () => {}, update: () => {},
    onRemoved: { addListener: (fn) => { L.tabRemoved = fn; } },
    onActivated: { addListener: (fn) => { L.tabActivated = fn; } },
    onUpdated: { addListener: (fn) => { L.tabUpdated = fn; } },
  },
  runtime: {
    lastError: null,
    getManifest: () => ({ version: '1.0.58' }),
    onMessage: { addListener: (fn) => { L.message = fn; } },
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
  },
  alarms: { create: () => {}, onAlarm: { addListener: (fn) => { L.alarm = fn; } } },
};
globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };

const EXT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(EXT, 'background.js'), 'utf8');
vm.runInThisContext(src, { filename: 'background.js' });

ok(typeof L.responseStarted === 'function', 'background.js 已注册 webRequest.onResponseStarted');
ok(typeof L.completed === 'function', '原 onCompleted 监听器仍在');

globalThis.activeTabId = 33;
const MP3 = 'https://other-bj.kuwo.cn/8cbb93903b6288009b9b611c4a4a79926af9c4b8/lx/resource/n2/35/78/3775897390.mp3';

function headers(mime) {
  return [{ name: 'Content-Type', value: mime }, { name: 'Content-Length', value: '7975922' }];
}
function start(d) {
  L.responseStarted(Object.assign({
    url: MP3, type: 'main_frame', tabId: 33, requestId: 'r1',
    initiator: '', responseHeaders: headers('audio/mpeg'),
  }, d || {}));
}
function complete(d) {
  L.completed(Object.assign({
    url: MP3, type: 'main_frame', tabId: 33, requestId: 'r1',
    initiator: '', responseHeaders: headers('audio/mpeg'),
  }, d || {}));
}

// ① 请求头先到（Cookie / Referer 记进 headerMeta）
L.sendHeaders({
  requestId: 'r1', url: MP3,
  requestHeaders: [{ name: 'Cookie', value: 'sid=abc' }, { name: 'Referer', value: 'https://kuwo.cn/' }],
});

// ② 响应头到达即入库 —— 不等 52 秒后的 onCompleted
start();
eq(globalThis.store.list(33).length, 1, '★ 响应头到达就已有 1 条（不用等请求完成）');
eq(globalThis.store.list(33)[0].kind, 'media', 'MP3 判定为 media');
eq(globalThis.store.list(33)[0].size, 7975922, '响应头的 Content-Length 已带进条目');
eq(globalThis.store.list(33)[0].cookie, 'sid=abc', '早捕也带上请求头里的 Cookie');
ok(globalThis.headerMeta.has('r1'), '★ 早捕不删 headerMeta，完成阶段仍可用');

// ③ 请求完成再走一次：幂等，不重复、不丢字段
complete();
eq(globalThis.store.list(33).length, 1, '★ onCompleted 后仍只有 1 条（URL 去重）');
ok(!globalThis.headerMeta.has('r1'), '完成阶段才清理 headerMeta');

// ④ 非媒体请求不得早捕（否则会把整页资源全塞进列表）
L.responseStarted({
  url: 'https://cdn.example.com/logo.png', type: 'image', tabId: 33, requestId: 'r2',
  initiator: '', responseHeaders: headers('image/png'),
});
L.responseStarted({
  url: 'https://cdn.example.com/api/config.json', type: 'xmlhttprequest', tabId: 33, requestId: 'r3',
  initiator: '', responseHeaders: headers('application/json'),
});
L.responseStarted({
  url: 'https://cdn.example.com/index.html', type: 'other', tabId: 33, requestId: 'r4',
  initiator: '', responseHeaders: headers('text/html'),
});
eq(globalThis.store.list(33).length, 1, '★ 图片 / JSON / HTML 都不会被早捕');

// ⑤ HLS 清单同样秒出
L.responseStarted({
  url: 'https://vod.example.com/2026/index.m3u8?auth=1', type: 'xmlhttprequest', tabId: 33,
  requestId: 'r5', initiator: '', responseHeaders: headers('application/vnd.apple.mpegurl'),
});
eq(globalThis.store.list(33).length, 2, 'm3u8 清单也被早捕');
const m3u8Item = globalThis.store.list(33).filter((it) => it.url.indexOf('.m3u8') >= 0)[0];
eq(m3u8Item && m3u8Item.kind, 'playlist', '清单判定为 playlist');

// ⑥ getState 在「请求还没完成」时就能返回条目（弹窗不再空）
let st = null;
L.message({ type: 'getState', tabId: 33 }, {}, (r) => { st = r; });
ok(st && st.items.length >= 1, '★ 未完成请求时 getState 已能看到条目');

// ⑦ popup 首帧不再被端口探测串行阻塞（源码级契约）
const popup = fs.readFileSync(path.join(EXT, 'popup.js'), 'utf8');
const iGetEp = popup.lastIndexOf("chrome.runtime.sendMessage({ type: 'getEndpoint' }");
const iRefresh = popup.lastIndexOf('\nrefresh();', iGetEp);
ok(iRefresh >= 0 && iGetEp >= 0 && iRefresh < iGetEp,
  '★ refresh() 在初始化块里先于 getEndpoint 独立调用（首帧不等端口探测）');

// ⑧ 早捕异常不能影响原有 onCompleted 路径
L.responseStarted({ url: MP3, tabId: 33, requestId: 'r6', responseHeaders: null });
ok(typeof L.completed === 'function', '异常响应头未破坏原有监听器');

console.log((failures ? 'FAIL' : 'PASS') + '  ' + passes + ' passed / ' + failures + ' failed');
process.exit(failures ? 1 : 0);
