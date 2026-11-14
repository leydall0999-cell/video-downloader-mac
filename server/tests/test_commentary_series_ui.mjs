// 第三个成片形态（series）UI 接线守卫（2026-11-15 重构版）
//
// 背景演进：
//   ① 最初「剧集深度解说」是作为「解说类型」第 5 档上线的（series_deep）；
//   ② 用户指出它应当是**与「现有解说」「重构叙事二创」并列的第三种成片形态**，
//      藏在解说类型里名不副实（剪辑层当时没有任何 series 分支，画面与 standard 同款）；
//   ③ 现在改为「剪辑模板」第三项（template=series）：剪辑层走知识卡隔断，
//      文案层由 comGetOptions 映射成 commentary_type=series_deep，
//      选中该模板时「解说类型」整组灰化（沿用既有 .is-disabled 约定）。
//
// 本守卫锁四件事，任一处被改坏都会让「选了却跑不对/跑不起来」：
//   A. 剪辑模板必须有第三项，且 HTML radio 与 JS options 一致；
//   B. 解说类型必须回归 4 档，不得残留 series_deep（否则双入口 + 半套形态）；
//   C. comGetOptions 的映射必须存在（template=series → commentary_type=series_deep）；
//   D. 灰化必须走现成机制（.is-disabled），且 comHlSource 也跟着 series 禁用。
//
// 读真源码（web/index.html、web/app.js、server/app.py），不测副本、不依赖 /tmp。
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

// ---------- A. 剪辑模板 3 档 ----------
const tplStart = html.indexOf('id="comTemplateRow"');
ok(tplStart > 0, 'index.html 里找不到 comTemplateRow（剪辑模板行）');
const tplBlock = html.slice(tplStart, tplStart + 3000);
const tplRadio = [...tplBlock.matchAll(/name="comTemplate" value="([a-z]+)"/g)].map((m) => m[1]);
ok(JSON.stringify(tplRadio) === JSON.stringify(['standard', 'reconstruct', 'series']),
  `剪辑模板应为 3 档且顺序为 standard→reconstruct→series，实际 ${JSON.stringify(tplRadio)}`);

const TPL_LABEL = [
  ['standard', '现有解说'],
  ['reconstruct', '重构叙事二创'],
  ['series', '剧集深度解说'],
];
for (const [val, label] of TPL_LABEL) {
  ok(new RegExp(`value="${val}"[^>]*><span><b>${label}</b>`).test(tplBlock),
    `剪辑模板 ${val} 的可见文案被改动或丢失（期望「${label}」）`);
}

// JS 下拉源必须与 HTML radio 一致，否则下拉里看不到 / 选了发错值
for (const [val] of TPL_LABEL) {
  ok(new RegExp(`\\['${val}',\\s*'[^']+'\\]`).test(appJs),
    `COM_SELECT_DEFS.comTemplate.options 缺少 ${val} 项（下拉与 radio 不一致会静默发错值）`);
}

// ---------- B. 解说类型回归 4 档、无 series_deep ----------
const typeStart = html.indexOf('id="comTypePanel"');
ok(typeStart > 0, 'index.html 里找不到 comTypePanel（解说类型下拉）');
const typeBlock = html.slice(typeStart, typeStart + 3000);

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
const legacyOrder = [...typeBlock.matchAll(/data-value="([a-z_]+)"/g)].map((m) => m[1]);
ok(JSON.stringify(legacyOrder.slice(0, 4)) === JSON.stringify(LEGACY.map((x) => x[0])),
  '旧 4 个档位的顺序被改动');

const optVals = [...typeBlock.matchAll(/class="com-mode-dropdown-option[^"]*"[^>]*data-value="([a-z_]+)"/g)]
  .map((m) => m[1]);
const radioVals = [...typeBlock.matchAll(/name="comType" value="([a-z_]+)"/g)].map((m) => m[1]);
ok(JSON.stringify([...optVals].sort()) === JSON.stringify([...radioVals].sort()),
  `解说类型 option 与 radio 取值不一致：option=${optVals} radio=${radioVals}`);
ok(optVals.length === 4 && radioVals.length === 4,
  `解说类型必须回归 4 档（第三种形态已迁到剪辑模板），实际 option=${optVals.length} radio=${radioVals.length}`);
ok(!/series_deep/.test(typeBlock),
  '解说类型里仍残留 series_deep —— 会形成「双入口 + 半套形态」，必须移除');
ok(typeBlock.indexOf('com-mode-dropdown-sep') < 0,
  '解说类型里仍残留分组分隔线（第三模块迁走后此处应无分隔线）');

// ---------- C. 提交链路与映射 ----------
ok(/commentary_type:\s*\(tplEl && tplEl\.value === 'series'\)[\s\S]{0,60}?'series_deep'/.test(appJs),
  'comGetOptions() 缺少映射：剪辑模板为 series 时必须发 commentary_type=series_deep');
ok(/commentary_type:[\s\S]{0,220}?typeEl \? typeEl\.value : 'deep_hl'/.test(appJs),
  '非 series 分支必须仍从 :checked radio 读 commentary_type（透传链不能被改成常量）');
ok(/template: tplEl \? tplEl\.value : 'standard'/.test(appJs),
  "template 必须原样发模板值（series 要能被后端拼成 --template series）");
ok(/"--commentary-type",\s*commentary_type/.test(serverPy),
  '后端 _commentary_option_args 必须直接透传 --commentary-type（不设白名单）');
ok(/if template and template not in \("standard", "none", ""\):/.test(serverPy),
  '后端 --template 拼接分支被改动（series 非默认值，必须能透传下去）');

// ---------- D. 灰化与联动 ----------
ok(/const applyComTypeDisabled = \(\) => \{/.test(appJs),
  '缺少 applyComTypeDisabled（选剧集模板时「解说类型」应灰化而不是留着可点）');
ok(/comTypeDropdown\.classList\.toggle\('is-disabled', isSeries\)/.test(appJs),
  '解说类型灰化必须复用 .is-disabled 约定（styles.css:3297 已有样式）');
ok(/applyComSelectEnabled = \(\) => \{[\s\S]{0,500}?applyComTypeDisabled\(\)/.test(appJs),
  'applyComSelectEnabled 未调用 applyComTypeDisabled —— 切换模板时灰化不会跟着更新');
ok(/v === 'full_normal' \|\| v === 'full_deep' \|\| tv === 'series'/.test(appJs),
  'comHlSource 禁用规则未跟随 series 模板（会显示一个对知识卡隔断无效的开关）');
ok(/只有 deep_hl \/ normal_hl/.test(appJs) && /full_normal \/ full_deep 走完整转写稿/.test(appJs),
  '前两族联动的依据注释被改动（保留它说明旧行为为何如此）');

console.log(`OK ${n} 项断言 · 第三种成片形态（剪辑模板第三项）+ 解说类型回归 4 档`);
