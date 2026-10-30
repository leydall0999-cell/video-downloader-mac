#!/usr/bin/env bash
# publish_update.sh — 手工发布「自更新」版本到阿里云 VPS 更新源。
#
# ⚠️ 安全约定（用户明确要求）：构建（build_mac.sh）**绝不**自动发布更新。
# 自动发布会让「刚构建、尚未测试」的版本立刻对所有用户可见，风险极高。
# 正确流程：
#   1) bash desktop/build_mac.sh                      # 构建
#   2) ditto dist/VideoDownloader.app /Applications/  # 部署到本机
#   3) 在本机上充分测试，确认没有问题
#   4) bash desktop/publish_update.sh                 # 确认后手工发布
# 只有执行到第 4 步，用户端「检查更新」才会看到新版本。
#
# 用法：
#   bash desktop/publish_update.sh                    # 交互确认后发布
#   bash desktop/publish_update.sh --yes              # 跳过确认
#   bash desktop/publish_update.sh --force            # 允许发布不高于当前线上版本号的版本
#   bash desktop/publish_update.sh --full             # 强制本次仅发布全量（即使版本号升级也不生成增量补丁）
#   VDL_RELEASE_NOTES="修复 XX 问题" bash desktop/publish_update.sh
#   VDL_KEEP_BASELINES=3 bash desktop/publish_update.sh   # 历史基线保留版数（默认 2，0=不裁剪）
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO/dist/VideoDownloader.app"
ZIP="$REPO/dist/VideoDownloader.app.zip"
LATEST_JSON="$REPO/dist/latest.json"

VPS_HOST="${VDL_UPDATE_HOST:-root@8.138.223.3}"
VPS_DIR="${VDL_UPDATE_DIR:-/opt/vdl-update}"
PUB_BASE="${VDL_UPDATE_BASE_URL:-http://8.138.223.3:8765}"
NOTES="${VDL_RELEASE_NOTES:-性能优化与问题修复}"
# 更新内容支持多行（2026-09-26）：用户端「关于本应用」按行渲染成列表。
#   VDL_RELEASE_NOTES=$'新增：媒体库搜索\n修复：积分到期提示' bash publish_update.sh
#   或把多行说明写进文件：VDL_RELEASE_NOTES_FILE=/tmp/notes.txt bash publish_update.sh
if [ -n "${VDL_RELEASE_NOTES_FILE:-}" ] && [ -f "${VDL_RELEASE_NOTES_FILE:-}" ]; then
  NOTES="$(cat "$VDL_RELEASE_NOTES_FILE")"
  echo "   更新说明来源 : ${VDL_RELEASE_NOTES_FILE}（$(printf '%s\n' "$NOTES" | grep -c .) 条）"
fi

ASSUME_YES=0
FORCE=0
FULL=0
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --force)  FORCE=1 ;;
    --full)   FULL=1 ;;
    *) echo "未知参数：${arg}（支持 --yes / --force）"; exit 2 ;;
  esac
done

# 版本比较：_ver_ge A B → A >= B 时返回 0，否则返回 1
_ver_ge() {
  python3 - "$1" "$2" <<'PY'
import sys
def p(v):
    n = []
    for x in v.split('.'):
        d = ''.join(c for c in x if c.isdigit())
        n.append(int(d) if d else 0)
    while len(n) < 3:
        n.append(0)
    return tuple(n[:3])
a, b = p(sys.argv[1]), p(sys.argv[2])
sys.exit(0 if a >= b else 1)
PY
}

echo "▶ 发布自更新版本到 VPS 更新源"
echo "   VPS      : $VPS_HOST:$VPS_DIR"
echo "   对外地址 : $PUB_BASE"

# 1) 前置检查
if [ ! -d "$APP" ]; then
  echo "❌ 未找到 $APP —— 请先执行 bash desktop/build_mac.sh 构建。"
  exit 1
