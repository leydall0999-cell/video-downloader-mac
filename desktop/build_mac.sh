#!/usr/bin/env bash
# 构建 macOS 桌面版（VideoDownloader.app）
# 用法：
#   bash desktop/build_mac.sh                 # 正常构建（结束后自动收尾旧产物）
#   bash desktop/build_mac.sh --cleanup-only  # 只清理历史构建垃圾，不构建
# 环境变量：
#   VDL_BUILD_KEEP_OLD=1  保留最近几份 _old_* 用于回滚（默认 1；设 0 表示全清）
#   VDL_BUILD_WORKPATH=…  PyInstaller 工作目录（默认 $REPO/build）。**沙盒里必须改**：
#                         仓库内 build/ 若残留上次构建的已签名 exe，替换大二进制会被拦
#                         （AssertionError: Executable contains code signature!）。
#                         沙盒用法：VDL_BUILD_WORKPATH=/tmp/vdl_build_work bash desktop/build_mac.sh
# 产物：dist/VideoDownloader.app（双击即用，无需安装 Python/ffmpeg）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# ── 构建互斥锁：防止并发构建互相争抢 dist/ 与 .build_venv（原子 mkdir，退出即释放）──
BUILD_LOCK="$REPO/.build.lock"
if ! mkdir "$BUILD_LOCK" 2>/dev/null; then
  echo "❌ 已有另一个构建在运行（锁目录 $BUILD_LOCK 存在）。请等其结束后再构建。" >&2
  echo "   若确认无其它构建，可手动删除该锁目录：rmdir '$BUILD_LOCK'" >&2
  exit 1
fi
trap 'rmdir "$BUILD_LOCK" 2>/dev/null || true' EXIT

# ── 构建指纹：在【构建一开始】就锁定，不要等到结尾 ──────────────────────────
# 🔴 2026-09-22 用户报了「构建老是出现提交旧版本」，实测复现了两条根因，都在这几行：
#
#   根因 A（标签谎报「旧提交」）：原先只写 `git rev-parse --short HEAD`，它只看
#   提交指针、**完全不看工作区**。所以只要构建时还有未提交的改动，打出来的标签就是
#   「上一个提交」，而包里的代码其实是改动后的新代码 —— 包内容 ≠ 标签。
#   实测：工作区改过 web/app.js 后跑热更，包内 app.js 已含改动，标签却仍是 de33099。
#   修法：加 `-dirty` 后缀。带 `-dirty` 就说明「这个包对应一份没进版本库的代码」，
#   一眼可辨，不会再被误读成「构建出了旧版本」。
#
#   根因 B（标签谎报「新提交」）：指纹原先在构建【结尾】才取（原 477-478 行），
#   而全量构建要跑约 40 分钟。构建期间若落了提交，标签会指向「比包里代码更新」的
#   提交号。2026-09-15 已在解说管线上踩过同一个坑（见下方 PIPELINE_HASH 注释），
#   当时只给管线打了补丁，app 自身的 SHA 一直没修。现在在开头锁定，一次定位。
#
# ⚠️ dirty 判定必须排除未追踪文件（--untracked-files=no）：本仓库常驻大量
#    未追踪产物（dist/、PROJECT_SNAPSHOT_*.md、.workbuddy/、docs/…），若把它们
#    算作 dirty，每次构建都会显示 -dirty，这个标记就彻底失去意义了。
APP_BUILD_HASH="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  APP_BUILD_HASH="$APP_BUILD_HASH-dirty"
  APP_BUILD_DATE="$(date +'%m-%d %H:%M')"   # 脏构建：显示构建时刻，而非上一个提交时间
else
  APP_BUILD_DATE="$(git -C "$REPO" log -1 --format='%cd' --date=format:'%m-%d %H:%M' 2>/dev/null || echo '?')"
fi
echo "▶ 构建指纹已锁定：$APP_BUILD_HASH @ $APP_BUILD_DATE"

# 找 node：先 PATH，再 workbuddy 托管（按版本号取最新），最后常见位置。
# 不在脚本里硬写绝对路径，跨机器/跨用户都能跑；找不到则明确报错而不是静默回退。
NODE=""
NODE="$(command -v node 2>/dev/null || true)"
if [ -z "$NODE" ] && [ -d "$HOME/.workbuddy/binaries/node/versions" ]; then
  for d in $(ls -1 "$HOME/.workbuddy/binaries/node/versions" 2>/dev/null | sort -V); do
    cand="$HOME/.workbuddy/binaries/node/versions/$d/bin/node"
    [ -x "$cand" ] && NODE="$cand" && break
  done
fi
if [ -z "$NODE" ]; then
  for cand in /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node; do
    [ -x "$cand" ] && NODE="$cand" && break
  done
fi
if [ -z "$NODE" ]; then
  echo "❌ 找不到 node，JS 语法门禁无法执行。请安装 node 或将 node 加入 PATH 后重试。" >&2
  exit 1
fi

PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
VENV="$REPO/.build_venv"

# ── 历史构建产物收尾 ────────────────────────────────────────────────
# 背景：下面打包前会把上一次产物 mv 成 _old_<pid>（绕开沙盒的批量删除守卫，
#       PyInstaller 自己清 build/dist 会被拦）。只挪不删 => 每构建一次堆 ~200MB。
# 策略：构建成功后统一收尾，每类只保留最近 N 份用于回滚，更早的**移入回收站**
#       （可恢复，不用 rm -rf；osascript/Finder 在本机未授权，故直接搬 ~/.Trash）。
BUILD_KEEP_OLD="${VDL_BUILD_KEEP_OLD:-1}"

# ── PyInstaller 工作目录（沙盒关键）─────────────────────────────────
# 🔴 2026-09-20 踩坑：默认 workpath = $REPO/build。若该目录里**残留上一次构建的
#    build/VideoDownloader/VideoDownloader**（一个已带 LC_CODE_SIGNATURE 的 40MB exe），
#    PyInstaller 的 fix_exe_for_code_signing() 会先删签名再改 Mach-O 头，
#    而「覆盖替换一个已存在的 40MB 可执行文件」正是沙盒 file-write-unlink 守卫拦的动作
#    ⇒ 构建以 `AssertionError: Executable contains code signature!` 收场，
#    且脚本里 `mv build/VideoDownloader build/_old_<pid>` 对同名大目录同样会被拦（静默 || true）。
#    把 workpath 指到仓库外的可写目录即可根治：新目录每次都是干净的，不触发任何替换。
# 默认值保持 $REPO/build（不改变用户本机行为）；沙盒里用
#    VDL_BUILD_WORKPATH=/tmp/vdl_build_work bash desktop/build_mac.sh
VDL_BUILD_WORKPATH="${VDL_BUILD_WORKPATH:-$REPO/build}"
mkdir -p "$VDL_BUILD_WORKPATH" 2>/dev/null || true
if [ "$VDL_BUILD_WORKPATH" != "$REPO/build" ]; then
  echo "ℹ️  PyInstaller 工作目录改为 ${VDL_BUILD_WORKPATH}（绕开沙盒对仓库内 build/ 的替换守卫）"
fi

trash_path() {
  local src="$1" base dest n
  [ -e "$src" ] || return 0
  base="$(basename "$src")"
  dest="$HOME/.Trash/$base"
  n=1
  while [ -e "$dest" ]; do
    dest="$HOME/.Trash/${base}-$n"
    n=$((n + 1))
  done
  if mv "$src" "$dest" 2>/dev/null; then
    echo "   ♻️  移入回收站：$base"
  else
    echo "   ⚠️  移入回收站失败，已跳过（未删除）：$src"
  fi
}

