/* 「插件发过来是一串数字，要原名」回归测试（2026-11-13 用户实测）。
 *
 * 现象：扩展弹窗与桌面面板里显示 2044590383.mp3 / M5000039SjyS2Oznrv.mp3，
 *      下载后的文件名也是这串数字，而页面上明明写着「友谊之光 - Maria Cordero」。
 *
 * 根因（实测 flac.music.hi.cn 得到）：
 *   ① 该站是 React SPA，document.title 恒为「无损音乐下载」，不是歌名；
 *   ② 媒体直链是纯数字 ID / 哈希，且响应头 Content-Disposition 也是哈希名
 *      —— 靠 URL / 标准响应头拿原名都是死路；
 *   ③ 真正的原名只在**页面 DOM**（下载弹窗的「名称：xxx」），而「点击下载」用的是
 *      `<a href=直链 target="_blank">`，**没有 download 属性**；
 *   ④ 服务端 downloader.probe() 对直链短路，把 URL basename 当成 info["title"]，
 *      run_download 又用它覆盖 task.title ⇒ 任务名和落盘文件名全变数字。
 *
 * 修法：页面侧采集原名（pagewatch.js）→ 后台按 URL 关联（background.js::nameForUrl /
 *      noteMediaName）→ 随 ext-push 上报 title → 服务端 / 桌面端 / 下载器全链路透传。
 *
 * 本测试跑**真实 background.js**（chrome 桩），钉住最容易改坏的两类时序：
 *   名称先到再嗅到流 / 先嗅到流再补名称。服务端与下载器配套用例见
 *   server/tests/test_sniffer_media_name.py。
 *
 * 运行：node extension/tests/test_media_name.js
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

// ---- chrome.* 桩（与 test_page_title_lag.js 同款）----
const L = {};
const pushes = [];
globalThis.fetch = (url, opts) => {
  const u = String(url);
  if (u.indexOf('/api/sniffer/ext-push') >= 0) {
    pushes.push(JSON.parse((opts && opts.body) || '{}'));
  }
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve(u.indexOf('/api/sniffer/status') >= 0 ? { state: 'idle' } : { ok: true })
  });
};
globalThis.chrome = {
  webRequest: {
    onSendHeaders: { addListener: (fn) => { L.sendHeaders = fn; } },
    onCompleted: { addListener: (fn) => { L.completed = fn; } },
    onHeadersReceived: { addListener: () => {} },
    onErrorOccurred: { addListener: () => {} },
  },
  storage: {
    local: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); }, remove: (_k, cb) => { if (cb) cb(); } },
    session: { get: (_k, cb) => cb({}), set: (_o, cb) => { if (cb) cb(); } },
  },
  action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {}, setTitle: () => {}, setIcon: () => {} },
  tabs: {
    query: (q, cb) => { const f = typeof q === 'function' ? q : cb; if (f) f([]); },
    // 标签页标题是 SPA 恒定站点名，绝不是原名 —— 保持这个桩才能证明 title 是独立字段
    get: (id, cb) => cb({ id: id, title: '无损音乐下载', url: 'https://flac.music.hi.cn/' }),
    create: () => {}, update: () => {},
    onRemoved: { addListener: () => {} },
    onActivated: { addListener: () => {} },
    onUpdated: { addListener: () => {} },
  },
  runtime: {
    lastError: null,
    getManifest: () => ({ version: '1.0.57' }),
    onMessage: { addListener: (fn) => { L.message = fn; } },
    onInstalled: { addListener: () => {} }, onStartup: { addListener: () => {} },
    sendMessage: () => {}, getURL: () => '', reload: () => {},
  },
  alarms: { create: () => {}, clear: () => {}, get: (_n, cb) => cb(undefined), onAlarm: { addListener: () => {} } },
};
globalThis.importScripts = () => { require(path.join(__dirname, '..', 'sniff-core.js')); };
vm.runInThisContext(
  fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'),
  { filename: 'background.js' });

// 端口探测是异步的（30 个候选 base 逐个 fetch）；这里直接给一个确定基址，
// 让 pushToServer 在本测试用例里同步完成，断言不必等待事件循环。
globalThis.getEndpoint = (cb) => cb('http://127.0.0.1:8321');

ok(typeof L.completed === 'function', 'background.js 已注册 webRequest.onCompleted');
ok(typeof L.message === 'function', 'background.js 已注册 runtime.onMessage');
ok(typeof globalThis.nameForUrl === 'function', '★ 已提供 nameForUrl（嗅探时认领原名）');
ok(typeof globalThis.noteMediaName === 'function', '★ 已提供 noteMediaName（原名晚到补推）');

function sendName(name, href, tabId) {
  let got = null;
  L.message({ type: 'mediaName', name: name, href: href || '', ts: Date.now() },
    { tab: { id: tabId || 7 } }, (r) => { got = r; });
  return got;
}
function complete(url, tabId) {
  L.completed({
    url: url, requestId: 'req-' + Math.random(), tabId: tabId || 7, type: 'other',
    initiator: 'https://flac.music.hi.cn/',
    responseHeaders: [{ name: 'Content-Type', value: 'audio/mpeg' }]
  });
}
const lastItem = () => (pushes.length ? pushes[pushes.length - 1].items[0] : {});
const pushesFor = (u) => pushes.filter((p) => p.items && p.items[0] && p.items[0].url === u);

const NAME = '友谊之光 - Maria Cordero';
const U_A = 'https://cdn.x.com/2044590383.mp3';
const U_B = 'https://cdn.x.com/M5000039SjyS2Oznrv.flac?time=1794537501&sign=abc';

// ---- 1) 名称先到（用户先点「点击下载」），浏览器随后才发媒体请求 ----
eq(sendName(NAME, U_A, 7).ok, true, 'mediaName 消息有 ok 回执');
pushes.length = 0;
complete(U_A, 7);
eq(lastItem().title, NAME, '★ 原名随嗅探条目一起推给桌面端（不是 URL basename）');
eq(lastItem().page_title, '无损音乐下载', 'page_title 仍是标签页标题，两个字段互不干扰');
eq(lastItem().url, U_A, 'URL 原样保留，原名不影响下载');

// ---- 2) 先嗅到流（无原名），页面随后才报出名字 → 必须补推 ----
pushes.length = 0;
complete(U_B, 8);
eq(lastItem().title || '', '', '首推时还没有原名，允许为空');
eq(sendName('海阔天空', U_B, 8).ok, true, '晚到的 mediaName 回执 ok');
eq(pushesFor(U_B).length, 2, '★ 原名晚到后补推一次（同名 URL，不新增条目）');
eq(lastItem().title, '海阔天空', '★ 补推只更新 title');
eq(lastItem().url, U_B, '补推 URL 不变，服务端按 URL 去重');

// ---- 3) 一次性查询串差异不影响关联（time/sign 每次都不同）----
const U_C = 'https://cdn.x.com/song998.flac';
eq(sendName('光辉岁月', U_C + '?sign=xyz', 9).ok, true, '带查询串的名称线索被接受');
pushes.length = 0;
complete(U_C, 9);
eq(lastItem().title, '光辉岁月', '★ 同 origin+pathname 即认作同一媒体（忽略 time/sign）');

// ---- 4) 带 href 却没对上的名称，绝不串到别的媒体（上一首歌的名字不许串到这一首）----
pushes.length = 0;
complete('https://cdn.x.com/other-555.mp3', 11);
eq(lastItem().title || '', '', '★ 带下载链接却不匹配的名称不参与时间窗口兜底');

// ---- 5) 没有 href 的线索仍可时间窗口兜底（window.open / Blob 下发的场景）----
eq(sendName('无链接下载', '', 12).ok, true, '无 href 的名称线索被接受');
pushes.length = 0;
complete('https://cdn.x.com/blob-666.mp3', 12);
eq(lastItem().title, '无链接下载', '★ 无 href 线索走同标签页时间窗口兜底');

// ---- 6) 过期线索绝不串进新条目（把线索时间戳直接改旧，等价于 90s 后）----
const hints = globalThis.nameHints;
ok(Array.isArray(hints) && hints.length > 0, '★ nameHints 线索表存在');
hints.forEach((h) => { h.ts -= 90000; });
pushes.length = 0;
complete('https://cdn.x.com/brand-new-777.mp3', 10);
eq(lastItem().title || '', '', '★ 超 60s 的旧名称不许挂到新条目上');

// ---- 5) sniff-core 层：按 URL 补写 + 时间窗口兜底 ----
const CORE = globalThis.VDLSniffCore;
const s = new CORE.TabStores();
s.add(1, { url: 'https://a.com/1.mp3', mime: 'audio/mpeg', ts: 1000 });
const hit = s.setTitleByUrl('https://a.com/1.mp3', '直接补写');
eq(hit && hit.title, '直接补写', 'sniff-core 按 URL 补写原名');
const s2 = new CORE.TabStores();
s2.add(2, { url: 'https://b.com/2.mp3', mime: 'audio/mpeg', ts: 2000 });
const h2 = s2.setTitleRecent(2, '窗口兜底', 2010, 45);
eq(h2 && h2.title, '窗口兜底', 'sniff-core 时间窗口兜底');
const h3 = s2.setTitleRecent(2, '过期不该补', 3000, 45);
eq(h3, null, '超窗口不补');

// ---- 6) 契约：popup 手动发送与显示也必须带原名 ----
const popup = fs.readFileSync(path.join(__dirname, '..', 'popup.js'), 'utf8');
ok(/title:\s*it\.title\s*\|\|\s*''/.test(popup), '★ popup /api/sniffer/send 载荷带 title');
ok(/var realName = String\(it\.title \|\| ''\)\.trim\(\);/.test(popup), '★ popup 读取 it.title 作为媒体原名');
ok(/url\.textContent = realName \|\| it\.url;/.test(popup), '★ popup 列表原名优先于 URL 显示');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
eq(manifest.version, '1.0.57', '扩展版本已升到 1.0.57');

console.log(`\n媒体原名回归测试：通过 ${passes} / 失败 ${failures}`);
if (failures) process.exit(1);
