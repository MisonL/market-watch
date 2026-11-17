"""盯盘系统路径单一真值源。

历史：扁平目录阶段，11 个模块各自写一遍
`DIR = os.environ.get("MW_DIR") or os.path.expanduser("~/.market-watch")`，
且 `os.path.join(DIR, "state.json")` 这类拼接散布各处。分层后所有路径
决策收敛到本模块 —— 改目录结构只动这里，主模块全部 import。

铁律：`DIR` 受 `MW_DIR` 环境变量覆盖（沙箱纪律）—— 测试必须 `MW_DIR=` 起，
否则 `DIR` 落回生产根，探针写穿线上状态。沙箱根 = 生产根的**完整镜像**，
层子目录在沙箱里同样存在（make_sandbox 负责建）。
"""
import os

# 运行根：生产 ~/.market-watch；沙箱/测试 = MW_DIR 指向的目录。
# ⚠️ 2026-11-05（P2）：原写 `os.environ.get("MW_DIR") or path` —— 空字符串
# （`MW_DIR=` 或 `MW_DIR=$(mktemp -d …)` 取空）被 `or` 吞掉，**静默落回
# 生产根**：你以为在沙箱跑探针，实际在写穿线上 state/logs（沙箱纪律的
# 最坏失效形态）。改为显式 None 判据；给空串则拒绝启动（fail-closed）。
_MW_DIR_ENV = os.environ.get("MW_DIR")
if _MW_DIR_ENV is not None and not _MW_DIR_ENV.strip():
    raise SystemExit(
        "paths: MW_DIR 为空字符串 —— 疑似沙箱变量没取到值；为避免写穿"
        "生产根（~/.market-watch），拒绝启动。unset 它或给一个非空路径。")
DIR = (_MW_DIR_ENV if _MW_DIR_ENV is not None
       else os.path.expanduser("~/.market-watch"))

# 本文件所在 = src/（11 个 .py 所在）。SRC 语义：源码目录。
MAIN_ROOT = os.path.dirname(os.path.abspath(__file__))

# 分层子目录（相对 DIR）。迁移后结构：
#   DIR/
#     src/  11 个 .py（本目录，MAIN_ROOT）
#     conf/ config.json sectors.json plan.md claude-plan.md private-notes.md
#     state/ state.json rate.json pending.json hot_cache.json vr_base.json
#            cache/ history/ DRY_RUN
#     logs/  watch.log *.out *.err *.lock
CONF_DIR  = os.path.join(DIR, "conf")
STATE_DIR = os.path.join(DIR, "state")
LOGS_DIR  = os.path.join(DIR, "logs")
CACHE_DIR = os.path.join(STATE_DIR, "cache")
HIST_DIR  = os.path.join(STATE_DIR, "history")


def p_conf(name):
    return os.path.join(CONF_DIR, name)


def p_state(name):
    return os.path.join(STATE_DIR, name)


def p_log(name):
    return os.path.join(LOGS_DIR, name)


def p_cache(name):
    return os.path.join(CACHE_DIR, name)


def p_hist(name):
    return os.path.join(HIST_DIR, name)

# ---------- launchd 标签前缀（2026-11-17 开源通用化） ----------
#
# `com.mison.market*` 是作者的本地反向域名前缀，对其他人毫无意义。
# 它散落在 webui.py / collect.py / selfcheck.py。
#
# ⚠️ **为什么不能把字面量全改掉**：`selfcheck_mutation.py` 有 2 条变异体
# （`sched-phantom-job`、`push-guard-launchd-gate-dropped`）的注入锚是
# **逐字等于** selfcheck.py / webui.py 里那几行 `com.mison.market*` 字面量。
# 直接改字面量 → 2 条锚点失配 → 回归报假漏网。
#
# 所以采用**运行时重写**：
#   · webui.py   —— 模块级 `_LAUNCHD_PREFIX`，对 JOBS/_DESIGNED_RC 做替换
#   · collect.py —— 从本常量取值，标签写在提示文字里（无锚点依赖）
#   · selfcheck.py —— 字面量原样保留（锚点所在），prefix 只用于**显示**
# 默认值刻意是 `com.mison.market`：改默认会让已有部署的自检、WebUI 面板、
# 定时任务核对全部指向不存在的 label（`launchctl print` 查不到 → 假红）。
# 新用户按 README 设 `MW_LAUNCHD_PREFIX` 即可。
LAUNCHD_PREFIX = os.environ.get("MW_LAUNCHD_PREFIX", "com.mison.market")
