#!/usr/bin/env bash
# 取「内嵌 Chromium」—— chrome-headless-shell（Chrome for Testing 官方精简构建）
#
# 为什么内嵌：抖音解析必须由**真实 Chromium 内核**发起（抖音 Argus 风控要求设备
# 指纹与浏览器同源），App 外壳是 WKWebView/WebView2，拿不到 CDP 网络层。原来靠
# 用户自己装 Chrome/Edge，等于把「装浏览器」变成使用门槛 —— 2026-11-03 用户否决。
# 内嵌后零安装、零窗口、零外部进程依赖。
#
# 为什么用 headless-shell 而不是完整 Chrome：
#   完整 Chrome 解压 ~470MB，headless-shell 只要 204MB（压缩 99MB），
#   且 2026-11-03 实测能过抖音（冷启 18.4s / 热启 5.2s，br=1380 高码率、带音轨）。
#
# 产物：vendor/chrome-headless-shell-mac-arm64/（不进 git，构建前自动拉）
#
# 用法：bash desktop/fetch_chromium.sh [--force]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VDL_CHROMIUM_VERSION:-156.0.8078.80}"
DEST="$REPO/vendor"

# 与 mac 端 App 架构对齐（构建机是什么架构就打什么架构）
if [ "$(uname -m)" = "arm64" ]; then
  PLATFORM="mac-arm64"
  EXPECT_SHA="${VDL_CHROMIUM_SHA256:-ea615baa2e9caf0d02d1898a6562b5f8fa9c35239707dae0f592178df5b986ac}"
else
  PLATFORM="mac-x64"
  # Intel 构建尚未实测，默认不钉 sha（下载后打印真实值，实测通过后再补进这里）
  EXPECT_SHA="${VDL_CHROMIUM_SHA256:-}"
fi
NAME="chrome-headless-shell-$PLATFORM"
OUT="$DEST/$NAME"
ZIP="$DEST/$NAME.zip"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# 已就位且版本正确就直接复用（构建可重复跑，不重复下 99MB）
if [ "$FORCE" -eq 0 ] && [ -x "$OUT/chrome-headless-shell" ]; then
  HAVE="$("$OUT/chrome-headless-shell" --version 2>/dev/null | awk '{print $NF}' || true)"
  if [ "$HAVE" = "$VERSION" ]; then
    echo "   ✔ 内嵌 Chromium 已就位（$HAVE，$(du -sh "$OUT" | cut -f1)）"
    exit 0
  fi
  echo "   版本不符（现有 ${HAVE:-未知}，需要 $VERSION）→ 重新获取"
fi

mkdir -p "$DEST"
# 国内镜像优先（storage.googleapis.com 在这台机器上实测会被中断），官方作兜底
URLS=(
  "https://registry.npmmirror.com/-/binary/chrome-for-testing/$VERSION/$PLATFORM/$NAME.zip"
  "https://storage.googleapis.com/chrome-for-testing-public/$VERSION/$PLATFORM/$NAME.zip"
)

OK=0
for u in "${URLS[@]}"; do
  echo "   下载 $NAME.zip ← $u"
  if /usr/bin/curl -sL --noproxy '*' --connect-timeout 20 --retry 2 \
        -o "$ZIP" "$u" && [ -s "$ZIP" ]; then
    if unzip -t "$ZIP" >/dev/null 2>&1; then OK=1; break; fi
    echo "   ⚠️ 包不完整，换下一个源"
  else
    echo "   ⚠️ 下载失败，换下一个源"
  fi
done
[ "$OK" -eq 1 ] || { echo "❌ 所有源都取不到 Chromium"; exit 1; }

GOT_SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
if [ -n "$EXPECT_SHA" ]; then
  if [ "$GOT_SHA" != "$EXPECT_SHA" ]; then
    echo "❌ sha256 不匹配（可能是新版本/被篡改）"
    echo "   期望 $EXPECT_SHA"
    echo "   实际 $GOT_SHA"
    exit 1
  fi
  echo "   ✔ sha256 校验通过"
else
  echo "   ⚠️ 未钉 sha256，本次实际值：$GOT_SHA（实测可用后请写回本脚本）"
fi

rm -rf "$OUT"
ditto -x -k "$ZIP" "$DEST/_unzip_tmp"
mv "$DEST/_unzip_tmp/$NAME" "$OUT"
rm -rf "$DEST/_unzip_tmp"
rm -f "$ZIP"

[ -x "$OUT/chrome-headless-shell" ] || { echo "❌ 解压后找不到 chrome-headless-shell"; exit 1; }
echo "   ✔ 就位：$OUT（$(du -sh "$OUT" | cut -f1)）"