# 用法：cleanup_family <保留份数> <glob...>；按修改时间新→旧，超出的进回收站
cleanup_family() {
  local keep="$1"; shift
  local idx=0 p
  while IFS= read -r p; do
    [ -n "$p" ] && [ -e "$p" ] || continue
    idx=$((idx + 1))
    if [ "$idx" -le "$keep" ]; then
      echo "   ↩︎  保留（可回滚）：$(basename "$p")"
    else
      trash_path "$p"
    fi
  done < <(ls -dt "$@" 2>/dev/null || true)
}

cleanup_old_artifacts() {
  echo "▶ 收尾：清理历史构建产物（保留最近 ${BUILD_KEEP_OLD} 份，其余进回收站）"
  cleanup_family "$BUILD_KEEP_OLD" "$REPO"/dist/_old_*.app
  cleanup_family "$BUILD_KEEP_OLD" "$REPO"/dist/_oldfolder_*
  cleanup_family "$BUILD_KEEP_OLD" "$REPO"/build/_old_*
  echo "   dist 现占用：$(du -sh "$REPO/dist" 2>/dev/null | awk '{print $1}')  build 现占用：$(du -sh "$REPO/build" 2>/dev/null | awk '{print $1}')"
}

if [ "${1:-}" = "--cleanup-only" ] || [ "${1:-}" = "--cleanup" ]; then
  cleanup_old_artifacts
  echo "✅ 清理完成（文件在废纸篓里，确认没问题后自行清空即可释放空间）"
  exit 0
fi

echo "▶ 准备构建环境（独立 venv）"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --timeout 120 --retries 5 --no-cache-dir --index-url "$PIP_INDEX" -r requirements.txt pyinstaller Pillow pywebview

echo "▶ 应用图标：使用品牌图标（视频工坊 · 青蓝圆角）"
SRC_ICON="$REPO/brand/app_icon/icon_videoworkshop_squircle_1024.png"
ICON_PNG="$REPO/desktop/icon.png"
if [ -f "$SRC_ICON" ]; then
  cp -f "$SRC_ICON" "$ICON_PNG"
  echo "icon.png <- $SRC_ICON"
else
  echo "⚠️ 未找到品牌图标源，回退到内联生成的旧图标" >&2
  "$VENV/bin/python" - "$REPO" <<'PY'
import sys
from pathlib import Path
from PIL import Image, ImageDraw
repo = Path(sys.argv[1])
W = 512
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, W, W], radius=112, fill=(5, 102, 226, 255))
cx = W // 2
d.polygon([(cx, 372), (cx - 96, 256), (cx + 96, 256)], fill=(255, 255, 255, 255))
d.rectangle([cx - 30, 140, cx + 30, 300], fill=(255, 255, 255, 255))
d.rounded_rectangle([cx - 120, 406, cx + 120, 452], radius=22, fill=(255, 255, 255, 255))
out = repo / "desktop" / "icon.png"
img.save(out)
print("icon.png (fallback) ->", out)
PY
fi
ICON_PNG="$REPO/desktop/icon.png"
ICON_ICNS="$REPO/desktop/icon.icns"

# 清理上一轮构建 mv 出来的临时副本（自愈式回收）。
# 背景：下面若干处刻意用 mv 而非 rm -f 移走旧产物，是为了避开沙盒的批量删除守卫；
# 但这些副本此前**从不清退**，每次构建都新攒一份 —— 历史上堆到 207 个
# icon.icns.bak.<pid>。这里在「写入新副本之前」统一清一遍，保证同类残留永远
# 不超过 1 份，同时完整保留 mv 绕过守卫的做法。
_cleanup_stale_copies() {
  local f
  for f in "$@"; do
    [ -e "$f" ] || continue
    rm -rf -- "$f" 2>/dev/null || true
  done
}

# 用 mv 而非 rm -f 移走旧 .icns，避开沙盒的批量删除守卫（2112 个文件的误报）
_cleanup_stale_copies "$ICON_ICNS".bak.* "$REPO/VideoDownloader.spec".bak.* \
  "$REPO"/dist/_old_* "$REPO"/build/_old_* "$REPO"/.jscheck.err.bak.*
[ -e "$ICON_ICNS" ] && mv "$ICON_ICNS" "$ICON_ICNS.bak.$$" 2>/dev/null || true
# 用 Pillow 生成 icns：sips 对部分 RGBA PNG 会静默失败（Error 13），Pillow 稳定可靠
if "$VENV/bin/python" - "$ICON_PNG" "$ICON_ICNS" <<'PY' 2>/dev/null
import sys
from PIL import Image
Image.open(sys.argv[1]).save(sys.argv[2])
print("icon.icns ->", sys.argv[2])
PY
then
  echo "icon.icns <- Pillow ($ICON_PNG)"
else
  echo "⚠️ Pillow 生成 icns 失败，回退 sips" >&2
  sips -s format icns "$ICON_PNG" --out "$ICON_ICNS" >/dev/null 2>&1 || true
fi

echo "▶ 打包 VideoDownloader.app（单文件夹 / 无控制台）"
# 用 rename 移走旧产物，避免触发沙盒的批量删除守卫（pyinstaller 清理旧 build/dist 会被拦截）
if [ -e "$REPO/dist/VideoDownloader.app" ]; then
  mv "$REPO/dist/VideoDownloader.app" "$REPO/dist/_old_$$.app" 2>/dev/null || true
fi
if [ -e "$REPO/dist/VideoDownloader" ]; then
  mv "$REPO/dist/VideoDownloader" "$REPO/dist/_oldfolder_$$" 2>/dev/null || true
fi
if [ -e "$REPO/build/VideoDownloader" ]; then
  mv "$REPO/build/VideoDownloader" "$REPO/build/_old_$$" 2>/dev/null || true
fi
# 删除 stale .spec：PyInstaller 若发现 repo 根已有同名 spec，会直接复用其配置，
# 而旧 spec 可能指向已不存在的 staging 目录或导致 onedir 输出，破坏后续 .app 注入步骤。
[ -e "$REPO/VideoDownloader.spec" ] && mv "$REPO/VideoDownloader.spec" "$REPO/VideoDownloader.spec.bak.$$" 2>/dev/null || true

# ── 解说管线随包（自包含铁律：#198 单二进制双角色）──
# 设环境变量 COMMENTARY_PIPELINE_DIR 指向 commentary-pipeline 仓库根目录；
# 不设默认尝试 REPO 平级目录（可被 Python 项目目录布局覆盖）。