fi
if [ ! -f "$REPO/VERSION" ]; then
  echo "❌ 未找到 $REPO/VERSION —— 无法确定版本号。"
  exit 1
fi
VERSION="$(tr -d '[:space:]' < "$REPO/VERSION")"
if [ -z "$VERSION" ]; then
  echo "❌ VERSION 文件为空。"
  exit 1
fi
echo "   本地版本 : v$VERSION"

# 2) 读取线上当前版本，防止误把旧版本发布成最新
REMOTE_VER="0.0.0"
REMOTE_URL=""
# 注意：ssh 必须加 -n，否则它会吃掉脚本的 stdin，导致后面「输入版本号确认」读到空而误判为取消。
if ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VPS_HOST" \
     "test -f '$VPS_DIR/latest.json' && cat '$VPS_DIR/latest.json'" 2>/dev/null >/tmp/vdl_remote_latest.json; then
  REMOTE_VER="$(python3 -c 'import json;print(json.load(open("/tmp/vdl_remote_latest.json")).get("version","0.0.0"))' 2>/dev/null || echo 0.0.0)"
  REMOTE_URL="$(python3 -c 'import json;print(json.load(open("/tmp/vdl_remote_latest.json")).get("url",""))' 2>/dev/null || echo)"
fi
echo "   线上版本 : v$REMOTE_VER"
if _ver_ge "$REMOTE_VER" "$VERSION"; then
  if [ "$FORCE" != "1" ]; then
    echo "❌ 线上版本 v$REMOTE_VER 已不低于待发布版本 v${VERSION}，拒绝发布。"
    echo "   请先把 $REPO/VERSION 改成更大的版本号；确需覆盖请加 --force。"
    exit 1
  fi
  echo "   ⚠️ 已指定 --force，强制覆盖线上 v$REMOTE_VER"
fi

# 3) 打包（ditto 保留 .app 的符号链接结构，解压后能直接运行）
#    注意：必须带 --keepParent，否则 ditto 会把 bundle 内容（Contents/…）平铺到 zip 根部，
#    客户端解压后拿不到 VideoDownloader.app/ 这一层，导致"更新准备失败"。
echo "▶ 打包 ${APP}（保留符号链接结构 + .app 外层目录）"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
ZIP_SIZE="$(stat -f%z "$ZIP")"
ZIP_SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
echo "   更新包: $(du -h "$ZIP" | awk '{print $1}')  sha256=${ZIP_SHA:0:16}…"

# 3.5) 缓存本版「基线 app」目录（供生成文件级 delta 比对，不入版本库）
BASE_DIR="$REPO/desktop/.release_baselines"
NEW_BASE="$BASE_DIR/$VERSION/VideoDownloader.app"
mkdir -p "$(dirname "$NEW_BASE")"
# 用 ditto 覆盖（不先 rm -rf，避免触发沙箱批量删除保护而中断脚本）
if [ -d "$NEW_BASE" ]; then
  ditto "$APP" "$NEW_BASE"
else
  cp -R "$APP" "$NEW_BASE"
fi
echo "   本版基线 app 已缓存: $NEW_BASE"

