#!/bin/sh
# 容器入口 —— 启动前的自检与配置引导。
#
# 设计原则：**能明确说清楚的问题就别让它半死不活地跑起来**。
# 这个程序在缺配置时的默认行为是"每一轮都失败、但进程不退出"，
# 在容器里那表现为「容器是 running 状态、日志一直在刷、就是不推送」——
# 这是最难查的形态。所以宁可启动即失败。
set -eu

APP_DIR="${APP_DIR:-/app}"
cd "$APP_DIR"

_ok()   { printf '\033[32m✅ %s\033[0m\n' "$*" >&2; }
_warn() { printf '\033[33m⚠️  %s\033[0m\n' "$*" >&2; }
_die()  { printf '\033[31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

# 带 flush 的 print 不可用（这是 sh）—— 用 stderr 统一输出，
# 否则 stdout 缓冲会让「启动」这行排在后面的错误后面，日志读起来是乱的。
printf "── market-watch 容器启动 ──
" >&2

# ---------- 1. 时区 ----------
# 与 src/scheduler.py 的 check_timezone 双保险：这里先拦一次，
# 让用户在"容器起不来"时就看到原因，而不是等 scheduler 退出后再翻日志。
case "${TZ:-}" in
  Asia/Shanghai|Asia/Chongqing|PRC) _ok "时区 $TZ" ;;
  "") _warn "TZ 未设置，容器默认 UTC —— 程序所有时段判断会偏 8 小时" ;;
  *)  _warn "TZ=$TZ（非北京时间）—— 若时段判断异常请改成 Asia/Shanghai" ;;
esac

# ---------- 2. 必需配置 ----------
# conf/config.json 里有 webhook 等凭据，**不进镜像**（打进镜像等于泄露）。
# 必须由使用者挂载进来。
_missing=""
[ -f conf/config.json ] || _missing="$_missing conf/config.json"
[ -f conf/sectors.json ] || _missing="$_missing conf/sectors.json"
[ -f conf/plan.md ]     || _missing="$_missing conf/plan.md"

if [ -n "$_missing" ]; then
  _die "缺少配置文件:$_missing
   这三个文件含凭据与个人计划，不进镜像，需挂载：
     docker run -v \$PWD/conf:/app/conf ...
   首次使用可从模板开始：
     cp conf/config.example.json conf/config.json   # 再填入 webhook
     cp conf/plan.example.md     conf/plan.md"
fi
_ok "配置文件齐全（config.json / sectors.json / plan.md）"

# sectors.json 是唯一随镜像发布的配置（无可执行凭据），但用户若覆盖了
# 就用自己的 —— 这里只做合法性校验，避免挂错文件。
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY' || _die "conf/sectors.json 不是合法 JSON —— 请检查挂载的文件"
import json, sys
try:
    json.load(open("conf/sectors.json", encoding="utf-8"))
except Exception as e:
    sys.exit(f"  {type(e).__name__}: {e}")
PY
  _ok "sectors.json 合法"
fi

# ---------- 3. 目录与权限 ----------
mkdir -p state logs
# 目录 700、文件 600：自检有一条判据盯着这个（state/logs/lock 不得带
# group/other 位）。容器里 umask 可能是 022，裸 mkdir 会建出 755。
chmod 700 state logs 2>/dev/null || true

if [ ! -w state ] || [ ! -w logs ]; then
  _die "state/ 或 logs/ 不可写 —— 检查挂载卷的所有者（容器内 UID 10001）：
     docker run --user \$(id -u):\$(id -g) ...   或用
     chown -R 10001:10001 ./state ./logs"
fi
_ok "state/ 与 logs/ 可写"

# ---------- 4. 可选依赖 ----------
# curl 是所有行情请求的实际执行者（marketlib.cj）
command -v curl >/dev/null 2>&1 || _die "缺少 curl —— 行情与推送都依赖它"
_ok "curl 就位"

# claude CLI：没有它程序照样跑，但 AI 摘要会全部退回模板兜底。
# 这是"能用但体验差"，所以只警告不拦。
if command -v claude >/dev/null 2>&1; then
  _ok "claude CLI 就位（AI 摘要可用）"
else
  _warn "claude CLI 未安装 —— AI 摘要将全部使用模板兜底（推送仍会正常发出）
   如需 AI 摘要，请在镜像内安装，或把 CLI 的凭据挂载进来"
fi

# ---------- 5. 交接 ----------
if [ "$#" -eq 0 ]; then
  set -- python3 -B src/scheduler.py
fi

printf "── 启动: %s ──\n" "$*" >&2
# exec：让 PID 1 就是目标进程，SIGTERM 才能直达（否则要等 sh 转发，
# 而 sh 收到 SIGTERM 不一定转发给子进程 —— 容器停 10 秒等超时就是这么来的）
exec "$@"
