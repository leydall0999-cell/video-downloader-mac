#!/usr/bin/env node
/**
 * 操作引导「怎么用」守卫（2026-10-09）
 *
 * 背景：用户要求「给每个功能加上操作引导」。落地形态 = 每个功能视图里一条
 * 原生 <details class="howto"> 折叠条，首次进入该功能时由 app.js 自动展开一次。
 *
 * 本守卫钉死四件事，任何一条被改坏都会立刻变红：
 *   ① 覆盖完整 —— 18 个功能入口（含去水印 4 个子面板）每个都有且仅有一条引导；
 *   ② 落在对的位置 —— 每条引导必须在自己那个视图/子面板的区间内，不能串台；
 *   ③ 文案不脱节 —— 引导里用「」引出来的每个控件名，都必须在 index.html / app.js
 *      里真实存在（改了按钮文案却忘了改引导 → 立刻红）；
 *   ④ 机制在位 —— 首次自动展开的 JS 逻辑 + 折叠样式 + 默认收起。
 *
 * 运行：node server/tests/test_howto_guide.mjs
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..', '..');
const REL = (p) => join(repoRoot, p);

const indexHtml = readFileSync(REL('web/index.html'), 'utf8');
const appJs = readFileSync(REL('web/app.js'), 'utf8');
const css = readFileSync(REL('web/styles.css'), 'utf8');

// ⚠️ 「出处库」必须先把引导条自身剥掉，否则断言会自证成真：
//    引导里写什么，库里就有什么 —— 把「解析链接」改成「解析视频啊」也照样通过。
const HOWTO_BLOCK_RE = /<details class="howto"[\s\S]*?<\/details>/g;
const hay = indexHtml.replace(HOWTO_BLOCK_RE, '') + '\n' + appJs;
// ⚠️ JS 断言也必须剥掉注释：注释里同样写着 d.closest('[hidden]') 之类的字样，
//    代码被改坏而注释留着时，纯全文匹配会误判为通过。
const appJsCode = appJs
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/(^|[^:])\/\/[^\n]*/g, '$1');

let count = 0;
const ok = (cond, msg) => { count++; assert.ok(cond, msg); };
const eq = (a, b, msg) => { count++; assert.equal(a, b, msg); };

/* ------------------------------------------------------------------ */
/* 期望清单：key -> [所属区间的前锚, 后锚]                              */
/* 前锚取该功能容器的开标签，后锚取下一个容器的开标签 —— 用字符串下标    */
/* 夹逼就能确定「这条引导到底在谁的肚子里」，比解析 DOM 树更直接。       */
/* ------------------------------------------------------------------ */
const EXPECT = new Map([
  ['download',      ['class="hero-link-wrap"', 'class="panel input-panel"']],
  ['uploadconvert', ['id="uploadConvertView"', 'id="musicConvertView"']],
  ['musicconvert',  ['id="musicConvertView"', 'id="imageConvertView"']],
  ['imageconvert',  ['id="imageConvertView"', 'id="compressView"']],
  ['compress',      ['id="compressView"', 'id="srView"']],
  ['sr',            ['id="srView"', 'id="shareView"']],
  ['share',         ['id="shareView"', 'id="pageView"']],
  ['page',          ['id="pageView"', 'id="bridgeView"']],
  ['bridge',        ['id="bridgeView"', 'id="subtitleView"']],
  ['subtitle',      ['id="subtitleView"', 'id="dwView"']],
  ['dw_img',        ['id="dwImgPane"', 'id="dwPdfPane"']],
  ['dw_pdf',        ['id="dwPdfPane"', 'id="dwVideoPane"']],
  ['dw_video',      ['id="dwVideoPane"', 'id="dwMattingPane"']],
  ['matting',       ['id="dwMattingPane"', 'id="idphotoView"']],
  ['idphoto',       ['id="idphotoView"', 'id="profileView"']],
  ['library',       ['id="libraryView"', 'id="commentaryView"']],
  ['commentary',    ['id="commentaryView"', 'id="subscribeView"']],
  ['subscribe',     ['id="subscribeView"', 'id="torrentView"']],
  ['torrent',       ['id="torrentView"', 'id="libModal"']],
]);