# 3.6) 尝试生成文件级增量包（已装版本 == 线上版本 时，用户端只下几 MB 差分）
PATCH_BIN=""
PATCH_SHA=""
PATCH_SIZE=0
PATCH_URL=""
FROM_VERSION=""
BSDIFF_BIN="$REPO/desktop/tools/bsdiff"
[ -x "$BSDIFF_BIN" ] || BSDIFF_BIN="$(command -v bsdiff || echo bsdiff)"
if [ "$FULL" != "1" ] && [ "$REMOTE_VER" != "0.0.0" ] && [ "$REMOTE_VER" != "$VERSION" ] && _ver_ge "$VERSION" "$REMOTE_VER"; then
  FROM_VERSION="$REMOTE_VER"
  OLD_BASE="$BASE_DIR/$FROM_VERSION/VideoDownloader.app"
  if [ ! -d "$OLD_BASE" ]; then
    echo "   未找到 $FROM_VERSION 本地基线，尝试从 VPS 下载上一版完整包以生成基线…"
    OLD_ZIP="/tmp/vdl_old_${FROM_VERSION}.zip"
    if [ -n "$REMOTE_URL" ] && curl -fsS --max-time 180 "$REMOTE_URL" -o "$OLD_ZIP" 2>/dev/null; then
      OLD_EXTRACT="/tmp/vdl_old_${FROM_VERSION}"
      rm -rf "$OLD_EXTRACT"; mkdir -p "$OLD_EXTRACT"
      if ditto -x -k "$OLD_ZIP" "$OLD_EXTRACT" 2>/dev/null && [ -d "$OLD_EXTRACT/VideoDownloader.app" ]; then
        mkdir -p "$(dirname "$OLD_BASE")"
        rm -rf "$OLD_BASE"; cp -R "$OLD_EXTRACT/VideoDownloader.app" "$OLD_BASE"
      fi
    fi
  fi
  if [ -d "$OLD_BASE" ]; then
    PATCH_BIN="$REPO/dist/patch-${FROM_VERSION}_${VERSION}.delta"
    if python3 "$REPO/desktop/tools/make_delta.py" "$OLD_BASE" "$NEW_BASE" "$PATCH_BIN" "$BSDIFF_BIN" 2>/dev/null; then
      PATCH_SIZE="$(stat -f%z "$PATCH_BIN")"
      PATCH_SHA="$(shasum -a 256 "$PATCH_BIN" | awk '{print $1}')"
      PATCH_URL="$PUB_BASE/patch-${FROM_VERSION}_${VERSION}.delta"
      echo "   增量更新包: $(du -h "$PATCH_BIN" | awk '{print $1}')  sha256=${PATCH_SHA:0:16}…"
    else
      PATCH_BIN=""; FROM_VERSION=""
    fi
  else
    echo "   ⚠️ 无法获取 $FROM_VERSION 基线，本次仅发布全量更新（用户端自动回退全量）。"
    FROM_VERSION=""
  fi
else
  echo "   首次发布或强制覆盖同版本：仅生成全量更新。"
fi

# 3.7) 裁剪历史基线（必须放在 3.6 之后，否则会删掉刚用到的 from 版本基线）
#      每版基线是一份完整 app（约 700MB），不清理会随版本无限膨胀
#      （2026-09-10 实测累积到 5.8GB / 7 个版本）。
#      保留最近 N 版即可：生成 delta 只用到「线上那一版」的基线，且基线缺失时
#      3.6 会自动从 VPS 下载上一版完整包重建。当前版与线上版永不清。
#      VDL_KEEP_BASELINES=0 可关闭裁剪；默认保留 2 版。
KEEP_BASES="${VDL_KEEP_BASELINES:-2}"
if [ "$KEEP_BASES" -gt 0 ] 2>/dev/null && [ -d "$BASE_DIR" ]; then
  # 只把「版本号形态」的目录当候选（目录里若混入其它文件/目录，一律不碰）
  # BSD sort 无 -V，用逐段数值排序代替语义化版本排序
  _bases_all="$(ls -1 "$BASE_DIR" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+)*$' | sort -t. -k1,1n -k2,2n -k3,3n || true)"
  _bases_n="$(printf '%s\n' "$_bases_all" | grep -c . || true)"
  if [ "$_bases_n" -gt "$KEEP_BASES" ]; then
    _bases_drop="$(printf '%s\n' "$_bases_all" | head -n $((_bases_n - KEEP_BASES)))"
    for _bv in $_bases_drop; do
      # 不用 `[ -z x ] && continue` 写法：set -e 下 && 短路返回 1 的语义有歧义
      if [ -z "$_bv" ]; then
        continue
      fi
      # 保护：当前发布版本与线上版本（生成增量可能依赖后者）永不裁剪
      if [ "$_bv" = "$VERSION" ] || { [ -n "$REMOTE_VER" ] && [ "$_bv" = "$REMOTE_VER" ]; }; then
        continue
      fi
      if [ -d "$HOME/.Trash" ]; then
        if mv "$BASE_DIR/$_bv" "$HOME/.Trash/release_baseline_${_bv}_$(date +%Y%m%d-%H%M%S)" 2>/dev/null; then
          echo "   已裁剪历史基线 ${_bv}（移入回收站，可恢复）"
        fi
      elif rm -rf "$BASE_DIR/$_bv" 2>/dev/null; then
        echo "   已裁剪历史基线 ${_bv}"
      fi
    done
  fi
