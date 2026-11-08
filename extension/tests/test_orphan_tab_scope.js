/* 「跨域 HLS 清单拿不到 tabId」回归测试（node 直跑，chrome.* 用桩）。
 *
 * 2026-11-08 实测：nmwchxx.com 播放页的 index.m3u8 由 hls.js 用跨域 fetch 拉取，
 * Chrome 给 webRequest 的 details.tabId = -1。旧实现把条目塞进「无标签页」分库，
 * 而 popup 只查当前标签页 →「视频在播、扩展却提示没嗅探到」，桌面端 items 库却
 * 已经收到该条目（page_title 为空、page_url 退化成 initiator 即为此分支的特征）。
 *
 * 参考猫抓「抓到的资源一律不丢」：归属不明时归到当前激活标签页；并为升级前
 * 已落在 -1 分库的存量条目提供同站兜底，用户不必刷新重播就能看见。
 *
 * 运行：node extension/tests/test_orphan_tab_scope.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let passes = 0;
let failures = 0;
function eq(actual, expect, msg) {
  if (actual === expect) { passes++; return; }
  failures++;
  console.error('FAIL: ' + msg + '（期望 ' + JSON.stringify(expect) +
    '，实际 ' + JSON.stringify(actual) + '）');
}
function ok(cond, msg) { eq(!!cond, true, msg); }

const L = {};
const TAB_URL = {
  33: 'https://nmwchxx.com/vod/play/116537/1/877419',
};

globalThis.fetch = () => Promise.resolve({
  ok: true, status: 200, json: () => Promise.resolve({ ok: true, state: 'idle' }),
});

globalThis.chrome = {
  webRequest: {
    onSendHeaders: { addListener: (fn) => { L.sendHeaders = fn; } },
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
      if (cb2) cb2([{ id: 33, url: TAB_URL[33], title: '金牌影院' }]);
    },
    get: (id, cb) => cb({ id: id, title: '金牌影院', url: TAB_URL[id] || 'https://nmwchxx.com/' }),
    create: () => {}, update: () => {},
    onRemoved: { addListener: (fn) => { L.tabRemoved = fn; } },
    onActivated: { addListener: (fn) => { L.tabActivated = fn; } },
    onUpdated: { addListener: (fn) => { L.tabUpdated = fn; } },
  },
  runtime: {
    lastError: null,
    getManifest: () => ({ version: '1.0.56' }),
    onMessage: { addListener: (fn) => { L.message = fn; } },
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
  },
  alarms: { create: () => {}, onAlarm: { addListener: (fn) => { L.alarm = fn; } } },
};

globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };

const src = fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8');
vm.runInThisContext(src, { filename: 'background.js' });

ok(typeof L.completed === 'function' && typeof L.message === 'function',
  'background.js 已注册 onCompleted / onMessage 监听器');

const M3U8 = 'https://ppvod011.blbtgg.com/splitOut/20250222/643014/index.m3u8?auth_key=abc';
const PAGE = 'https://nmwchxx.com/vod/play/116537/1/877419';

let rid = 0;
function complete(tabId, url, mime, initiator) {
  L.completed({
    url: url, type: 'xmlhttprequest', tabId: tabId,
    requestId: 'r' + (++rid),
    initiator: initiator || 'https://nmwchxx.com',
    responseHeaders: [{ name: 'Content-Type', value: mime }],
  });
}
function mainFrame(tabId, url) {
  L.completed({
    url: url, type: 'main_frame', tabId: tabId, requestId: 'r' + (++rid),
    initiator: '', responseHeaders: [{ name: 'Content-Type', value: 'text/html' }],
  });
}
function ask(payload) {
  let got = null;
  L.message(payload, {}, (r) => { got = r; });
  return got;
}

// 当前激活标签页 = 33，且主文档已把 tabPage[33] 记为播放页
globalThis.activeTabId = 33;
mainFrame(33, PAGE);
eq(globalThis.tabPage['33'], PAGE, '主文档请求已记录当前页 URL');

// ---- 1) 根因：tabId=-1 的跨域 HLS 清单必须归到当前标签页 ----
complete(-1, M3U8, 'application/vnd.apple.mpegurl');
eq(globalThis.store.list(33).length, 1, '★ tabId=-1 的 m3u8 已归入当前标签页（弹窗可见）');
eq(globalThis.store.list(33)[0].kind, 'playlist', '条目判定为 playlist');
eq(globalThis.store.list(-1).length, 0, '★ 不再产生「无标签页」分库条目');

// ---- 2) getState 能取到（用户打开弹窗不再显示空状态）----
const st = ask({ type: 'getState', tabId: 33 });
ok(st && st.items.length === 1, '★ getState 返回该 m3u8');
ok(st && st.items[0].url === M3U8, 'getState 条目 URL 正确');

// ---- 3) 正常 tabId 的请求仍走原分库（不回归）----
complete(22, 'https://cdn.example.com/normal.mp4', 'video/mp4', 'https://example.com');
eq(globalThis.store.list(22).length, 1, '正常 tabId 仍写入自己的分库');
eq(globalThis.store.list(33).length, 1, '正常请求不会串进当前标签页');

// ---- 4) 存量兜底：修复前已落在 -1 分库的条目，同站才并入 ----
globalThis.store.clearTab(33);
globalThis.store.add(-1, {
  url: 'https://ppvod011.blbtgg.com/old/index.m3u8', mime: 'application/vnd.apple.mpegurl',
  referer: 'https://nmwchxx.com/', pageUrl: 'https://nmwchxx.com/', ts: 1,
});
globalThis.store.add(-1, {
  url: 'https://other-site.com/ad.mp4', mime: 'video/mp4',
  referer: 'https://other-site.com/', pageUrl: 'https://other-site.com/', ts: 1,
});
eq(globalThis.store.list(33).length, 0, '存量场景：当前标签页分库为空');
const st2 = ask({ type: 'getState', tabId: 33 });
ok(st2 && st2.items.length === 1, '★ 存量兜底：同站旧条目并入，异站条目不混入');
ok(st2 && st2.items[0].url.indexOf('blbtgg.com') >= 0, '并入的是本页已抓到的 m3u8');

// 换到别的站时，不该把 nmwchxx 的存量串过去
globalThis.tabPage['44'] = 'https://another.example/video/1';
const st3 = ask({ type: 'getState', tabId: 44 });
ok(st3 && st3.items.length === 0, '★ 其它站页面不会串入本页存量条目');

// ---- 5) 变异测试：把兜底行改成恒假，断言必须变红 ----
const mutated = src.replace(
  'if (tabId <= 0 && activeTabId > 0) tabId = activeTabId;',
  'if (false) tabId = activeTabId;'
);
ok(mutated !== src, '变异替换命中了兜底行');
vm.runInThisContext(mutated, { filename: 'background_mutated.js' });
globalThis.activeTabId = 33;
complete(-1, 'https://cdn.example.com/mutated.m3u8', 'application/vnd.apple.mpegurl');
eq(globalThis.store.list(33).length, 0,
  '★ 变异后：-1 请求不再归入当前标签页（证明兜底行确为修复点）');
ok(globalThis.store.list(-1).some((i) => i.url.indexOf('mutated.m3u8') >= 0),
  '★ 变异后：条目重新落进看不见的 -1 分库');

console.log('test_orphan_tab_scope: ' + passes + ' passed, ' + failures + ' failed');
process.exit(failures ? 1 : 0);