/* ---------------- ① 每条引导的结构 ---------------- */
const blocks = [...indexHtml.matchAll(
  /<details class="howto" data-howto="([^"]+)"[\s\S]*?<\/details>/g,
)].map((m) => ({ key: m[1], html: m[0], at: m.index }));

eq(blocks.length, EXPECT.size,
  `引导条数量应恰好 ${EXPECT.size} 条（每个功能一条），实际 ${blocks.length} 条`);

const gotKeys = blocks.map((b) => b.key);
for (const key of EXPECT.keys()) {
  ok(gotKeys.includes(key), `缺少功能「${key}」的操作引导（该功能页没有引导条）`);
}
eq(new Set(gotKeys).size, gotKeys.length, `引导条 key 有重复：${gotKeys.join(', ')}`);

for (const b of blocks) {
  const tag = `<details class="howto" data-howto="${b.key}">`;
  ok(b.html.startsWith(tag), `「${b.key}」引导条开标签不完整（应含 class="howto" 与 data-howto）`);
  ok(/<summary class="howto-head">/.test(b.html),
    `「${b.key}」缺少 <summary class="howto-head"> —— 没有它就点不开折叠`);
  ok(/class="howto-title"/.test(b.html), `「${b.key}」缺少 howto-title（用户看不到「怎么用 · xx」字样）`);
  ok(/<div class="howto-body">/.test(b.html), `「${b.key}」缺少 howto-body 内容容器`);
  ok(/<ol class="howto-steps">/.test(b.html), `「${b.key}」缺少 howto-steps 有序步骤列表`);
  ok(/class="howto-note"/.test(b.html), `「${b.key}」缺少 howto-note（注意事项）—— 每个功能至少要有边界说明`);

  const lis = [...b.html.matchAll(/<li>([\s\S]*?)<\/li>/g)].map((m) => m[1]);
  ok(lis.length >= 3, `「${b.key}」只有 ${lis.length} 步，操作引导至少要 3 步才讲得清`);
  lis.forEach((li, i) => {
    const plain = li.replace(/<[^>]+>/g, '').trim();
    ok(plain.length >= 8, `「${b.key}」第 ${i + 1} 步是空壳（剥掉标签后只剩 "${plain}"）`);
  });

  // 默认必须收起：HTML 里写死 open 会让「首次才展开」这个设计彻底失效
  ok(!/<details[^>]*\sopen[\s>]/.test(b.html),
    `「${b.key}」写了默认 open —— 引导条必须默认收起，由首次进入时才自动展开`);
}

/* ---------------- ② 每条落在自己的视图区间内 ---------------- */
function between(preAnchor, postAnchor) {
  const a = indexHtml.indexOf(preAnchor);
  const b = indexHtml.indexOf(postAnchor, a + 1);
  if (a < 0 || b < 0) return null;
  return { a, b, text: indexHtml.slice(a, b) };
}

for (const [key, [pre, post]] of EXPECT) {
  const seg = between(pre, post);
  ok(seg !== null, `定位「${key}」所在区间失败（锚点 "${pre}" 或 "${post}" 在 index.html 里找不到）`);
  ok(seg.text.includes(`data-howto="${key}"`),
    `「${key}」的引导条不在自己所属的视图区间内（跑到别的功能页去了）`);
  // 反向：这段区间里不该混进别的功能的引导
  for (const other of EXPECT.keys()) {
    if (other === key) continue;
    ok(!seg.text.includes(`data-howto="${other}"`),
      `「${key}」区间内混入了「${other}」的引导条（两者串台，切页时会同时展开）`);
  }
}

/* ---------------- ③ 引导里的控件名必须真实存在 ---------------- */
for (const b of blocks) {
  const plain = b.html.replace(/<[^>]+>/g, '');
  const quoted = [...plain.matchAll(/「([^」]+)」/g)].map((m) => m[1]);
  ok(quoted.length >= 2,
    `「${b.key}」引导里只用「」标注了 ${quoted.length} 个控件名 —— 步骤没有指向真实按钮，用户找不到点哪里`);
  for (const q of quoted) {
    ok(hay.includes(q),
      `「${b.key}」引导里引用的「${q}」在 index.html / app.js 中不存在 —— ` +
      '文案与真实控件脱节（多半是按钮文案改了却没同步引导）');
  }
}