fi

# 4) 生成 latest.json（发布清单）
#    用 python 写 JSON：notes 允许换行（多行更新内容），手写 heredoc 会破坏 JSON 合法性。
PUB_AT="$(date +%Y-%m-%d)"
VDL_J_VERSION="$VERSION" VDL_J_NOTES="$NOTES" VDL_J_PUB_AT="$PUB_AT" \
VDL_J_URL="$PUB_BASE/VideoDownloader.app.zip" VDL_J_SIZE="$ZIP_SIZE" VDL_J_SHA="$ZIP_SHA" \
VDL_J_FROM="$FROM_VERSION" VDL_J_PATCH_URL="$PATCH_URL" VDL_J_PATCH_SIZE="$PATCH_SIZE" VDL_J_PATCH_SHA="$PATCH_SHA" \
python3 - > "$LATEST_JSON" <<'PY'
import json, os
notes = os.environ.get("VDL_J_NOTES", "")
data = {
    "version": os.environ["VDL_J_VERSION"],
    "notes": notes,
    # notes_list：按行拆好的更新条目，客户端直接渲染列表（兼容旧客户端只用 notes）
    "notes_list": [ln.strip() for ln in notes.replace("\r\n", "\n").split("\n") if ln.strip()],
    "published_at": os.environ["VDL_J_PUB_AT"],
    "url": os.environ["VDL_J_URL"],
    "size": int(os.environ["VDL_J_SIZE"] or 0),
    "sha256": os.environ["VDL_J_SHA"],
    "from_version": os.environ["VDL_J_FROM"],
    "patch_url": os.environ["VDL_J_PATCH_URL"],
    "patch_size": int(os.environ["VDL_J_PATCH_SIZE"] or 0),
    "patch_sha256": os.environ["VDL_J_PATCH_SHA"],
}
print(json.dumps(data, ensure_ascii=False, indent=2))
PY
cat "$LATEST_JSON"

# 4.1) 生成 latest.json 的 Ed25519 签名（安全审计 P0-1）
#      客户端强制验签：清单里的 url / sha256 一旦被中间人篡改，验签必然失败。
#      私钥只存在于发布机，经 VDL_UPDATE_SIGNING_KEY 指定（生成见 tools/gen_update_signing_key.py）。
LATEST_SIG="${LATEST_JSON}.sig"
rm -f "$LATEST_SIG"
# 签名依赖 cryptography —— 它装在构建 venv 里，系统 python3 通常没有
# （2026-10-11 实测：用裸 python3 会 ModuleNotFoundError → 签名失败 → 发布中止）。
# 优先用构建 venv 的解释器，缺失时回退 python3。
SIGN_PY="$REPO/.build_venv/bin/python"
[ -x "$SIGN_PY" ] || SIGN_PY="python3"
SIGN_KEY="${VDL_UPDATE_SIGNING_KEY:-}"
# 🔴 2026-10-30 修复（发布 1.1.05 时踩到）：未设环境变量时原来只 warn 就继续，
#    但脚本开头已经 rm 掉本地 .sig ⇒ 上传步骤没有 .sig 可传 ⇒ 线上留着**上一版**的
#    latest.json.sig，与新清单字节不匹配 ⇒ 客户端 fail-closed 验签必然失败 ⇒ 全员更新卡死。
#    正确处理：先回退发布机默认密钥路径；仍找不到就**中止发布**，绝不产出无签名清单。
if [ -z "$SIGN_KEY" ] && [ -f "$HOME/.vdl_update_signing_key.pem" ]; then
  SIGN_KEY="$HOME/.vdl_update_signing_key.pem"
  echo "   签名私钥（默认路径）: $SIGN_KEY"
