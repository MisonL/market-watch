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

_LAYER_SUBDIRS = {
    "CONF_DIR": ("conf",),
    "STATE_DIR": ("state",),
    "LOGS_DIR": ("logs",),
    "CACHE_DIR": ("state", "cache"),
    "HIST_DIR": ("state", "history"),
}

#这些模块用 `from paths import X`拷走了常量值，refresh时必须同步。
#漏一个的后果 =该模块继续写旧根（「闸门在 ≠闸门生效」的老形态）。
_RESYNC_TARGETS = {
    "runlog": ("HIST_DIR", "RUNS", "PUSHES"),
    "marketlib": ("CACHE_DIR", "LOGS_DIR"),
}


def _resync(vals, old_dir=None):
    """把新路径值写回已 import的消费方模块；失败静默。

    ⚠️**只改仍指向旧根的值**。这条约束是自检当场逼出来的：
    `selfcheck`的 `[执行流水新鲜度]`会把 `runlog.RUNS`桩到临时文件
    （否则会往生产流水塞假记录），然后还原。若 `refresh()`无条件把
    `RUNS`改回 `HIST_DIR/runs.jsonl`，桩就被**改穿**了：
    `[桩泄漏]`一律报「runlog.RUNS被桩掉后未还原」，
    而实际是 refresh在还原**之后**才跑。

    判据：当前值以 `old_dir`开头（=还在旧根下）才改写；
    已被指向别处（桩/沙箱）的原样不动。
    """
    import sys as _sys
    _hist = vals.get("HIST_DIR")
    for _mod_name, _attrs in _RESYNC_TARGETS.items():
        _m = _sys.modules.get(_mod_name)
        if _m is None:
            continue
        for _a in _attrs:
            try:
                _cur = getattr(_m, _a, None)
                if isinstance(_cur, str) and old_dir and \
                not _cur.startswith(old_dir):
                    continue #已被桩/指向别处，不动它
                if _a in ("CONF_DIR", "STATE_DIR", "LOGS_DIR",
                    "CACHE_DIR", "HIST_DIR"):
                    _new_v = vals[_a]
                elif _a == "RUNS" and _hist:
                    _new_v = os.path.join(_hist, "runs.jsonl")
                elif _a == "PUSHES" and _hist:
                    _new_v = os.path.join(_hist, "pushes.jsonl")
                else:
                    continue
                # ⚠️**值相同就不要赋值**。自检「桩泄漏」用 `is`（对象同一性）
                #对账 `_SNAP0`，而 `os.path.join`每次调用都新建 str对象 ——
                #即使内容一样，`is`也会判「被桩掉后未还原」。
                if _cur == _new_v:
                    continue
                setattr(_m, _a, _new_v)
            except Exception:
                pass


def refresh(new_dir=None):
    """按 `MW_DIR`（或显式 `new_dir`）重算全部路径常量，并同步已导入方。

    ##为什么要这个函数

    `DIR`及五个子目录常量原先**只在 import时算一次**。而
    `runlog.py` / `marketlib.py` / `hot.py`写的是
    `from paths import HIST_DIR, CACHE_DIR …` ——那是**值拷贝**，
    之后 `paths.DIR`再变也不会跟着变。

    于是「先 import paths、后设 MW_DIR」的顺序会**静默写穿生产根**。

    `[实测]`2026-11-22复现 ——设 `MW_DIR=$(mktemp -d)`后
    `analyze.p_hist("pushes.jsonl")`仍返回
    `/Users/…/.market-watch/state/history/pushes.jsonl`，一次
    `analyze.push()`就往**生产** `pushes.jsonl`追加了一条合成记录，
    并标记 `ok: true`。

    `run_all.sh`每个测试用**独立子进程**并先设 `MW_DIR`，所以批量跑
    时不暴露；**单独 `python3 tests/xxx.py`就会写穿** ——
    「本地绿 ≠安全」：沙箱在批量路径上有效，在单跑路径上失效。

    ##契约

    ·只改变量，不动已有文件 ——绝不创建/删除任何路径；
    ·幂等：重复调用结果相同；
    ·同步已 import的 `paths`消费方（`_RESYNC_TARGETS`）；
    ·不抛异常（调用点在测试/启动路径上）。
    """
    global DIR, CONF_DIR, STATE_DIR, LOGS_DIR, CACHE_DIR, HIST_DIR
    try:
        _new = new_dir
        if _new is None:
            _env = os.environ.get("MW_DIR")
            _new = (_env if _env is not None
            else os.path.expanduser("~/.market-watch"))
        if not _new:
            return DIR
        _old_dir = DIR
        DIR = _new
        _vals = {"DIR": DIR}
        for _name, _parts in _LAYER_SUBDIRS.items():
            _vals[_name] = os.path.join(DIR, *_parts)
        CONF_DIR = _vals["CONF_DIR"]
        STATE_DIR = _vals["STATE_DIR"]
        LOGS_DIR = _vals["LOGS_DIR"]
        CACHE_DIR = _vals["CACHE_DIR"]
        HIST_DIR = _vals["HIST_DIR"]
        _resync(_vals, _old_dir)
        return DIR
    except Exception:
        return DIR



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