/* ---------------- ④ 首次自动展开的机制 ---------------- */
ok(/function howtoAutoOpen\s*\(/.test(appJsCode),
  'app.js 里找不到 howtoAutoOpen() —— 首次进入功能时不会自动展开引导');
ok(/const HOWTO_SEEN_KEY = 'vdl_howto_seen';/.test(appJsCode),
  '已读记录用的 localStorage key 不见了（换了 key 会导致所有用户被重新弹一遍引导）');

// ⚠️ 以下断言必须限定在 howtoAutoOpen 的函数体内部：
//    `d.closest('[hidden]')` 与 `details.howto[data-howto]` 在 howtoSyncButtons /
//    按钮点击处理器里也会出现 —— 全文匹配时，函数体被改坏也能靠别处的同名字样蒙混过关。
const howtoFnStart = appJsCode.indexOf('function howtoAutoOpen()');
const howtoFnEnd = appJsCode.indexOf('\n  }\n', howtoFnStart);
const howtoFn = howtoFnStart > 0
  ? appJsCode.slice(howtoFnStart, howtoFnEnd > 0 ? howtoFnEnd : howtoFnStart + 1500)
  : '';
ok(howtoFn.length > 200, '定位 howtoAutoOpen 的函数体失败（找不到起止位置）');
ok(/localStorage\.getItem\(HOWTO_SEEN_KEY\)/.test(howtoFn)
   && /localStorage\.setItem\(HOWTO_SEEN_KEY/.test(howtoFn),
  'howtoAutoOpen 内部没有同时读写已读记录 —— 只读不写会每次进页面都弹');
ok(/d\.closest\('\[hidden\]'\)/.test(howtoFn),
  'howtoAutoOpen 内部没有用 closest([hidden]) 判断可见性 —— 会把其它隐藏视图的引导也一并展开');
ok(/details\.howto\[data-howto\]/.test(howtoFn),
  'howtoAutoOpen 的选取器不再是 details.howto[data-howto] —— 引导条会被漏选或误选');
ok(/seen\.indexOf\(k\)\s*>=\s*0/.test(howtoFn),
  'howtoAutoOpen 内部没有「已读过就跳过」的判断 —— 每次切页都会重新弹引导');
// 🔴 已读的条必须**显式收起**，不能只 return：SPA 不重建 DOM，上次展开着离开
//    再切回来仍是展开的，就不满足「除第一次外每次进入都默认收起」。
ok(/if \(d\.open\) d\.open = false;/.test(howtoFn),
  'howtoAutoOpen 对已读引导条只跳过、没有显式收起 —— 上次展开着离开，切回来还是展开的（应默认收起）');

const swDef = appJsCode.indexOf('function switchView(');
const swCall = appJsCode.indexOf('try { howtoAutoOpen(); }');
ok(swDef > 0 && swCall > swDef,
  'switchView 里没有调用 howtoAutoOpen() —— 切到某功能页时引导不会展开');
const fns = appJsCode.match(/function howtoAutoOpen\s*\(/g) || [];
eq(fns.length, 1, `howtoAutoOpen 定义了 ${fns.length} 次（应恰好 1 次）`);

/* ---------------- ⑤ 样式必须能折叠成一行 ---------------- */
// 先剥掉注释再取规则体：既支持「选择器独占一行」的多行写法，也支持
// 「.howto-body { padding: ...; }」这种单行写法；同时天然排除「规则被注释掉」的假绿。
const cssCode = css.replace(/\/\*[\s\S]*?\*\//g, '');
function ruleBody(selector) {
  const i = cssCode.indexOf(selector);
  if (i < 0) return null;
  const s = cssCode.indexOf('{', i);
  if (s < 0) return null;
  const e = cssCode.indexOf('}', s);
  if (e < 0) return null;
  const body = cssCode.slice(s + 1, e).trim();
  return body.length > 0 ? body : null;   // 空规则体形同虚设，判失败
}

const need = [
  ['.howto {', /border-radius|border:/, '引导条外框（缺了会变成裸文字，跟正文糊在一起）'],
  ['.howto-head {', /cursor:\s*pointer/, '引导条标题行（缺了用户不知道这行可以点）'],
  ['.howto-head::-webkit-details-marker {', /display:\s*none/, '去掉原生三角（否则和自绘箭头重复出现）'],
  ['.howto[open] .howto-caret {', /transform/, '展开时箭头翻转'],
  ['.howto-body {', /padding/, '展开后的内容内边距'],
  ['.howto-steps li {', /font-size|line-height/, '步骤字号行高'],
  ['.howto-note {', /border-top|font-size/, '注意事项样式'],
  ['.howto-btn {', /margin-left:\s*auto/, '右上角「?」按钮（少了 margin-left:auto 会紧贴标题文字而不是靠最右）'],
  ['.howto-btn.is-active {', /color|background/, '「?」按钮的展开态（用户看不出引导正开着）'],
  ['#downloadView .hero .howto-btn {', /position:\s*absolute/, '下载页 hero 是居中布局，「?」按钮必须绝对定位到右上角'],
  ['.lib-head > .guide-title {', /margin-right/, 'h2 吃掉左侧剩余空间，右侧的按钮组与「?」才会一起靠右'],
  ['.howto.is-flash {', /animation/, '点「?」展开时的闪烁提示'],
];
for (const [sel, re, why] of need) {
  const body = ruleBody(sel);
  ok(body !== null, `styles.css 里缺少可用规则 ${sel} —— ${why}`);
  ok(re.test(body), `styles.css 的 ${sel} 规则体缺少关键声明（${why}）`);
}
ok(/@keyframes howtoFlash\s*\{/.test(cssCode),
  'styles.css 缺少 howtoFlash 动画定义 —— 点「?」展开时不会有视线提示');

/* ---------------- ⑥ 右上角「?」按钮：随时重新查看引导 ---------------- */
// 背景：引导条只在首次进入时自动展开一次；没有这个按钮，用户忘了就没有入口。
const BTN_RE = /<button type="button" class="howto-btn"[^>]*>\?<\/button>/g;
const btnCount = (indexHtml.match(BTN_RE) || []).length;
eq(btnCount, 16,
  `「?」查看引导按钮应恰好 16 个（11 个纯标题视图 + 4 个 lib-head 视图 + 下载页），实际 ${btnCount} 个`);

const TITLE_IDS = ['ucTitle', 'musTitle', 'imgTitle', 'cpTitle', 'srTitle',
  'shareTitle', 'pageTitle', 'bridgeTitle', 'sbTitle', 'dwTitle', 'idpTitle'];
for (const id of TITLE_IDS) {
  const reBtn = new RegExp(`<h2 id="${id}"[^>]*>[\\s\\S]*?class="howto-btn"[\\s\\S]*?</h2>`);
  ok(reBtn.test(indexHtml),
    `「${id}」标题里没有「?」按钮 —— 该功能右上角缺少随时重看引导的入口`);
  const reSpan = new RegExp(
    `<h2 id="${id}"[^>]*>\\s*<span class="guide-title-text">[\\s\\S]*?</span>[\\s\\S]*?class="howto-btn"`);
  ok(reSpan.test(indexHtml),
    `「${id}」标题文案没有包进 <span class="guide-title-text"> —— ` +
    'JS 一旦用 textContent 改标题文案，会把「?」按钮一起抹掉');
}

for (const view of ['libraryView', 'commentaryView', 'subscribeView', 'torrentView']) {
  const vi = indexHtml.indexOf(`<section id="${view}"`);
  ok(vi > 0, `找不到 <section id="${view}">`);
  const end = indexHtml.indexOf('<details class="howto"', vi);
  const seg = indexHtml.slice(vi, end > 0 ? end : vi + 3000);
  ok(seg.includes('class="howto-btn"'),
    `「${view}」的头部（lib-head）里没有「?」按钮 —— 右上角缺少随时重看引导的入口`);
}

const heroStart = indexHtml.indexOf('<section class="hero"');
const heroEnd = indexHtml.indexOf('class="panel input-panel"');
ok(heroStart > 0 && heroEnd > heroStart, '定位下载页 hero 区间失败');
ok(indexHtml.slice(heroStart, heroEnd).includes('class="howto-btn"'),
  '下载页 hero 右上角没有「?」按钮');

// JS 侧：按钮能开合引导、状态能同步，且动态标题不会把按钮抹掉
ok(/function howtoSyncButtons\s*\(/.test(appJsCode),
  'app.js 缺少 howtoSyncButtons() —— 「?」按钮的显示与激活态不会更新');
ok(/e\.target\.closest\('\.howto-btn'\)/.test(appJsCode),
  'app.js 没有绑定「?」按钮的点击 —— 按钮点了没反应');
ok(/btn\.classList\.toggle\('is-active'/.test(appJsCode),
  '「?」按钮没有激活态同步逻辑（引导开着时按钮不会高亮）');
ok(/howtoScopeOf\s*=\s*\(btn\)\s*=>\s*btn\.closest\('#downloadView, section\[id\]'\)/.test(appJsCode),
  'howtoScopeOf 的作用域定位被改动 —— 去水印 4 个子面板共用一个按钮会找错引导');
ok(/addEventListener\('toggle',[\s\S]{0,700}?\}, true\)/.test(appJsCode),
  '缺少对 details toggle 的捕获监听（toggle 事件不冒泡，必须用捕获）—— 手动开合引导时按钮状态会不同步');
const swSync = appJsCode.indexOf('try { howtoSyncButtons(); }');
ok(swSync > swDef,
  'switchView 里没有调用 howtoSyncButtons() —— 切到另一功能时按钮状态会残留');
// 🔴 最要命的一条：这两个标题是 JS 动态改的，用 textContent 整体赋值会把按钮抹掉
ok(!/el\.ucTitle\.textContent\s*=/.test(appJsCode) && !/el\.dwTitle\.textContent\s*=/.test(appJsCode),
  '「视频格式转换」或「去水印」的标题又改回 textContent 整体赋值了 —— ' +
  'textContent 会清空 h2 的全部子节点，标题里的「?」按钮会被一起抹掉');
ok(/function setGuideTitle\s*\(/.test(appJsCode),
  'app.js 缺少 setGuideTitle() —— 动态标题改文案时应只替换 .guide-title-text 的文字');

/* ---------------- ⑦ 「✕ 关闭引导」：关掉整条消失，右上角「?」重新打开 ---------------- */
// 用户原话：「引导条打开要可以关闭，重新打开就点问号」。
const closeCount = (indexHtml.match(/class="howto-close"/g) || []).length;
eq(closeCount, EXPECT.size,
  `「✕ 关闭引导」按钮应恰好 ${EXPECT.size} 个（每条引导一个），实际 ${closeCount} 个`);

for (const b of blocks) {
  ok(/class="howto-close"/.test(b.html), `「${b.key}」没有「✕ 关闭引导」按钮 —— 用户打开了却关不掉`);
  ok(/class="howto-foot"/.test(b.html), `「${b.key}」缺少 howto-foot 容器`);
  const iSum = b.html.indexOf('</summary>');
  const iBody = b.html.indexOf('<div class="howto-body">');
  const iClose = b.html.indexOf('class="howto-close"');
  ok(iBody > 0 && iClose > iBody,
    `「${b.key}」的关闭按钮不在 .howto-body 内 —— 收起状态下也会露出来，且与标题行的开合点击打架`);
  ok(iSum > 0 && iClose > iSum,
    `「${b.key}」的关闭按钮落在 <summary> 里 —— 点它会同时触发标题行的开合`);
  // 关闭区文案里不得出现「」引用：③ 段会把引导条内所有「」当成"界面真实控件名"去校验，
  // 写个「问号按钮」之类进去会立刻假失败（属自证式噪音，不是真缺陷）。
  const footSeg = b.html.slice(b.html.indexOf('class="howto-foot"'));
  ok(!/「/.test(footSeg),
    `「${b.key}」的关闭区文案里出现了「」引用 —— 会被 ③ 段当成控件名去 index.html/app.js 里找，必假失败`);
}

const closeCssNeed = [
  ['.howto.is-closed {', /display:\s*none/, '关掉的引导条必须整条不显示（否则还占一行，等于没关掉）'],
  ['.howto-foot {', /display:\s*flex/, '关闭区布局（按钮与提示文字同一行）'],
  ['.howto-close {', /cursor:\s*pointer/, '关闭按钮要能被认出可点'],
];
for (const [sel, re, why] of closeCssNeed) {
  const body = ruleBody(sel);
  ok(body !== null, `styles.css 里缺少可用规则 ${sel} —— ${why}`);
  ok(re.test(body), `styles.css 的 ${sel} 规则体缺少关键声明（${why}）`);
}

ok(/const HOWTO_CLOSED_KEY = 'vdl_howto_closed';/.test(appJsCode),
  'app.js 缺少 HOWTO_CLOSED_KEY —— 关闭状态不会被记住，用户关掉后下次进来又被弹出来');
ok(/function howtoApplyClosed\s*\(/.test(appJsCode),
  'app.js 缺少 howtoApplyClosed() —— 已关闭的引导条不会真正隐藏');
ok(/e\.target\.closest\('\.howto-close'\)/.test(appJsCode),
  'app.js 没有绑定「✕ 关闭引导」的点击 —— 按钮点了没反应');
ok(/classList\.add\('is-closed'\)/.test(appJsCode),
  '关闭引导时没有打上 .is-closed —— 引导条不会消失');

// 🔴 最要命的一条：关掉的引导条**绝不能用 hidden 属性**。
//    howtoSyncButtons / visibleHowtoIn 靠 closest('[hidden]') 判「本视图有没有引导条」，
//    一旦给 details 加 hidden，右上角「?」按钮会把自己也判成"本视图没有引导"而跟着隐藏
//    ⇒ 用户再也打不开这条引导（正好废掉「重新打开就点问号」）。
const closeSeg = appJsCode.slice(
  appJsCode.indexOf("closest('.howto-close')"),
  appJsCode.indexOf("closest('.howto-btn')"));
ok(closeSeg.length > 100 && closeSeg.length < 900,
  '定位「关闭引导」的处理逻辑失败（切片长度异常）');
ok(!/\.hidden\s*=\s*true/.test(closeSeg) && !/setAttribute\(\s*'hidden'/.test(closeSeg),
  '关闭引导时给元素加了 hidden 属性 —— 会让右上角「?」按钮把自己也判成没内容而隐藏，用户再也打不开');

ok(/is-closed'\)\)\s*return/.test(howtoFn),
  'howtoAutoOpen 没有跳过已关闭的引导 —— 用户关掉后切页回来又会被自动弹开');

// 点「?」= 显示 / 隐藏 二态开关（见下段 ⑧）：先摘掉 .is-closed 再展开，或可见时整条隐藏
const btnSeg = appJsCode.slice(
  appJsCode.indexOf("closest('.howto-btn')"),
  appJsCode.indexOf("closest('.howto-btn')") + 1100);
ok(/classList\.remove\('is-closed'\)/.test(btnSeg),
  '点「?」时没有解除 .is-closed —— 关掉的引导条再也打不开（「重新打开就点问号」这条链路断掉）');
// 🔴 必须把 d.open = true 限定在 is-closed 分支**内部**（到 } else if 为止）。
//    否则后面那条 else-if 分支里的 d.open = true 会把这条断言「喂饱」，
//    本分支被改成 open=false 也照样通过（变异测试实测过的漏网）。
ok(/classList\.remove\('is-closed'\);[\s\S]{0,250}?d\.open = true;\s*\n\s*\} else if \(!d\.open\)/.test(btnSeg),
  '点「?」解除关闭后没有在本分支内强制展开 —— 用户点一下只看到一行空壳，需要再点一下才展开');

// switchView 里必须「先套用已关闭状态、再自动展开」，顺序反了会把刚关掉的又弹开
const swClosed = appJsCode.indexOf('try { howtoApplyClosed(); }', swDef);
const swCallAt = appJsCode.indexOf('try { howtoAutoOpen(); }', swDef);
ok(swClosed > swDef,
  'switchView 里没有调用 howtoApplyClosed() —— 切页时已关闭的引导条会重新露面');
ok(swClosed > swDef && swCallAt > swClosed,
  'switchView 里 howtoApplyClosed() 必须排在 howtoAutoOpen() 之前（顺序反了会把已关闭的引导又自动展开）');

/* ---------------- ⑧ 点第二次「?」必须整条隐藏，不能只是「收起」 ---------------- */
// 🔴 用户反馈：「点第二次要隐藏啊，现在收起来是不对的，占位置了」。
//    收起态（open=false 但仍可见）依旧占一行，等于没关掉。所以「?」是
//    「可见 ⇄ 整条隐藏」二态开关，绝不能退化成 open = !open 的开合。
ok(!/d\.open\s*=\s*!\s*d\.open/.test(btnSeg),
  '点「?」又变回 open = !open 的开合 —— 第二次点击只会「收起」，引导条仍占一行（用户要的是整条隐藏）');
ok(/classList\.add\('is-closed'\)[\s\S]{0,120}?d\.open = false;/.test(btnSeg),
  '「已展开」态点「?」没有整条隐藏（缺 classList.add(is-closed) + open=false，且必须先打 class 再置 open）—— 会留下占一行的收起态');
// 🔴 真机探针抓到的真缺陷：引导条初始就是「收起但可见」，若点「?」直接落到隐藏分支，
//    用户第一次点开看到的反而是引导被关掉。必须有独立的「收起 → 展开」分支。
ok(/\} else if \(!d\.open\) \{\s*\n\s*d\.open = true;\s*\n\s*\}/.test(btnSeg),
  '缺少「收起态点「?」= 展开」的分支 —— 用户第一次点按钮会看到引导反而消失（真机探针实测过的缺陷）');
ok(/if \(d\.classList\.contains\('is-closed'\)\) \{[\s\S]{0,400}?classList\.remove\('is-closed'\)/.test(btnSeg),
  '「?」的打开分支结构被破坏（is-closed 判断与 remove 不再成对）—— 关掉的引导条打不开');
// 隐藏时不得滚动 / 闪动：条已不可见，滚动只会让页面莫名跳一下
ok(/if \(!d\.classList\.contains\('is-closed'\)\) \{[\s\S]{0,200}?scrollIntoView/.test(btnSeg),
  '「?」隐藏引导条时仍执行 scrollIntoView —— 条已消失，滚动会让页面莫名跳动');
ok(/is-active',\s*!!\(d && d\.open && !d\.classList\.contains\('is-closed'\)\)/.test(appJsCode),
  'is-active 判定没有排除 is-closed —— 引导条已隐藏、「?」按钮却还亮着（状态自相矛盾）');

// 点标题行收起（原生 toggle）也必须转成整条隐藏，否则同样留下占位态
// ⚠️ 必须钉死到 document 级的引导 toggle 监听（app.js:18165 附近），
//    不能用裸 addEventListener('toggle' —— 其它视图（如步骤折叠 _sp、musicFold）
//    也有同名监听，会抢到错误片段导致误判。
const TOGGLE_ANCHOR = "document.addEventListener('toggle'";
const _ti = appJsCode.indexOf(TOGGLE_ANCHOR);
const toggleSeg = appJsCode.slice(_ti, _ti + 400);
ok(_ti >= 0 && toggleSeg.length > 150,
  '定位 document 级 toggle 监听失败（找不到 document.addEventListener(\'toggle\') 片段）');
// 🔴 反向要求：toggle 里**绝不能**把「收起」转成整条隐藏。
//    「收起」是合法默认态（除首次外每次进入都收起、只剩一行标题可点开），
//    一旦转隐藏，用户点标题行收起后整条就消失、再也看不到。
ok(!/classList\.add\('is-closed'\)/.test(toggleSeg),
  'toggle 监听里把「收起」转成了整条隐藏 —— 用户点标题行收起后整条消失，只能靠「?」找回（收起必须是合法态）');
ok(/howtoSyncButtons\(\)/.test(toggleSeg),
  'toggle 监听丢了 howtoSyncButtons() —— 手动开合引导时「?」按钮状态不同步');

console.log(`✅ 操作引导守卫生效：${EXPECT.size} 个功能入口全覆盖、位置正确、`
  + `文案与真实控件一致、首次展开机制与折叠样式在位、`
  + `右上角「?」按钮 ${btnCount} 处落点、三态（隐藏→展开 / 收起→展开 / 展开→整条隐藏）、`
  + `「✕ 关闭引导」${EXPECT.size} 处且可被「?」重新打开（共 ${count} 项断言）`);