fi
if [ -z "$SIGN_KEY" ]; then
  echo "   ✗ 未找到签名私钥（VDL_UPDATE_SIGNING_KEY / ~/.vdl_update_signing_key.pem 都没有）。"
  echo "      客户端强制验签：无签名清单会被全部拒收，线上旧 .sig 又会与新清单不匹配。"
  echo "      生成密钥：$SIGN_PY tools/gen_update_signing_key.py"
  exit 1
elif [ ! -f "$SIGN_KEY" ]; then
  echo "   ✗ VDL_UPDATE_SIGNING_KEY 指向的文件不存在：$SIGN_KEY"
  exit 1
elif "$SIGN_PY" - "$LATEST_JSON" "$SIGN_KEY" <<'PY'
import base64, sys
from cryptography.hazmat.primitives import serialization
path, keyfile = sys.argv[1], sys.argv[2]
key = serialization.load_pem_private_key(open(keyfile, "rb").read(), password=None)
raw = open(path, "rb").read()
open(path + ".sig", "wb").write(base64.b64encode(key.sign(raw)))
PY
then
  echo "   ✔ 已生成签名 latest.json.sig"
else
  echo "   ✗ 签名生成失败（发布机是否已安装 cryptography？）"
  exit 1
fi

# 5) 发布前人工确认（默认开启，避免误发未测试版本）
if [ "$ASSUME_YES" != "1" ]; then
  echo
  echo "⚠️  即将把 v$VERSION 发布为线上最新版本，所有用户的「检查更新」都会看到它。"
  printf "   确认已在本机测试通过？请输入版本号 %s 以继续（直接回车取消）: " "$VERSION"
  read -r confirm || confirm=""
  if [ "$confirm" != "$VERSION" ]; then
    echo "   已取消发布。"
    exit 0
  fi
fi

# 6) 上传
# 顺序：先落地安装包与增量补丁，最后才原子替换 latest.json —— 否则用户端可能读到
# 指向尚未上传完成的包的清单（patch_url 404 / zip 半截）。
# ssh/scp 一律 BatchMode=yes：key 不可用时**立即报错**，而不是退化成密码提示静默空等。
echo "▶ 上传到 $VPS_HOST:$VPS_DIR"
SSH_OPTS="-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15"
ssh -n $SSH_OPTS "$VPS_HOST" "mkdir -p '$VPS_DIR'"

# 大包优先用 rsync 续传：断管后重跑只补差额，不必从头再传 400+MB。
if command -v rsync >/dev/null 2>&1 && ssh -n $SSH_OPTS "$VPS_HOST" "command -v rsync >/dev/null 2>&1"; then
  echo "   · rsync 续传安装包（已传部分自动跳过）"
  rsync -a --partial --append --inplace --progress \
    -e "ssh $SSH_OPTS" "$ZIP" "$VPS_HOST:$VPS_DIR/VideoDownloader.app.zip"
else
  scp $SSH_OPTS "$ZIP" "$VPS_HOST:$VPS_DIR/VideoDownloader.app.zip"
