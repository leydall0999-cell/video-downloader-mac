// 第三个独立解说模块（series_deep）UI 接线守卫（2026-11-15）
//
// 背景：用户要求「把剧集深度解说做成单独第三个解说模块」。引擎层在独立仓
//   commentary-pipeline（family=series，独立写作规则 + 剧集知识卡 + 剧透复查），
//   前两族（highlights / full）的规则文本一行不动。本守卫只管桌面端接线：
//   下拉里单独成组的新选项 → 隐藏 radio → comGetOptions() → 后端透传 --commentary-type。
//
// 为什么需要守卫：自定义下拉是「option + 隐藏 radio」双份结构，只加 option 忘了 radio
//   会变成「点了看着选中、提交时仍是 deep_hl」的静默失效（历史上踩过同类坑）。
//
// 本测试读真源码（web/index.html、web/app.js、server/app.py），不测副本、不依赖 /tmp。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const html = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const serverPy = readFileSync(join(repoRoot, 'server', 'app.py'), 'utf8');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };

// 只取「解说类型」那块下拉，避免全页里同值字符串凑数让断言自证成真
const i0 = html.indexOf('id="comTypePanel"');
const i1 = html.indexOf('id="comOpts"', i0) > i0 ? html.indexOf('id="comOpts"', i0) : -1;
const panelStart = html.indexOf('id="comTypePanel"');
ok(panelStart > 0, 'index.html 里找不到 comTypePanel（解说类型下拉）');
const typeBlock = html.slice(panelStart, panelStart + 3000);

// ---- ① 前两族 4 个档位逐字保留（旧文案常量写死，杜绝「改旧模块」）----
const LEGACY = [
  ['deep_hl', '高光处叠加深度解说'],
  ['normal_hl', '高光部分普通解说'],
  ['full_normal', '全片普通解说'],
  ['full_deep', '全片深入解说'],
];
for (const [val, label] of LEGACY) {
  ok(new RegExp(`data-value="${val}"[^>]*>${label}</button>`).test(typeBlock),
    `旧档位 ${val} 的 option 文案被改动或丢失（前两族零改动）`);
  ok(new RegExp(`name="comType" value="${val}"`).test(typeBlock),
    `旧档位 ${val} 的隐藏 radio 丢失`);
}
// 旧 4 项顺序不变（渲染顺序影响默认高亮与 is-selected 初始化）
const legacyOrder = [...typeBlock.matchAll(/data-value="([a-z_]+)"/g)].map(m => m[1]);
ok(JSON.stringify(legacyOrder.slice(0, 4)) === JSON.stringify(LEGACY.map(x => x[0])),
  '旧 4 个档位的顺序被改动');

// ---- ② 第三个模块：单独成组、option 与 radio 成对 ----
ok(/data-value="series_deep"[^>]*>剧集深度解说<\/button>/.test(typeBlock),
  '缺少 series_deep 的 option（第三个模块必须能在下拉里被选到）');
ok(/name="comType" value="series_deep"/.test(typeBlock),
  '缺少 series_deep 的隐藏 radio —— 选了也不会提交，典型静默失效');
const iSep = typeBlock.indexOf('com-mode-dropdown-sep');
const iFullDeep = typeBlock.indexOf('data-value="full_deep"');
const iSeries = typeBlock.indexOf('data-value="series_deep"');
ok(iFullDeep > 0 && iSep > iFullDeep && iSeries > iSep,
  '第三个模块必须用分隔线单独成组（用户在「单独第三个模块」语义），且排在旧 4 项之后');

// ---- ③ option 与 radio 的取值集合必须完全一致（防漏配/多配）----
// 默认项带 is-selected 类，class 属性是 "com-mode-dropdown-option is-selected"，
// 正则须允许 class 里有额外 token，否则默认项会被漏算（守卫自身假红）。
const optVals = [...typeBlock.matchAll(/class="com-mode-dropdown-option[^"]*"[^>]*data-value="([a-z_]+)"/g)]
  .map(m => m[1]);
const radioVals = [...typeBlock.matchAll(/name="comType" value="([a-z_]+)"/g)].map(m => m[1]);
ok(JSON.stringify([...optVals].sort()) === JSON.stringify([...radioVals].sort()),
  `option 与 radio 取值不一致：option=${optVals} radio=${radioVals}`);
ok(optVals.length === 5 && radioVals.length === 5,
  `应为 5 个类型（旧 4 + 新 1），实际 option=${optVals.length} radio=${radioVals.length}`);

// ---- ④ 提交链路：前端取值 → 后端参数，全链路不改写类型 ----
ok(/commentary_type:\s*typeEl\s*\?\s*typeEl\.value\s*:\s*'deep_hl'/.test(appJs),
  'comGetOptions() 必须继续从 :checked radio 读 commentary_type（透传链不能被改成常量/白名单过滤）');
ok(/"--commentary-type",\s*commentary_type/.test(serverPy),
  '后端 _commentary_option_args 必须直接透传 --commentary-type（不设旧 4 种白名单）');

// ---- ⑤ 联动：第三个模块按全片渲染，禁用「自动挑高光」；旧两族规则不变 ----
ok(/v === 'full_normal' \|\| v === 'full_deep' \|\| v === 'series_deep'/.test(appJs),
  'series_deep 未加入 comHlSource 禁用规则（它会显示一个对全片渲染无效的开关）');
ok(/只有 deep_hl \/ normal_hl/.test(appJs) && /full_normal \/ full_deep 走完整转写稿/.test(appJs),
  '前两族联动的注释/依据被改动（要保留它作为「旧行为为何如此」的说明）');

console.log(`OK ${n} 项断言 · 第三个解说模块 UI 接线 + 前两族零改动`);
