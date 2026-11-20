// 扩展 popup「视频页空状态」操作按钮守卫（2026-10-01 用户反馈）。
//
// 背景：YouTube 等用加密流（UMP/SABR）的站点，扩展抓不到可下载直链，popup 里原本
// 只显示一段文字提示，**没有任何可点操作**，用户反馈「这里也要可以操作」。
// 现在空状态必须给「解析并下载 / 复制链接」两个按钮，且复制失败要如实提示（不静默）。
//
// 本测试做源码级契约钉死（不为 popup 造一整套假 document），与 test_ext_version_cmp.py
// 的「从源码抽真实实现来跑」思路一致：防止有人后续把按钮/事件删掉而无人察觉。
'use strict';

const fs = require('fs');
const path = require('path');

let PASS = 0;
let FAIL = 0;
function check(name, cond, extra) {
  if (cond) { PASS++; console.log('  ✅ ' + name); }
  else { FAIL++; console.log('  ❌ ' + name + (extra !== undefined ? '  → ' + extra : '')); }
}

const dir = path.join(__dirname, '..');
const js = fs.readFileSync(path.join(dir, 'popup.js'), 'utf8');
const css = fs.readFileSync(path.join(dir, 'popup.css'), 'utf8');
const html = fs.readFileSync(path.join(dir, 'popup.html'), 'utf8');

console.log('▶ 扩展 popup「视频页空状态」操作按钮契约');

// ① 视频页分支仍在（防整体被删）
check('保留视频页空状态分支（st.videoPage）', /if \(st\.videoPage\)/.test(js));

// ② 空状态改为 DOM 构建并挂 empty-actions 容器（不再是单一 innerHTML 字符串）
check('空状态含 empty-actions 容器', /acts\.className = 'empty-actions'/.test(js));

// ③ 两个按钮文案必须存在
check('含「解析并下载」按钮', /textContent = '解析并下载'/.test(js));
check('含「复制链接」按钮', /textContent = '复制链接'/.test(js));

// ④ 事件必须真的绑定（点了没反应 = 需求未达成）
check('「解析并下载」绑定 sendCurrentPage',
  /addEventListener\('click', function \(\) \{ sendCurrentPage\(dlBtn\)/.test(js));
check('「复制链接」绑定 copyPageUrl',
  /addEventListener\('click', function \(\) \{ copyPageUrl\(cpBtn\)/.test(js));

// ⑤ 复制实现存在且用剪贴板；失败必须可见提示（绝不静默——这正是本次用户遇到的坑）
check('定义了 copyPageUrl 并使用剪贴板',
  /function copyPageUrl\(btn\)/.test(js) && /navigator\.clipboard\.writeText\(url\)/.test(js));
check('复制失败有可见提示（不静默）', /复制失败，请手动复制地址栏链接/.test(js));

// ⑥ 样式：布局类必须有定义，否则两个按钮会挤成一行无间距
check('popup.css 定义 .empty-actions', /\.empty-actions\s*\{/.test(css));

// ⑦ 「解析并下载」文案在提示里指向该操作（用户体验连贯）
check('提示语引导用户点「解析并下载」', /点下面「解析并下载」/.test(js));

// ⑧ 清晰度选择（2026-10-01 用户反馈「目前没法选择分辨率」）：
// 扩展发来的条目原本一律按「最佳画质（自动）」建任务，用户无从选择。现在 popup 顶部
// 给下拉、随每条发送上报、并记住选择；用户选的分辨率还必须回显出来。
const selBlock = (html.match(/<select id="sendQuality"[\s\S]*?<\/select>/) || [''])[0];
const selOptions = (selBlock.match(/<option value="([^"]*)"/g) || []).length;
check('popup.html 含 #sendQuality 下拉', selBlock.length > 0);
check('清晰度下拉含 8 档（best/2160/1440/1080/720/480/360/audio）', selOptions === 8, selOptions);
check('发送时上报 quality（qualityForItem）', /quality: qualityForItem\(it\),/.test(js));
check('只对「页面 / 清单」上报（直链本身就是单一流）',
  /if \(k !== 'page' && k !== 'playlist'\) return '';/.test(js));
check('改选落 chrome.storage.local（下次打开还记得）',
  /chrome\.storage\.local\.set\(\{ sendQuality: state\.quality \}\)/.test(js));
check('改选即时回显「将以「…」下载」', /\.empty-q/.test(js) && /将以「/.test(js));
check('popup.css 定义 .empty-q', /\.empty-q\s*\{/.test(css));
check('popup.css 定义 .qrow（下拉行布局）', /\.qrow\s*\{/.test(css));

// ⑨ 兜底视图里的「视频信息」（2026-10-02 用户截图：「这里把嗅探到的视频信息也加上，
// 比如：标题、时长、大小」）。加密流抓不到直链，但标题（标签页）与时长（页面侧
// <video>.duration）是已知的 —— 兜底视图不能只剩一段解释。
check('兜底视图渲染视频标题（st.videoTitle → .empty-title）',
  /st\.videoTitle/.test(js) && /vt\.className = 'empty-title'/.test(js));
check('兜底视图渲染时长（fmtDur(st.videoDuration)）', /fmtDur\(st\.videoDuration\)/.test(js));
check('取不到就不硬塞占位（标题空/元信息空均不渲染）',
  /if \(vtitle\) \{/.test(js) && /if \(vmeta\.length\) \{/.test(js));
check('如实说明拿不到整体大小（加密流是分片的）', /抓不到直链和整体大小/.test(js));
check('popup.css 定义 .empty-title', /\.empty-title\s*\{/.test(css));
check('popup.css 定义 .empty-meta', /\.empty-meta\s*\{/.test(css));

// ⑩ 可视清晰度档位条（2026-11-20 用户「没有选择分辨率、都是默认」）：
// 原来的 <select> 收起只显示一行淡字，用户以为不能选。现在铺一排一眼可见的档位
// 按钮，点选写回同一 select 并派发 change，复用既有上报/记忆逻辑。
const chipBlock = (html.match(/<div class="qchips"[\s\S]*?<\/div>/) || [''])[0];
check('popup.html 含可视档位条 #qualityChips', /id="qualityChips"/.test(chipBlock));
const chipCount = (chipBlock.match(/class="qchip"/g) || []).length;
check('档位条含 8 档（与下拉一致）', chipCount === 8, chipCount);
check('档位条每档 data-q 对应下拉 value',
  ['best','2160','1440','1080','720','480','360','audio'].every(v =>
    chipBlock.indexOf('data-q="' + v + '"') >= 0));
check('档位点击写回 select 并派发 change（复用既有逻辑）',
  /sel\.value = v;[\s\S]*dispatchEvent\(new Event\('change'/.test(js));
check('syncQualityUI 同步高亮当前档',
  /classList\.toggle\('is-on', chips\[i\]\.getAttribute\('data-q'\) === q\)/.test(js));
check('档位条初始立即高亮（不依赖 chrome 回调）',
  /syncQualityUI\(\);[\s\S]*?\}\)\(\);/.test(js));
check('popup.css 定义 .qchip 与 .qchip.is-on',
  /\.qchip\s*\{/.test(css) && /\.qchip\.is-on\s*\{/.test(css));

console.log('');
console.log('=========================================');
console.log('  通过: ' + PASS + '   失败: ' + FAIL);
console.log('=========================================');
process.exit(FAIL === 0 ? 0 : 1);
