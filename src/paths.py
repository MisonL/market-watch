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

# 参与「重定根」的模块：凡是 `from paths import …` 拿走常量值、或在模块层
# 用这些常量拼过路径的，都在这里。**不要在别处再维护一份名字清单**。
_RESYNC_MODULES = ("marketlib", "runlog", "hot", "analyze", "collect",
                   "feeds", "alert", "webui")


def _under(path, root):
    """`path` 是否在 `root` 之下 —— **按路径分量**判断，不用裸 startswith。

    `[实测]`2026-11-22：`old_dir="/tmp/mw"` 时 `/tmp/mw-probe/runs.jsonl`
    会被裸 startswith 判成"旧根内的规范路径"并被覆盖 —— 一个**外部桩**
    被抹成部署流水路径，桩后的写入直接落到生产历史里。
    """
    try:
        if not isinstance(path, str) or not path or not root:
            return False
        p = os.path.normpath(path)
        r = os.path.normpath(root)
        return p == r or p.startswith(r + os.sep)
    except Exception:
        return False


def _rebase(path, old_dir, new_dir):
    """把旧根下的路径改挂到新根；算不出相对路径就原样返回。"""
    try:
        rel = os.path.relpath(os.path.normpath(path),
            os.path.normpath(old_dir))
        return os.path.join(new_dir, rel)
    except Exception:
        return path


def _resync(old_dir, new_dir):
    """把所有**仍落在旧根下**的模块级路径常量重定根到新根。

    ## 为什么是"扫描"而不是"名字清单"

    原先这里维护 `_RESYNC_TARGETS`，只列了 `runlog.{HIST_DIR,RUNS,PUSHES}`
    与 `marketlib.{CACHE_DIR,LOGS_DIR}`。实际漏了**派生常量**：
    `runlog.BEAT_FILE`、`marketlib.DCACHE_FILE`、`hot.CACHE_DIR` ——
    它们同样在 import 时用 paths 常量拼好、同样被值拷贝固化。

    后果：换成新沙箱后流水迁走了，**心跳与行情缓存仍写旧根**。
    同一个「闸门在 ≠ 闸门生效」，只是换了一层。
    `[实测]`2026-11-22：A→B 之后 `runlog.RUNS` 在新根，而
    `runlog.BEAT_FILE` / `marketlib.DCACHE_FILE` / `hot.CACHE_DIR` 仍在 A。

    所以改成**扫描** `_RESYNC_MODULES` 里每个模块的模块级 str 属性，
    凡落在旧根下的一律重定根。名单从"逐个名字"退化成"哪些模块"，
    新增派生常量不必再回来改这里（这一类漏改就不会再发生）。

    ## 契约

    ·只改**落在旧根下**的值：指向别处（外部桩、临时探针）的原样不动；
    ·边界按路径分量判断（见 `_under`）；
    ·值相同不赋值 —— 自检[桩泄漏]用 `is` 对账，重建 str 会假报未还原；
    ·失败静默，绝不抛异常。
    """
    import sys as _sys
    if not old_dir or not new_dir:
        return
    try:
        if os.path.normpath(old_dir) == os.path.normpath(new_dir):
            return
    except Exception:
        return
    for _name in _RESYNC_MODULES:
        _m = _sys.modules.get(_name)
        if _m is None:
            continue
        for _attr, _cur in list(vars(_m).items()):
            if not _under(_cur, old_dir):
                continue
            try:
                _new_v = _rebase(_cur, old_dir, new_dir)
                if _new_v != _cur:
                    setattr(_m, _attr, _new_v)
            except Exception:
                pass


def refresh(new_dir=None):
    """按 `MW_DIR`（或显式 `new_dir`）重算全部路径常量，并同步已导入方。

    ## 为什么要这个函数

    `DIR` 及五个子目录常量原先**只在 import 时算一次**。各模块写的
    `from paths import HIST_DIR, CACHE_DIR …` ——那是**值拷贝**，
    之后 `paths.DIR` 再变也不会跟着变。

    于是「先 import paths、后设 MW_DIR」的顺序会**静默写穿生产根**。
    `[实测]`2026-11-22：设 `MW_DIR` 到临时目录后
    `analyze.p_hist("pushes.jsonl")` 仍返回生产路径，一次 `push()`
    就往生产流水追加了一条合成记录。`run_all.sh` 每个测试独立子进程
    并**先设** `MW_DIR`，所以批量跑不暴露；单独跑就会写穿。

    ## 契约

    ·只改变量，不动已有文件 ——绝不创建/删除任何路径；
    ·幂等：重复调用结果相同；
    ·**空值/空白一律拒绝**（与 import 期同一套 fail-closed，见文件头）；
    ·派生常量走 `_resync` 扫描重定根；
    ·不抛其它异常（调用点在测试/启动路径上）。
    """
    global DIR, CONF_DIR, STATE_DIR, LOGS_DIR, CACHE_DIR, HIST_DIR
    _new = new_dir
    if _new is None:
        _new = os.environ.get("MW_DIR")
        if _new is None:
            _new = os.path.expanduser("~/.market-watch")
    # ⚠️2026-11-22：与文件头 import 期**同一套** fail-closed 判据。
    # 原实现收下空串后"静默保留旧根"（你以为换了沙箱，其实还在生产根），
    # 收下纯空白后把它当根 —— `p_state()` 于是返回**相对路径**，
    # 写入落到当前工作目录。两种都是"沙箱没生效但不报错"。
    if not isinstance(_new, str) or not _new.strip():
        raise SystemExit("paths.refresh: MW_DIR 为空/空白 —— 拒绝切换到未定义根，会写穿到当前目录或静默留在旧根。")
    _new = os.path.abspath(os.path.expanduser(_new))
    try:
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
        _resync(_old_dir, DIR)
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