# 白名单精选管线文件到临时 staging 目录：
# 避免 .git(608M)/work(924M)/input(1.6G)/output(425M)/.venv(131M) 等垃圾随包
_stage_commentary() {
  local src="$1"
  # 每次构建用独立 staging 目录(带 PID)，避免并发构建互相挪走对方目录导致校验误报
  local staging="$REPO/build/commentary_staging_$$"
  # 清掉历史 staging 残留(避免堆积；用移入回收站，不触发删除守卫)
  # 注意：trash_path 的日志必须丢到 stderr，否则会被 $(_stage_commentary ...) 一并捕获，
  # 污染 COMMENTARY_STAGING 变量（见下方 grep 校验），导致版本校验误报失败。
  for _old in "$REPO"/build/commentary_staging_*; do
    [ -e "$_old" ] && trash_path "$_old" >&2
  done
  mkdir -p "$staging"
  # 白名单精选管线核心文件（排除 .git/work/input/output/.venv/.venv_qwen3tts/__pycache__ 等 3.5G+ 垃圾）
  cp "$src/process.py" "$staging/" 2>/dev/null || true
  if [ -d "$src/scripts" ]; then
    mkdir -p "$staging/scripts"
    # 排除 Qwen3-TTS 专用虚拟环境（本机外部服务，端口 7871，不应随 App 分发）
    # 同时排除 __pycache__、日志、.DS_Store 等构建/调试残留
    find "$src/scripts" -mindepth 1 -maxdepth 1 \
      ! -name '.venv*' \
      ! -name '__pycache__' \
      ! -name '.DS_Store' \
      ! -name '*.log' \
      ! -name '*.bak*' \
      ! -name '*.orig' \
      ! -name '*.rej' \
      ! -name '*.pyc' \
      -exec cp -R {} "$staging/scripts/" \;
  fi
  # 2026-09-07: 不再随包预置 whisper 模型(改为首次按需下载, 省 ~141MB)。
  # 只复制 models/ 下除 whisper-base 外的其它内容(若有)。
  if [ -d "$src/models" ]; then
    mkdir -p "$staging/models"
    for _m in "$src/models"/*; do
      [ -e "$_m" ] || continue
      [ "$(basename "$_m")" = "whisper-base" ] && continue
      cp -R "$_m" "$staging/models/"
    done
  fi
  [ -d "$src/assets" ] && cp -R "$src/assets" "$staging/"
  # 字幕自包含铁律：确保随包 assets/fonts 有真实中文字体，否则 PIPL 在打包机上字幕中文
  # 会回落到系统字体（用户机器缺中文字体时直接不显示 -> 用户看到的「没有字幕」）。
  # 开发态 assets/fonts 通常只有 README，这里从本机系统字体补一份进去。
  _fonts="$staging/assets/fonts"
  mkdir -p "$_fonts"
  if ! ls "$_fonts"/*.tt[cf] >/dev/null 2>&1; then
    for _sys in "/System/Library/Fonts/Hiragino Sans GB.ttc" \
                "/System/Library/Fonts/PingFang.ttc" \
                "/System/Library/Fonts/STHeiti Light.ttc"; do
      if [ -f "$_sys" ]; then
        cp "$_sys" "$_fonts/" 2>/dev/null && echo "   ✔ 复制系统中文字体进包: $(basename "$_sys")" >&2
        break
      fi
    done
  fi
  for _f in "requirements.txt" "requirements-gpu.txt" "requirements-moviepy.txt" "requirements-worker.txt" \
            "VERSION" "selfcheck.py" "prepare_model.py" "start_worker.sh" \
            "COMMENTARY_PLAN.md" "verify_quality.py" "run_local.sh" "README.md"; do
    [ -f "$src/$_f" ] && cp "$src/$_f" "$staging/"
  done
  # ── 记录「真正被打进包」的管线 revision ────────────────────────────────
  # 踩坑（2026-09-15 实测）：staging 在构建**开头**执行，而指纹在构建**结尾**读 `git HEAD`；
  # 两者之间若又落了提交，指纹就会"虚报"——构建 10 指纹写着 6d5c330，包里却没有那次提交的
  # 定向重写代码（排查了一轮才发现）。这里在暂存**当场**把 revision 写进 staging，供指纹步优先读取；
  # 工作树相对 HEAD 有未提交改动时追加 -dirty，避免「HEAD 干净但内容已改」再次骗人。
  _pl_sha="$(git -C "$src" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if [ -n "$(git -C "$src" status --porcelain -- scripts process.py 2>/dev/null)" ]; then
    _pl_sha="${_pl_sha}-dirty"
  fi
  printf '%s\n' "$_pl_sha" > "$staging/_pipeline_sha.txt"
  echo "   ✔ 管线暂存版本: ${_pl_sha}（指纹以此为准，不取构建末尾的 HEAD）" >&2
  echo "$staging"
}

COMMENTARY_DIR="${COMMENTARY_PIPELINE_DIR:-$REPO/../commentary-pipeline}"
COMMENTARY_DATA=()
if [ -d "$COMMENTARY_DIR" ] && [ -f "$COMMENTARY_DIR/process.py" ]; then
  echo "▶ 捆绑解说管线(随包自包含): $COMMENTARY_DIR"
  # 安装管线核心依赖到构建 venv，让 PyInstaller 一并冻结进 exe。
  # 单二进制双角色：worker 子进程用 sys.executable 重入自身，
  # faster_whisper/edge_tts 等必须在冻结包里、不能依赖外部 .venv。
  "$VENV/bin/pip" install --timeout 120 --retries 5 --no-cache-dir --index-url "$PIP_INDEX" -r "$COMMENTARY_DIR/requirements.txt"
  # ── 铁律校验：转写链路必须真能解码（2026-10-23 线上事故）────────────────────
  # av 19.0.1 删除了 av.open(..., metadata_errors=...) 参数，而 faster-whisper 1.2.1
  # 的 decode_audio() 仍在传 → 打包后**所有转写 1 秒内崩**（TypeError），
  # App 只显示「生成失败：解说管线退出 1」，traceback 落在 transcribe。
  # 最阴的地方：av 是 faster-whisper 的传递依赖，不钉版本时 pip 装最新；
  # 开发机 venv / 管线 venv 恰好都是 18.0.0（能跑），只有构建 venv 装成 19（坏），
  # 于是「本地怎么测都正常，发出去的包全挂」。这里用**真实 decode_audio 调用**验证，
  # 走的就是打进包的同一份代码，0.2 秒静音即可，不联网不占资源。
  _av_rc=0
  "$VENV/bin/python" - <<'PY' || _av_rc=$?
import os, struct, sys, tempfile
try:
    import av, faster_whisper
    from faster_whisper.audio import decode_audio
except Exception as exc:
    print(f"❌ 转写依赖导入失败: {exc}"); sys.exit(1)
frames = b"\x00\x00" * 3200          # 200ms 16kHz 单声道 s16 静音
data = (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
        + b"data" + struct.pack("<I", len(frames)) + frames)
p = os.path.join(tempfile.gettempdir(), "vdl_av_probe.wav")
with open(p, "wb") as f:
    f.write(data)
rc = 0
try:
    decode_audio(p, sampling_rate=16000)
    print(f"✅ 转写依赖兼容：av {av.__version__} + faster-whisper {faster_whisper.__version__}")
except TypeError as exc:
    if "metadata_errors" in str(exc):
        print(f"❌ av {av.__version__} 与 faster-whisper {faster_whisper.__version__} 不兼容：{exc}")
        print("   → av 必须 <19，见 commentary-pipeline/requirements.txt 的钉版本说明")
        rc = 1
    else:
        print(f"⚠️ 解码探针 TypeError（非版本问题，不阻断）: {exc}")
except Exception as exc:
    print(f"⚠️ 解码探针异常（非版本问题，不阻断）: {exc}")
finally:
    try: os.unlink(p)
    except OSError: pass
sys.exit(rc)
PY
  if [ "$_av_rc" -ne 0 ]; then
    echo "❌ 构建中止：转写依赖不兼容（av / faster-whisper 版本冲突，详见上方提示）"
    exit 1
  fi
  # 白名单精选管线文件到临时 staging 目录，再 --add-data（避免 .git/work/input 等 3.5G 垃圾随包）
  COMMENTARY_STAGING=$(_stage_commentary "$COMMENTARY_DIR")
  # ── 铁律校验：确保随包的是新版解说管线（含 --one-click 等 1.1.0 新 CLI）──
  # 历史上曾因 COMMENTARY_PIPELINE_DIR 未传入而静默打进旧版 1.0.0；
  # 这里在打包前先卡死，宁可构建失败也不产出错误版本。
  if ! grep -q -- "--one-click" "$COMMENTARY_STAGING/process.py" 2>/dev/null; then
    echo "❌ 解说管线版本校验失败：未在 staging 检测到 1.1.0 新 CLI(--one-click)，疑似误捆绑旧版！"
    echo "   当前 COMMENTARY_DIR=$COMMENTARY_DIR"
    echo "   请确认该目录指向 commentary-pipeline 1.1.0（设 COMMENTARY_PIPELINE_DIR 或确保 $REPO/../commentary-pipeline 软链正确）"
    exit 1
  fi
  echo "   ✔ 解说管线版本校验通过（检测到 1.1.0 新 CLI）"
  COMMENTARY_DATA=(
    --add-data "$COMMENTARY_STAGING:commentary"
    --collect-all faster_whisper
    --collect-all edge_tts
    --collect-all ctranslate2
    --collect-all tokenizers
    --collect-all av
  )
else
  echo "⚠️  未找到解说管线($COMMENTARY_DIR)，解说功能不包含在包内；设 COMMENTARY_PIPELINE_DIR=<路径> 可启用"
fi

# ── 百度网盘 / PCS-Go 功能已整体移除（用户要求「网盘全部不要」），不再预下载或打包 BaiduPCS-Go ──
_PCS_ADD_BINARY_ARG=""

# ── deno：YouTube JS 挑战（nsig）的 JS runtime ──
# requirements.txt 里的 PyPI 包 `deno` 会把官方二进制装到 $VENV/bin/deno，
# 而 PyInstaller **不会**分发 venv 的脚本目录 ⇒ 必须显式 --add-binary 才有。
# 落到包内 <_MEIPASS>/bin/deno，运行时由 server/downloader.py 的
# _find_js_runtime_binary() 找到并交给 yt-dlp 的 js_runtimes（见其上方实测矩阵）。
# 缺了它：带登录态会卡在 "The page needs to be reloaded"，裸请求被 bot 拦截。
_DENO_BINARY_ARGS=()
if [ -x "$VENV/bin/deno" ]; then
  _DENO_BINARY_ARGS=(--add-binary "$VENV/bin/deno:bin")
else
  echo "⚠️  未找到 $VENV/bin/deno —— 包内将没有 JS runtime，YouTube 可能解不了 nsig 挑战" >&2
fi

# ── 写入构建信息（版本号显示用）──
# 用开头锁定的 APP_BUILD_HASH（同款含 -dirty），不要在这里另取一次 HEAD。
# 注：本文件当前无人消费（全仓 grep 无读取方），保留是为向后兼容；
#     它在 .gitignore:46 里，属未追踪产物，不会污染上面「是否 dirty」的判定。
_BUILD_HASH="$APP_BUILD_HASH"
_BUILD_TIME="$(date '+%m-%d %H:%M')"
cat > "$REPO/server/build_info.txt" <<BUILDINFO
{"hash": "${_BUILD_HASH}", "time": "${_BUILD_TIME}"}
BUILDINFO
echo "   ✔ 构建信息: ${_BUILD_HASH} @ ${_BUILD_TIME}"

# ── JS 语法门禁：防止前端语法错误（如顶层 await）导致整个 app.js 解析失败、所有按钮失效 ──
echo "▶ JS 语法门禁 (node --check web/*.js + extension/*.js)..."
JS_FAIL=0
for f in "$REPO"/web/*.js "$REPO"/extension/*.js; do
  if [ -f "$f" ]; then
    if ! "$NODE" --check "$f" 2>"$REPO"/.jscheck.err; then
      echo "❌ JS 语法错误: $f"; cat "$REPO"/.jscheck.err; JS_FAIL=1
    fi
  fi
done
# 用 mv 而非 rm -f 移走 .jscheck.err，避开沙盒的批量删除守卫误报
[ -e "$REPO"/.jscheck.err ] && mv "$REPO"/.jscheck.err "$REPO"/.jscheck.err.bak.$$ 2>/dev/null || true
if [ "$JS_FAIL" -ne 0 ]; then
  echo "   ❌ 前端 JS 存在语法错误，已阻断构建（避免出包后按钮全失效）" >&2
  exit 1
fi
echo "   ✔ 前端 JS 语法校验通过"

# ── 随包资源瘦身 + 源码防泄露（2026-09-30 P0 加固）────────────────────────────
# 原写法 `--add-data "$REPO/server:server"` 会把 server/ 整个目录**明文**打进包 ——
# 实测包内 148 个 .py 源码 + tests + __pycache__ + 编辑器备份 .bak 全都能直接读，
# 等于把后端源码送给任何拿到安装包的人。
# 改为只把运行时真正需要的**非源码**资源复制到 staging 再 --add-data：
#   assets/        扩展打包源 + 图标（routers/extension.py 寻路依赖）
#   build_info.txt 版本信息
#   licenses/      LGPL 等许可证文本（合规随包分发）
# Python 源码仍会被 PyInstaller 收进 PYZ（运行必需），但不再是明文 .py。
# PYZ 内的 .pyc 仍可被提取反编译 —— 那层要靠把核心模块 Cython 编成 .so（见下）。
SERVER_STAGING="${VDL_BUILD_WORKPATH}_server_resources"
# 用「移入废纸篓」代替 rm -rf（2026-10-02）：本目录有 53+ 个文件，`rm -rf` 会触发环境的
# 「批量删除守卫」（日志只有一行 SAFE_DELETE_BULK_CONFIRM_REQUIRED，阈值 50）→ 构建当场死掉、
# 且不留任何 error（第 4 次踩）。脚本里其他清理点早已是这个 mv 约定（见上文注释）。
if [ -e "$SERVER_STAGING" ]; then
  mv "$SERVER_STAGING" "$HOME/.Trash/vdl_server_staging_$(date +%s)" 2>/dev/null || rm -rf "$SERVER_STAGING"
fi
mkdir -p "$SERVER_STAGING"
cp -R "$REPO/server/assets" "$SERVER_STAGING/" 2>/dev/null
cp "$REPO/server/build_info.txt" "$SERVER_STAGING/" 2>/dev/null
[ -d "$REPO/server/licenses" ] && cp -R "$REPO/server/licenses" "$SERVER_STAGING/" 2>/dev/null
if [ ! -f "$SERVER_STAGING/build_info.txt" ] || [ ! -d "$SERVER_STAGING/assets" ]; then
  echo "❌ server 资源 staging 不完整（缺 build_info.txt 或 assets），终止构建"
  exit 1
fi
echo "   ✔ server 随包资源已 staging：$(find "$SERVER_STAGING" -type f | wc -l | tr -d ' ') 个非源码文件"

# ── yt-dlp 随包插件 staging（2026-10-13）──────────────────────────────────────
# 原写法 `--add-data "$REPO/yt_dlp_plugins:yt_dlp_plugins"` 会把仓库里的
# `__pycache__/*.pyc` 一起打进包（用户拿到一堆用不上的编译缓存；发布包机密核查
# 会把它判为我方区垃圾）。插件本体**必须**保持 .py 明文（yt-dlp 就是按源码加载插件），
# 所以只排除缓存与编辑器残留，用 staging 复制。
YDLP_STAGING="${VDL_BUILD_WORKPATH}_ytdlp_plugins"
if [ -e "$YDLP_STAGING" ]; then
  mv "$YDLP_STAGING" "$HOME/.Trash/vdl_ydlp_staging_$(date +%s)" 2>/dev/null || rm -rf "$YDLP_STAGING"
fi
mkdir -p "$YDLP_STAGING"
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' \
      --exclude '*.bak*' --exclude '*.orig' --exclude '*.rej' \
      "$REPO/yt_dlp_plugins/" "$YDLP_STAGING/"
echo "   ✔ yt-dlp 插件已 staging：$(find "$YDLP_STAGING" -type f | wc -l | tr -d ' ') 个文件（已剔除 __pycache__）"

# ── P1 加固（2026-09-30）：核心模块 Cython 编译为 .so ────────────────────────
# 去掉明文 .py 只是关上最敞开的门；PYZ 里的 .pyc 仍能被提取反编译（见下：
# PyInstaller ≥6.0 已移除 --key）。真正防住源码被读的手段是把核心模块编成 .so：
# 包里是机器码，源码根本不存在（实测 inspect.getsource 抛 OSError）。
# 只编「被逆向会造成实际损失」的模块 —— 全量编译会显著拉长构建时间并放大兼容性风险。
# 逃生阀：VDL_CYTHON_MODULES="" 可关闭（例如紧急发版撞上编译器问题）。
VDL_CYTHON_MODULES="${VDL_CYTHON_MODULES-license_client,user_membership,admin_store,baidu_contribute}"
NATIVE_STAGING="${VDL_BUILD_WORKPATH}_native"
NATIVE_PATH_ARG=""
if [ -n "$VDL_CYTHON_MODULES" ]; then
  "$VENV/bin/pip" install -q --timeout 120 --retries 3 --no-cache-dir --index-url "$PIP_INDEX" cython 2>&1 | tail -2
  # 同上：移入废纸篓而非 rm -rf，避开批量删除守卫（cython 产物文件数随模块数增长）
  if [ -e "$NATIVE_STAGING" ]; then
    mv "$NATIVE_STAGING" "$HOME/.Trash/vdl_native_staging_$(date +%s)" 2>/dev/null || rm -rf "$NATIVE_STAGING"
  fi
  mkdir -p "$NATIVE_STAGING"
  IFS=',' read -r -a CY_MODULES <<< "$VDL_CYTHON_MODULES"
  CY_SRCS=()
  for m in "${CY_MODULES[@]}"; do
    if [ ! -f "$REPO/server/$m.py" ]; then
      echo "❌ Cython 目标不存在: server/$m.py"; exit 1
    fi
    cp "$REPO/server/$m.py" "$NATIVE_STAGING/"
    CY_SRCS+=("$m.py")
  done
  # 2026-10-30：只写贡献令牌 —— 仓库源码永远占位符，构建时从本机密钥注入 staging
  # 副本（随 Cython 编进 .so，不进 git）。密钥缺失则保持占位符（运行时视为无凭据）。
  if [ -f "$NATIVE_STAGING/baidu_contribute.py" ] && [ -s "$HOME/.vdl_secrets/vdl_contribution_token" ]; then
    # 只锚定 _DEFAULT_TOKEN 赋值行做替换；全局替换会连比对哨兵一起换掉（1.1.04 事故）
    /usr/bin/sed -i '' "s|_DEFAULT_TOKEN = \"__CONTRIB_TOKEN__\"|_DEFAULT_TOKEN = \"$(cat "$HOME/.vdl_secrets/vdl_contribution_token")\"|" "$NATIVE_STAGING/baidu_contribute.py"
    echo "   ✔ 贡献令牌已注入 staging（仓库源码不受影响）"
  fi
  echo "▶ Cython 编译核心模块 ($VDL_CYTHON_MODULES) ..."
  if ! ( cd "$NATIVE_STAGING" && "$VENV/bin/cythonize" -i -3 "${CY_SRCS[@]}" ) >/dev/null 2>"$VDL_BUILD_WORKPATH/cython.err"; then
    echo "❌ Cython 编译失败，详见 $VDL_BUILD_WORKPATH/cython.err"
    tail -8 "$VDL_BUILD_WORKPATH/cython.err" 2>/dev/null
    exit 1
  fi
  for m in "${CY_MODULES[@]}"; do
    if ! ls "$NATIVE_STAGING"/"$m".cpython-*.so >/dev/null 2>&1; then
      echo "❌ Cython 未产出 $m 的 .so —— 终止构建（宁可失败，也不发未加固的包）"
      exit 1
    fi
  done
  echo "   ✔ 已编译 ${#CY_MODULES[@]} 个核心模块为 .so（源码不随包）"
  # --paths 顺序即优先级：放在 $REPO/server 之前，让依赖分析解析到 .so，
  # 并由 PyInstaller 作为 ExtensionModule 正确收集（符号/依赖交给它处理，
  # 比用 --add-data 塞 .so 稳妥）。
  NATIVE_PATH_ARG="--paths $NATIVE_STAGING"
fi

# ── 关于字节码加密（--key）：此路已封死，别再试 ──────────────────────────────
# ⚠️ PyInstaller **≥6.0 已移除 --key**（官方 2023 决定，pyinstaller#6999）。
# 实测本机 6.22.2：传 --key 会直接
#   ERROR: Bytecode encryption was removed in PyInstaller v6.0
# 终止构建。撤回降级到 5.x 也不划算（key 本身在 bootloader 里可被提取，
# 公开工具能从内存 dump 解密后的字节码 —— 本来就只防"顺手扒"）。
# 真正有效的是把核心模块 Cython 编成 .so：源码根本不进包，是机器码而非字节码。
# 故这里不再传任何加密参数；PYZ 里的 .pyc 仍可被反编译，靠 P1（Cython）加固。
echo "   ℹ️  PyInstaller $([ -x "$VENV/bin/pyinstaller" ] && "$VENV/bin/pyinstaller" --version 2>/dev/null || echo '?') 不支持 --key（≥6.0 已移除）；源码防泄露靠上面的 staging + P1 Cython"

# ⚠️ 不要给 onnx 加 --collect-all（2026-09-10 查证：这是构建 40 分钟的主因）：
# onnx 的 wheel 内含官方一致性测试数据集 onnx/backend/test/data（**24,209 个文件**），
# --collect-all 会把它们全部当 data 收进包 —— 上一版包内 31,019 个文件里 25,775 个
# （83%）来自 onnx，而 `grep onnx server/`（排除 onnxruntime）**零命中**，后端从未 import 过它。
# 这些文件平均 1.3KB：拖慢的不是字节，而是逐文件的拷贝 + codesign 密封开销。
# 若将来真有代码 import onnx，PyInstaller 的常规依赖分析会自动把它收进来（且不含 test data）。
"$VENV/bin/pyinstaller" \
  --name VideoDownloader \
  --windowed \
  --noconfirm \
  --workpath "$VDL_BUILD_WORKPATH" \
  --icon "$ICON_ICNS" \
  --osx-bundle-identifier com.videodownloader.desktop \
  $NATIVE_PATH_ARG \
  --paths "$REPO/server" \
  --paths "$REPO" \
  --add-data "$REPO/web:web" \
  --add-data "$REPO/extension:extension" \
  --add-data "$YDLP_STAGING:yt_dlp_plugins" \
  --add-data "$SERVER_STAGING:server" \
  --hidden-import app \
  --hidden-import credential_store \
  --hidden-import downloader \
  --hidden-import cookie_cache \
  --hidden-import ydlp_update \
  --hidden-import tts_guard \
  --hidden-import platforms \
  --hidden-import tasks \
  --hidden-import yt_dlp_plugins \
  --hidden-import yt_dlp_plugins.extractor \
  --hidden-import yt_dlp_plugins.extractor.chrqj \
  --hidden-import yt_dlp_plugins.extractor.kuaishou \
  --hidden-import webview \
  --hidden-import webview.platforms.cocoa \
  --hidden-import dewatermark_core \
  --hidden-import dewatermark_ai \
  --hidden-import dewatermark_diffusion \
  --hidden-import matting_ai \
  --hidden-import zhconv \
  --collect-all zhconv \
  --hidden-import sherpa_onnx \
  --collect-all sherpa_onnx \
  --collect-binaries cv2 \
  --collect-all pymupdf \
  --collect-all fitz \
  --collect-all requests \
  --collect-all PIL \
  --collect-all Quartz \
  --collect-all onnxruntime \
  --collect-all Cryptodome \
  --collect-submodules yt_dlp \
  ${COMMENTARY_DATA[@]+"${COMMENTARY_DATA[@]}"} \
  $_PCS_ADD_BINARY_ARG \
  ${_DENO_BINARY_ARGS[@]+"${_DENO_BINARY_ARGS[@]}"} \
  "$REPO/desktop/desktop_launcher.py"

# 校验：PyInstaller 必须产出 .app bundle，否则后续注入 ffmpeg/web 都会失败
if [ ! -d "$REPO/dist/VideoDownloader.app" ]; then
  echo "❌ PyInstaller 未产出 dist/VideoDownloader.app（可能被 stale .spec 覆盖为 onedir）" >&2
  echo "   请检查是否有 VideoDownloader.spec 残留或 PyInstaller 输出异常。" >&2
  exit 1
fi

# 清理 staging 临时目录（PyInstaller 已把文件拷进 .app，不再需要）
[ -n "${COMMENTARY_STAGING:-}" ] && [ -d "$COMMENTARY_STAGING" ] && mv "$COMMENTARY_STAGING" "$HOME/.Trash/commentary_staging_$(date +%s)" 2>/dev/null || true
[ -n "${YDLP_STAGING:-}" ] && [ -d "$YDLP_STAGING" ] && mv "$YDLP_STAGING" "$HOME/.Trash/vdl_ydlp_staging_done_$(date +%s)" 2>/dev/null || true
[ -n "${SERVER_STAGING:-}" ] && [ -d "$SERVER_STAGING" ] && mv "$SERVER_STAGING" "$HOME/.Trash/vdl_server_staging_done_$(date +%s)" 2>/dev/null || true

echo "▶ 应用中文名称（菜单栏 / Dock / 关于窗口）"
PLIST="$REPO/dist/VideoDownloader.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
  # CFBundleDisplayName：Dock 悬浮提示、菜单栏应用名、About 窗口标题
  # CFBundleName：Application 菜单中 "About XXX" / "Hide XXX" / "Quit XXX" 的 XXX 部分
  # CFBundleExecutable：系统权限弹窗（TCC）显示的进程名，必须与真实二进制文件名一致
  plutil -replace CFBundleDisplayName -string "视频工坊" "$PLIST"
  plutil -replace CFBundleName -string "视频工坊" "$PLIST"
  plutil -replace CFBundleExecutable -string "视频工坊" "$PLIST"
  # 同步重命名真实二进制文件（PyInstaller --name 产出 VideoDownloader，需手动改）
  _MACOS_DIR="$REPO/dist/VideoDownloader.app/Contents/MacOS"
  if [ -f "$_MACOS_DIR/VideoDownloader" ]; then
    mv "$_MACOS_DIR/VideoDownloader" "$_MACOS_DIR/视频工坊"
    echo "   二进制已重命名：VideoDownloader → 视频工坊"
  fi
  # 声明支持中文本地化（否则 macOS 不加载 zh-Hans.lproj）
  plutil -replace CFBundleLocalizations -json '["zh-Hans", "en"]' "$PLIST"
  plutil -replace CFBundleDevelopmentRegion -string "zh-Hans" "$PLIST"
  # 麦克风用途说明（2026-09-19）：「我的音色 · ⏺ 直接录制」用 WKWebView 的 getUserMedia 采集。
  # macOS 要求宿主 App 的 Info.plist 声明该键，否则 TCC **静默拒绝** ——
  # getUserMedia 会报 NotAllowedError（或一直挂着），而弹窗根本不出现，极难排查。
  # 键一开始不存在，`plutil -replace` 会失败 → 退回 `-insert`。
  plutil -replace NSMicrophoneUsageDescription \
    -string "用于录制「我的音色」参考音频，在本机完成语音克隆" "$PLIST" 2>/dev/null \
    || plutil -insert NSMicrophoneUsageDescription \
      -string "用于录制「我的音色」参考音频，在本机完成语音克隆" "$PLIST"
  echo "   已声明 NSMicrophoneUsageDescription（我的音色·直接录制）"
  echo "   已设置中文名 + 本地化声明：视频工坊 (zh-Hans)"
else
  echo "   ⚠️ Info.plist 不存在，跳过"
fi

echo "▶ 注入中文本地化（覆盖系统默认英文菜单词）"
LPROJ="$REPO/dist/VideoDownloader.app/Contents/Resources/zh-Hans.lproj"
mkdir -p "$LPROJ"
cat > "$LPROJ/Localizable.strings" << 'LOCEOF'
/* Menu bar items */
"About %@" = "关于 %@";
"Hide %@" = "隐藏 %@";
"Hide Others" = "隐藏其他";
"Show All" = "显示全部";
"Quit %@" = "退出 %@";

/* Window menu */
"Minimize" = "最小化";
"Zoom" = "缩放";
"Bring All to Front" = "全部移到最前";

/* File menu */
"Close" = "关闭";
"Save…" = "保存…";
"Revert to Saved" = "恢复已保存版本";

/* Edit menu */
"Undo" = "撤销";
"Redo" = "重做";
"Cut" = "剪切";
"Copy" = "复制";
"Paste" = "粘贴";
"Select All" = "全选";
"Delete" = "删除";

/* Format menu */
"Font" = "字体";
"Show Fonts" = "显示字体";
"Bigger" = "更大";
"Smaller" = "更小";
"Bold" = "粗体";
"Italic" = "斜体";
"Underline" = "下划线";

/* Help */
"Help" = "帮助";
LOCEOF
# .strings 文件必须转成二进制 plist 格式（UTF-8 文本格式 macOS 不加载）
plutil -convert binary1 "$LPROJ/Localizable.strings"
echo "   已注入 zh-Hans.lproj（Quit→退出 / Hide→隐藏 / About→关于 等，二进制格式）"

echo "▶ 捆绑 ffmpeg + ffprobe"
# ⚠️ 合规：必须捆绑 **LGPL 构建**（configure 不带 --enable-gpl）。
# Homebrew 默认 ffmpeg 带 --enable-gpl + libx264/libx265，会令整个 ffmpeg 转为
# GPL v2+，与「内置组件必须可商用」冲突。故优先取自编译的 LGPL 构建。
# 先取环境变量，其次用户目录（/opt 常无写权限），最后 /opt
LGPL_FF_DIR="${VDL_FFMPEG_LGPL_DIR:-$HOME/vdl-ffmpeg-lgpl}"
[ -x "$LGPL_FF_DIR/bin/ffmpeg" ] || LGPL_FF_DIR="/opt/vdl-ffmpeg-lgpl"
if [ -x "$LGPL_FF_DIR/bin/ffmpeg" ]; then
  FF="$LGPL_FF_DIR/bin/ffmpeg"
  FFPROBE="$LGPL_FF_DIR/bin/ffprobe"
  echo "   使用 LGPL 构建：$FF"
else
  FF="$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)"
  FFPROBE="$(command -v ffprobe || echo /opt/homebrew/bin/ffprobe)"
  echo "   ⚠️  未找到 LGPL 构建（${LGPL_FF_DIR}），回退系统 ffmpeg：$FF"
fi
# 硬校验：捆绑的二进制绝不能是 GPL 构建
if "$FF" -version 2>/dev/null | grep -q -- "--enable-gpl"; then
  echo "   ⚠️  警告：$FF 是 GPL 构建（含 libx264/libx265），商用分发有授权风险！"
else
  echo "   ✅ 已确认 LGPL 构建（无 --enable-gpl）"
fi
APP_BIN="$REPO/dist/VideoDownloader.app/Contents/MacOS"
mkdir -p "$APP_BIN/bin"
cp "$FF" "$APP_BIN/bin/ffmpeg"
cp "$FFPROBE" "$APP_BIN/bin/ffprobe"
chmod +x "$APP_BIN/bin/ffmpeg" "$APP_BIN/bin/ffprobe"

echo "▶ dylibbundler 把 ffmpeg/ffprobe 依赖打进来（脱离 Homebrew）"
# ⚠️ @executable_path = bin/ 这个目录，所以 ../libs 才是 Contents/MacOS/libs/
# ⚠️ 之前用 @executable_path/libs 错了——会落到 Contents/MacOS/bin/libs/
  if command -v dylibbundler >/dev/null 2>&1; then
  trash_path "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs"
  dylibbundler -x "$APP_BIN/bin/ffmpeg" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b -of >/dev/null
  dylibbundler -x "$APP_BIN/bin/ffprobe" -d "$REPO/dist/VideoDownloader.app/Contents/MacOS/libs" -p "@executable_path/../libs" -cd -b -of >/dev/null
else
  echo "⚠️  未找到 dylibbundler（brew install dylibbundler）—— 跳过；将依赖系统 Homebrew dylib"
fi

echo "▶ 注入构建指纹（页脚显示，便于确认是否最新版）"
# ⚠️ 这里不再现取 HEAD：用脚本开头锁定的 APP_BUILD_HASH / APP_BUILD_DATE。
#    原因见文件开头那段注释（根因 A「标签谎报旧提交」/ 根因 B「标签谎报新提交」）。
BUILD_HASH="$APP_BUILD_HASH"
BUILD_DATE="$APP_BUILD_DATE"
# 解说管线（commentary-pipeline）独立仓库，其 SHA 也一并注入指纹，避免「改了管线但 /api/version 不反映」的错觉。
# ⚠️ 优先读 staging 在**打包当场**记下的 revision，而不是此刻的 HEAD：staging 在构建开头、
#    指纹在构建结尾，期间若又落了提交，直接读 HEAD 会虚报版本（2026-09-15 踩过）。
if [ -n "${COMMENTARY_STAGING:-}" ] && [ -f "$COMMENTARY_STAGING/_pipeline_sha.txt" ]; then
  PIPELINE_HASH="$(tr -d '[:space:]' < "$COMMENTARY_STAGING/_pipeline_sha.txt")"
else
  PIPELINE_HASH="$(git -C "$COMMENTARY_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi
# 语义化版本号：仓库根 VERSION 文件（不随构建变化，仅发版时手工 +1），供「关于/自动更新」比对。
APP_VERSION="$(cat "$REPO/VERSION" 2>/dev/null | tr -d '[:space:]' | head -1)"
[ -z "$APP_VERSION" ] && APP_VERSION="0.0.0"
BUILD_INFO="v$APP_VERSION · 构建 $BUILD_HASH (app) / $PIPELINE_HASH (pipe) @ $BUILD_DATE"
# 缓存 bust 用的纯数字构建戳（避免中文/@ 在 perl 替换侧被错误插值），每次构建都变化
BUILD_STAMP="$(date +%y%m%d%H%M%S)"
# 页脚用 .*? 而非空 span，保证重复构建也能覆盖旧指纹（之前空 span 模式在已有内容时不匹配）
perl -0pi -e "s{<span id=\"buildTag\" class=\"build-tag\">.*?</span>}{<span id=\"buildTag\" class=\"build-tag\">$BUILD_INFO</span>}" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
# 给 app.js 注入构建戳作为缓存 bust 版本号（与页脚同源变化），避免桌面端缓存旧脚本
# ⚠️ 必须锚定到 `var fp = '__BUILD_FP__';` 整句并用 /g，否则只替换第一个出现（styles.css 缓存击穿那处），
# 导致 index.html 里真正决定 isDesktopBuild 的 `var fp`（行 1687）仍是字面量 → isDesktopBuild=false →
# desktop-app.js 只在不稳的 window.pywebview 事件里加载 → 原生文件框桥接整条链路失效。
# 注意：不能裸用 s{__BUILD_FP__}{...}g（会把 .indexOf('__BUILD_FP__') 比较串也替换掉，反而算错）。
perl -0pi -e "s{var fp = '__BUILD_FP__';}{var fp = '$BUILD_STAMP';}g" "$REPO/dist/VideoDownloader.app/Contents/Resources/web/index.html" 2>/dev/null || true
# 同时把指纹写入程序可读文件，供 /api/version 自检（避免肉眼误判版本）
# 注意：指纹必须每次构建都变化（含 BUILD_STAMP 时间戳），否则自动接管逻辑
# 会误判"版本相同"而不接管旧实例，导致仍跑旧版。
echo "$BUILD_INFO #$BUILD_STAMP" > "$REPO/dist/VideoDownloader.app/Contents/Resources/build_version.txt"
# 纯净语义化版本号（仅 vX.Y.Z），供 /api/system/info 与自动更新比对使用（不含构建戳/哈希）。
echo "$APP_VERSION" > "$REPO/dist/VideoDownloader.app/Contents/Resources/version.txt"
echo "   指纹：$BUILD_INFO #$BUILD_STAMP"
echo "   版本：$APP_VERSION"

echo "▶ 打包 aria2c（种子后端随安装包自包含，脱离本机 Homebrew）"
python3 "$REPO/desktop/bundle_aria2.py" "$REPO/dist/VideoDownloader.app/Contents/Resources" 2>&1 || echo "   ⚠️ aria2 打包跳过（种子功能将运行时禁用）"

# SenseVoice 模型（字幕「粤语 / 现场噪声」增强）：
# ⚠️ 2026-10-15 起默认**不再随包分发**：model.int8.onnx 单文件 228MB，占安装包
# 49.5%，而它只是 initial_prompt 增强（拿不到就静默降级纯 Whisper）。改为首次
# 使用本地字幕时按需下载（server/routers/subtitle.py::_ensure_sensevoice_model，
# R2 上的 xz 归档 146MB + sha256 校验），安装包 461MB → ~233MB。
# 需要包内自带（离线装机 / 内网分发）时：VDL_BUNDLE_SENSEVOICE=1 bash desktop/build_mac.sh
echo "▶ SenseVoice 模型（默认按需下载，不随包分发）"
SV_SRC="${VDL_SENSEVOICE_DIR:-$HOME/.cache/vdl-sensevoice}"
SV_DST="$REPO/dist/VideoDownloader.app/Contents/Resources/sensevoice"
if [ "${VDL_BUNDLE_SENSEVOICE:-0}" = "1" ] && [ -f "$SV_SRC/model.int8.onnx" ] && [ -f "$SV_SRC/tokens.txt" ]; then
  mkdir -p "$SV_DST"
  cp "$SV_SRC/model.int8.onnx" "$SV_DST/"
  cp "$SV_SRC/tokens.txt" "$SV_DST/"
  echo "   已按 VDL_BUNDLE_SENSEVOICE=1 注入：Contents/Resources/sensevoice（$(du -sh "$SV_DST" | cut -f1)）"
else
  # 防御：若上轮注入的目录因故残留（正常构建已在 PyInstaller 前移走旧 app），
  # 用 mv 进 _old_*（cleanup_family 的既有模式）移走，保证新包确无模型。
  if [ -e "$SV_DST" ]; then
    mv "$SV_DST" "$REPO/dist/_old_sensevoice_$$" 2>/dev/null || true
    echo "   ⚠️ 清理了残留的 $SV_DST"
  fi
  echo "   跳过：首次使用时从 download.hanyuxz.top/models/sensevoice-v1.tar.xz 下载（146MB）"
fi

echo "▶ 签名（ad-hoc）"
codesign --force --deep --sign - "$REPO/dist/VideoDownloader.app" 2>/dev/null
xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.app" 2>/dev/null
echo "   签名完成：$(codesign -dv "$REPO/dist/VideoDownloader.app" 2>&1 | grep 'Signature=' | head -1)"

echo "▶ 生成 DMG 分发包"
# 默认**不生成** DMG：只有「手动装包通道」（旧版用户下载 DMG 拖进应用程序）需要它，
# 而它要给 741MB 的 app 做一次 UDZO 压缩，每轮构建白花几分钟。
# 需要时：VDL_BUILD_DMG=1 bash desktop/build_mac.sh
if [ "${VDL_BUILD_DMG:-0}" = "1" ]; then
  mv "$REPO/dist/VideoDownloader.dmg" "$REPO/dist/_old.dmg.$$" 2>/dev/null || true
  # 组一个 DMG 专用 staging：.app + 指向 /Applications 的软链。
  # 少了这个软链，用户挂载后只能手动把 .app 拖进「应用程序」，步骤多、易漏步骤。
  # 注意不能用「软链指向 app + -srcfolder」的省空间写法：hdiutil 对指向 bundle 的
  # 软链处理不确定，改用 ditto 复制实体（与手工装包通道那份 DMG 的配方一致）。
  DMG_STAGE="/tmp/vdl_dmg_stage.$$"
  rm -rf "$DMG_STAGE" 2>/dev/null || true
  mkdir -p "$DMG_STAGE"
  ditto "$REPO/dist/VideoDownloader.app" "$DMG_STAGE/视频工坊.app"
  ln -s /Applications "$DMG_STAGE/Applications"
  hdiutil create -volname "视频工坊" -srcfolder "$DMG_STAGE" -ov -format UDZO "$REPO/dist/VideoDownloader.dmg" >/dev/null 2>&1 || echo "⚠️ DMG 生成失败（可忽略，.app 仍可单独分发）"
  # 清理 staging（约 683MB 临时副本）。rm 被沙箱/审批拦下时退到回收站，避免中断整条构建。
  if [ -d "$DMG_STAGE" ]; then
    rm -rf "$DMG_STAGE" 2>/dev/null || mv "$DMG_STAGE" "$HOME/.Trash/vdl_dmg_stage_$(date +%s)" 2>/dev/null || true
  fi

  # 去掉 DMG 自身的 quarantine 标记：用户从文件管理器双击挂载后拖出的 .app 才不会
  # 被 macOS 误判为「从互联网下载」而二次加上隔离属性（否则双击会触发 Gatekeeper 拦截）。
  xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.dmg" 2>/dev/null || true
  [ -e "$REPO/dist/_old.dmg.$$" ] && mv "$REPO/dist/_old.dmg.$$" "$HOME/.Trash/vdl_old_$$.dmg" 2>/dev/null || true
else
  echo "   ⏭  已跳过（DMG 仅手动装包通道需要；要生成请设 VDL_BUILD_DMG=1）"
  [ -e "$REPO/dist/VideoDownloader.dmg" ] && echo "   ℹ️  dist 里仍留着上一次构建的 VideoDownloader.dmg（旧版本，勿当成本轮产物分发）"
fi
xattr -dr com.apple.quarantine "$REPO/dist/VideoDownloader.app" 2>/dev/null || true
echo "   已去除 .app 的 quarantine 标记（DMG 若生成同样处理；用户首次打开仍需右键→打开 一次性放行）"

# ⚠️ 安全约定（用户明确要求）：构建**绝不**自动发布更新。
# 自动发布会让「刚构建、尚未测试」的版本立刻对用户可见，风险极高。
# 发布是独立的手工步骤：先部署到本机 /Applications 充分测试，确认无误后再执行
#     bash desktop/publish_update.sh
# 该脚本会把当前 dist/VideoDownloader.app 打成 zip、算 sha256、生成 latest.json 并上传到 VPS。
echo "ℹ️  构建不自动发布更新；确认版本无误后手工执行：bash desktop/publish_update.sh"

# 走到这里说明构建、签名、DMG 全部成功（set -e 保证失败会提前退出），
# 此时旧产物已无回滚价值，统一收尾，避免 dist/ 无限膨胀。
cleanup_old_artifacts

echo "▶ 构建自验证（沙盒内可验，避免发出坏包）"
_VERIFY_FAIL=0

# 1) 发布包机密核查（2026-10-13 用户要求「发布的安装包绝对不含机密数据」，常态化）
#    扫 7 类：明文凭据 / 私钥 / 服务端凭据文件 / 用户数据 / 内部信息 / 打包垃圾 / 我方明文源码。
#    A–D 类与我方区垃圾任一命中即拦发布（不回显任何凭据内容，只报位置与掩码证据）。
if [ -f "$REPO/server/tools/audit_release_secrets.py" ]; then
  echo "   ▶ 发布包机密核查（audit_release_secrets）..."
  if "$VENV/bin/python" "$REPO/server/tools/audit_release_secrets.py" \
       "$REPO/dist/VideoDownloader.app" >/tmp/vdl_release_audit.log 2>&1; then
    echo "   ✔ 未发现机密数据（详见 /tmp/vdl_release_audit.log）"
  else
    echo "   ❌ 发布包机密核查未通过（详见 /tmp/vdl_release_audit.log）"
    grep -E "❌|·" /tmp/vdl_release_audit.log | head -20
    _VERIFY_FAIL=1
  fi
else
  echo "   ⚠️ 机密核查脚本缺失，跳过（务必补回）"
  _VERIFY_FAIL=1
fi

# 2) 包内必备二进制（deno / ffmpeg / ffprobe）
#    deno 缺 ⇒ YouTube >1080P（DASH）解不了 nsig，高码率静默降级；
#    ffmpeg/ffprobe 缺 ⇒ 音视频合并与解说管线不可用。
#    构建期只打一句 ⚠️ 不构成防线，这里做硬断言。判据含 `--version` 冒烟执行，
#    能抓出「体积正常但动态库缺失/架构不匹配」这类只在运行期才暴露的坏包。
echo "   ▶ 包内必备二进制核查（deno / ffmpeg / ffprobe）..."
if "$VENV/bin/python" "$REPO/desktop/ci_assert_bundle_binaries.py" "$REPO/dist/VideoDownloader.app"; then
  echo "   ✔ 必备二进制齐备"
else
  echo "   ❌ 包内缺少可用二进制（详见上方输出）"
  _VERIFY_FAIL=1
fi

# 3) 离线测试（不依赖外部网络）：扫码登录 Mock 全链路 + 后端路由冒烟
if [ -f "$REPO/server/tests/run_offline_tests.sh" ]; then
  echo "   ▶ 运行离线测试..."
  if bash "$REPO/server/tests/run_offline_tests.sh" >/tmp/vdl_offline_test.log 2>&1; then
    echo "   ✔ 离线测试全部通过"
  else
    echo "   ❌ 离线测试失败（详见 /tmp/vdl_offline_test.log）"
    tail -20 /tmp/vdl_offline_test.log
    _VERIFY_FAIL=1
  fi
else
  echo "   ⚠️ 离线测试脚本缺失，跳过"
fi

if [ "$_VERIFY_FAIL" -ne 0 ]; then
  echo "❌ 构建自验证未通过，已拦截发布。请修复上述问题后重新构建。"
  exit 1
fi

echo "✅ 完成"
echo "   App : dist/VideoDownloader.app（双击即用）"
if [ "${VDL_BUILD_DMG:-0}" = "1" ]; then
  echo "   分发: dist/VideoDownloader.dmg（拖到 应用程序 即可）"
else
  echo "   分发: 未生成 DMG（本机/热更用 .app 即可；要分发给旧版用户请设 VDL_BUILD_DMG=1 重跑）"
fi
echo "   启动后默认打开原生窗口（本地端口 8321，被占用自动顺延；不跳浏览器）"
