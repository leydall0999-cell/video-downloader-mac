#!/usr/bin/env bash
# setup_periodic.sh — VDL 国内服务器「周期任务」一次性恢复脚本。
#
# 用途：服务器重装 / 新部署后，把巡检等周期任务重新挂上，避免「手敲 crontab 随重装丢失」。
#        本脚本与 vdl-platform-health.{service,timer} 一起纳入 git（desktop/server-periodic/），
#        来源真值在 mac 仓，ECS 上只放副本，改了以 git 为准。
#
# 覆盖：
#   1. 平台健康巡检  → systemd timer（每天 04:30 / 16:30，替代原 crontab 手敲行）
#   2. bilibili cookie 保活 → 已有 vdl-cookie.service，确保 enabled
#   3. 证书续期(acme.sh) → 标准 acme 自带 cron，这里只做存在性检查与提示
#
# 用法（ECS 上，需 root）：
#   sudo bash setup_periodic.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD=/etc/systemd/system

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; NC=$'\033[0m'
ok(){ echo "${GREEN}✓${NC} $*"; }
bad(){ echo "${RED}✗${NC} $*"; }
warn(){ echo "${YEL}!${NC} $*"; }

[[ $EUID -eq 0 ]] || { bad "请用 root 运行：sudo $0"; exit 1; }

echo "==> [1/3] 安装平台健康巡检 timer"
install -m 644 "$SRC/vdl-platform-health.service" "$SYSTEMD/"
install -m 644 "$SRC/vdl-platform-health.timer"   "$SYSTEMD/"
systemctl daemon-reload
systemctl enable --now vdl-platform-health.timer
if systemctl is-enabled --quiet vdl-platform-health.timer; then
  ok "vdl-platform-health.timer 已启用（每天 04:30 / 16:30）"
else
  bad "timer 未成功启用，查：systemctl status vdl-platform-health.timer"
fi

echo "==> [2/3] bilibili cookie 保活 service"
if [ -f "$SYSTEMD/vdl-cookie.service" ]; then
  systemctl enable --now vdl-cookie.service 2>/dev/null || true
  ok "vdl-cookie.service 已确保启用"
else
  warn "未发现 $SYSTEMD/vdl-cookie.service —— 若 cookie 保活依赖原 crontab 行，请确认 start_bili_cookie.sh 仍在"
fi

echo "==> [3/3] 证书续期 (acme.sh)"
if command -v crontab >/dev/null && crontab -l 2>/dev/null | grep -q "acme.sh --cron"; then
  ok "acme.sh 续期 cron 仍在"
else
  warn "未检测到 acme.sh 续期 cron —— 重装后需重装 acme.sh 或补回："
  warn '   55 4,10,16,22 * * * "/root/.acme.sh"/acme.sh --cron --home "/root/.acme.sh" > /dev/null'
fi

echo
ok "周期任务恢复完成。查看下次触发：systemctl list-timers vdl-platform-health.timer"