fi
# 🔴 6.1) 远端 sha256 强校验 —— 必须在替换 latest.json **之前**
#   为什么必需：rsync 用的是 --append。若上一次上传被中断（超时被杀 / Ctrl-C），
#   远端会留下「长度不匹配」或「半截」的旧文件；下次 --append 会**接着坏数据往后写**，
#   产出「长度对、内容错」的包，而 rsync 自己**不报错**。此时若照常替换 latest.json，
#   所有用户下载到的包 sha256 与清单不符 → 更新一律失败，脚本却打印「✅ 发布完成」。
#   （2026-10-11 实测踩到：本地/清单 0b383694…，远端却是 811fa459…，同尺寸、不同内容。）
REMOTE_ZIP_SHA="$(ssh -n $SSH_OPTS "$VPS_HOST" \
  "cd '$VPS_DIR' && (sha256sum VideoDownloader.app.zip 2>/dev/null || shasum -a 256 VideoDownloader.app.zip) | awk '{print \$1}'" \
  | tr -d '[:space:]')"
if [ "$REMOTE_ZIP_SHA" != "$ZIP_SHA" ]; then
  echo "   ✗ 远端安装包 sha256 与本地不一致，**中止发布**（latest.json 未替换，用户端不受影响）"
  echo "       本地: $ZIP_SHA"
  echo "       远端: ${REMOTE_ZIP_SHA:-<空>}"
  echo "     根因多为上次上传中断残留半截文件、本次 rsync --append 续在坏数据之后。"
  echo "     处置：ssh $VPS_HOST \"rm -f '$VPS_DIR/VideoDownloader.app.zip'\" 后重跑本脚本。"
  exit 1
fi
echo "   ✔ 远端安装包 sha256 与本地一致（${REMOTE_ZIP_SHA:0:16}…）"
if [ -n "$PATCH_BIN" ]; then
  scp $SSH_OPTS "$PATCH_BIN" "$VPS_HOST:$VPS_DIR/$(basename "$PATCH_BIN")"
  echo "   ✔ 已上传增量补丁 $(basename "$PATCH_BIN")"
fi
# latest.json 与签名原子替换：先传签名、再替换清单 —— 避免用户端读到
# 「有新清单但还没有对应签名」的中间态（那会被客户端判为不可信而拒绝）。
if [ -f "$LATEST_SIG" ]; then
  scp $SSH_OPTS "$LATEST_SIG" "$VPS_HOST:$VPS_DIR/latest.json.sig.tmp"
  ssh -n $SSH_OPTS "$VPS_HOST" "cd '$VPS_DIR' && mv -f latest.json.sig.tmp latest.json.sig"
fi
scp $SSH_OPTS "$LATEST_JSON" "$VPS_HOST:$VPS_DIR/latest.json.tmp"
ssh -n $SSH_OPTS "$VPS_HOST" "cd '$VPS_DIR' && cp -a latest.json latest.json.bak-\$(date +%s) 2>/dev/null || true; mv -f latest.json.tmp latest.json"
echo "   ✔ 已上传安装包、增量补丁、latest.json$( [ -f "$LATEST_SIG" ] && echo ' 与签名' )"

# 7) 校验对外可访问性（外部可达需阿里云安全组放行对应端口）
echo "▶ 校验对外可访问性"
if curl -fsS --max-time 8 "$PUB_BASE/latest.json" >/tmp/vdl_pub_check.json 2>/dev/null; then
  echo "   ✔ 可访问：$PUB_BASE/latest.json"
  python3 -c 'import json;d=json.load(open("/tmp/vdl_pub_check.json"));print("   线上版本:",d.get("version"),"| 更新内容:",(d.get("notes") or "").replace(chr(10)," / "))' 2>/dev/null || true
else
  echo "   ⚠️ 当前环境访问 $PUB_BASE 失败（多半是阿里云安全组未放行该端口）。"
  echo "      文件已上传成功，放行后用户端即可正常检查到更新。"
fi

echo
echo "✅ 发布完成：v${VERSION}"
echo "   更新内容："
printf '%s\n' "$NOTES" | sed 's/^/     · /'
