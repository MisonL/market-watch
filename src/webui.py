#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盯盘系统 WebUI —— 执行情况调阅面板（本机只读）。

存在理由：系统原先**不记录"每一轮是否执行"**。analyze.main() 有 5 条静默
return 路径（非交易日 / 时间窗外 / 抢锁失败 / 无触发 / 配额用尽），全部不写
日志 —— watch.log 1446 行里 [RUN] 标记 0 个。所以"某时刻那一轮跑没跑、
为什么没推"无法回放，只能靠 launchctl 的累计计数。

现在 history/runs.jsonl + history/pushes.jsonl 补上了这个缺口（见 runlog.py），
本文件把它渲染成可调阅的页面。

设计约束：
  · **只读**。不实现任何写方法（一律 405），不提供手动补跑 —— 加错也不影响
    主链路。用 ast 静态断言无 do_POST 定义。
  · **仅绑 127.0.0.1**。本机即信任边界，不做认证。
  · **纯 stdlib**，不引入 Flask/Jinja2，不用 CDN（离线可用）。
  · **不 import analyze 的 CFG**（含 webhook key）；webhook 只以布尔值出现。
  · **不渲染 private-notes.md**（个人持仓）。
"""
import os
import re
import socket
import sys
import ast
import json
import time
import errno
import threading
import datetime
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Any, NotRequired, TypedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ⚠️ 2026-11-01 修（P2）：分层后 `paths.py` 成了新的单点故障，而本模块在
# **模块级** import 它 —— 早于 main() 的启动自检 `_sources_sane`。paths.py
# 一坏（改代码的常见中间态）就崩在任何自检看到之前，被
# `KeepAlive{SuccessfulExit:false}` 反复拉起、反复崩溃，面板长期不可用，
# 且没有任何自动恢复路径。
# 处置：**原地等待**（与 main() 的 _sources_sane 等待循环同一策略）——
# 进程活着（launchd 不会再拉起第二个），每 30s 重试导入，paths.py 一修好
# 就继续正常启动。既不会崩溃循环，也不会永久死亡。
#
# ⚠️ 等待循环**只对直接执行本文件成立**（`__name__ == "__main__"`）。
# 被 `import webui` 时（selfcheck 读常量就会）绝不能无限等待 ——
# 那会把"导入失败"变成"调用方永久挂起"，比原来的即时 ImportError 更糟
# （自检会卡死而不是报 ❌）。故 import 语境下直接抛出，交给调用方处理。
def _import_paths():
    try:
        from paths import DIR  # noqa: E402  （路径单一真值源，见 src/paths.py）
        from paths import (p_state, p_conf, p_log, p_cache, p_hist,  # noqa: E402
                           HIST_DIR, STATE_DIR, LOGS_DIR, MAIN_ROOT)
        return DIR, p_state, p_conf, p_log, p_cache, p_hist, HIST_DIR, STATE_DIR, LOGS_DIR, MAIN_ROOT
    except Exception as _paths_e:      # noqa: BLE001
        if __name__ != "__main__":
            raise
        print(f"⚠️ paths.py 暂不可导入（{type(_paths_e).__name__}: {_paths_e}）"
              f" —— 不退出，每 30s 重试（修好后自动启动）", flush=True)
        return None


while True:
    _p = _import_paths()
    if _p is not None:
        (DIR, p_state, p_conf, p_log, p_cache, p_hist,
         HIST_DIR, STATE_DIR, LOGS_DIR, MAIN_ROOT) = _p
        break
    time.sleep(30)
HOST = os.environ.get("MW_HOST", "127.0.0.1")
PORT = int(os.environ.get("MW_PORT", "8787"))

# ⚠⚠ 2026-11-10（安全审查 S2）：面板**零鉴权**。若绑到非回环地址，同网段任何人都能
# 读到：推送正文（含 AI 结论）、pushes.jsonl 全部历史、watch.log、板块持仓口径、
# launchd 状态，以及 /api/log 里的 AI prompt 片段。
# 原实现只打一行警告就继续启动 —— 一个环境变量就能把它暴露出去。
# 改为**拒绝启动**：要对外提供，请先加鉴权，而不是改一个环境变量。
def _is_loopback(h):
    _h = (h or "").strip().lower()
    if _h in ("localhost", "::1", ""):
        return True
    return _h.startswith("127.")


MW_HOST_ALLOW_REMOTE = os.environ.get("MW_HOST_ALLOW_REMOTE") == "1"
if not _is_loopback(HOST) and not MW_HOST_ALLOW_REMOTE:
    raise SystemExit(
        f"拒绝启动：MW_HOST={HOST!r} 不是回环地址，而面板无鉴权。\n"
        f"如确实需对外提供，请同时设 MW_HOST_ALLOW_REMOTE=1，"
        f"并确认你了解风险（含推送正文与持仓口径）。")

HIST = HIST_DIR
RUNS = os.path.join(HIST, "runs.jsonl")
PUSHES = os.path.join(HIST, "pushes.jsonl")
STATE = p_state("state.json")
PENDING = p_state("pending.json")
RATE = p_state("rate.json")
# ⚠️ 与 `hot.HOT_CACHE` 必须同路径（面板读的就是 hot.analyze_cached 落的那份）。
# 用 ast 从 hot.py 静态取真值太脆（hot.py 顶部有 import 副作用），故直接
# 拼接 —— 与既有 RUNS/PUSHES 等常量的写法一致。改动 hot.HOT_CACHE 位置时
# 必须同步这里（已有自检项覆盖「面板常量与实现漂移」这一类）。
HOT_CACHE_PATH = p_state("hot_cache.json")
SECTORS = p_conf("sectors.json")
CONFIG = p_conf("config.json")
LOG = p_log("watch.log")
LOG1 = LOG + ".1"

# ⚠️ 2026-11-17（开源通用化）：前缀在**运行时**重写，不改字面量。
# 为什么不用 `paths.launchd_label()` 直接拼：本文件里这几段字符串同时是
# `selfcheck_mutation.py` 的**变异体注入锚**（锚的 old 文本逐字等于这里），
# 一改就 25 条锚点集体失配。所以做法是——字面量保持原样，下面统一把
# 前缀换成配置值（默认仍是 `com.mison.market`，不设环境变量时行为完全不变）。
_LAUNCHD_PREFIX = os.environ.get("MW_LAUNCHD_PREFIX", "com.mison.market")
JOBS = ["com.mison.marketwatch", "com.mison.marketcollect",
        "com.mison.marketwebui", "com.mison.marketselfcheck",
        # ⚠️ `marketagent`（调研 Agent）原先**不在名单里**，面板上看不到它 ——
        # 于是「agent 今天到底跑没跑」在这块面板上是零信息（2026-11-01 补）。
        # 它的 rc 语义与其它任务不同（见 selfcheck 的同类判据）：真任务只在
        # 16:05~17:55 执行，其余 23 小时无进程是常态。
        "com.mison.marketagent"]
JOBS = [(j.replace("com.mison.market", _LAUNCHD_PREFIX, 1)
         if _LAUNCHD_PREFIX != "com.mison.market" else j) for j in JOBS]

# 设计内非零退出码：面板不该把它们显示成红色故障
# （判据与 selfcheck 的 `_ONESHOT_OK_RC` / webui rc=3 注释同源）。
_DESIGNED_RC = {
    "com.mison.marketwebui": {3},          # os._exit(3) = 源码变更自重启信号
    "com.mison.marketselfcheck": {1},      # rc=1 = 自检的**结论**（发现异常）
}
if _LAUNCHD_PREFIX != "com.mison.market":
    _DESIGNED_RC = {(k.replace("com.mison.market", _LAUNCHD_PREFIX, 1)): v
                    for k, v in _DESIGNED_RC.items()}

# ---------- 与 analyze 共享的常量：**启动时静态读取，不复制** ----------
#
# 为什么不用 `import analyze`：那会触发连网、写 state.json 等副作用
# （webui 是只读面板，绝不能碰主链路的数据）。
# 为什么不用**手工副本**：实测 2026-09-24 一天内就漂移了三次 ——
#   · role 改名后这里没同步 → 面板显示「上午小结+下午预案」，
#     而群里收到「上午小结」
#   · SLOT_TTL 的 15:00 从 5 放宽到 20 后这里没同步 →
#     一次**成功**的补推被面板标成 "missed"（假告警）
# 手工副本靠 selfcheck 事后发现，不够 —— 面板当天就是错的。
# 故改为**用 ast 静态解析 analyze.py 取常量**（零副作用、35ms），
# 让"漂移"在结构上不可能发生。
#
# ⚠️ 必须有回退值：用户改 analyze.py 时出现语法错误是常见中间状态，
# 此时 ast.parse 会抛 SyntaxError。回退到下面这份**兜底副本**，
# 保证面板仍可用（只是数值可能略旧），而不是整个面板挂掉。
# ⚠️ 兜底副本必须与 analyze.SLOT_TTL 一致。09:25 的取值史：
#   30 -> 5（原值 30 会让"盘前简报"在开盘后仍被补推，前提已失效）
#   5  -> 8（5 分钟是**零冗余**的：窗口宽 300s 恰好等于调度间隔 300s，
#            窗口内只可能有一轮，而它因漂移常落在 09:29 之后，AI 稍慢
#            就推到 09:30 以后。实测 2026-09-29 那轮落在 09:29:34、
#            跑 62s、09:30:36 送达）
# selfcheck 的 [常量一致性] 会比对这两处 —— 上面这次 5->8 的改动就是
# 被它抓出来的（报 `analyze=8 vs 兜底=5`）。
_FALLBACK_SLOT_TTL = {"09:25": 8, "10:00": 20, "11:30": 20,
                      "13:30": 20, "14:30": 15, "15:00": 20, "summary": 13,
                      "deadline": 95}
# 槽位窗口的提前开放分钟数，必须与 analyze.SLOT_OPEN_EARLY 一致。
# 用途只有一个：`_slot_conclusion` 的日志回退容差下界 —— 提前开放后，
# 09:25 的推送可能落在 09:17~09:25 之间，容差仍写死 -5 就认领不到，
# 那些轮次会被判成"当日无该时段推送记录"（假告警）。
# 同样被 selfcheck 的 [常量一致性] 比对。
# 重试宽限，必须与 analyze.HOURLY_RETRY_GRACE 一致（同被 [常量一致性] 比对）。
_RETRY_GRACE = 15
_FALLBACK_SLOT_OPEN_EARLY = {"09:25": 8}
_FALLBACK_SLOTS = [
    ("09:25", "盘前简报", "09:25"),
    ("10:00", "开盘异动", "10:00"),
    ("11:30", "上午小结", "11:30"),
    ("13:30", "午后开局", "13:30"),
    ("14:30", "决策时段", "14:30"),
    ("summary", "收盘前汇总", "14:45~14:58"),
    ("deadline", "截止提醒", "13:30 后（观察期最后一日）"),
    ("15:00", "收盘定局", "15:00"),
]


# 需要从 analyze.py 静态读取的模块级常量。
# ⚠️ 三者都必须是**字面量**（ast.literal_eval 能求值），否则读不到。
# 原先 docstring 声称还取 HOURLY —— 那是假的：analyze 里 HOURLY 是
# `[(t, int(t[:2])*60+int(t[3:])) for t in TH["hourly"]]`，literal_eval
# 求不了列表推导式，所以它**从来没被取到过**。这里如实写明，不再假装。
_CONST_NAMES = ("SLOTS", "SLOT_TTL", "MAX_PUSH", "SLOT_OPEN_EARLY")

# MAX_PUSH 的兜底值：analyze.py 语法错误期间用它，保证面板仍可用。
# 必须与 analyze.MAX_PUSH 一致 —— selfcheck 的 [常量一致性] 会比对两处。
_FALLBACK_MAX_PUSH = 10


def _analyze_consts() -> dict[str, object]:
    """静态解析 analyze.py，取 _CONST_NAMES 里的模块级常量。

    纯 ast，不执行 analyze 的任何代码 —— 无副作用。
    返回 {名字: 值}；解析失败、或某个常量不是字面量时该键**缺失**，
    由调用方各自回退到兜底副本。
    """
    import ast as _ast
    fp = os.path.join(MAIN_ROOT, "analyze.py")
    try:
        with open(fp, encoding="utf-8") as _fh:
            _tree = _ast.parse(_fh.read())
    except Exception:
        return {}
    _out: dict[str, object] = {}
    for _n in _tree.body:
        if isinstance(_n, _ast.Assign):
            for _t in _n.targets:
                if isinstance(_t, _ast.Name) and _t.id in _CONST_NAMES:
                    try:
                        _out[_t.id] = _ast.literal_eval(_n.value)
                    except Exception:
                        pass
    return _out


_A_CONST = _analyze_consts()


def _dict_const(name):
    """取 analyze.py 里的常量，**只接受 dict**，否则返回空 dict（回退兜底副本）。

    ⚠️⚠️ 为什么必须校验形状（2026-10-29 修，P1 崩溃循环）：
    原写法是 `_A_CONST.get("SLOTS") or {}` —— 只挡得住 `None` / `{}`，
    **挡不住形状错**。`_analyze_consts()` 用 `ast.literal_eval` 取值，
    任何**合法字面量**都会原样返回：把 analyze.py 的 `SLOTS` 改成
    `[1, 2]`（或 `"x"`、`42`），`[1,2] or {}` 仍是 `[1,2]`，
    下面的 `_A_SLOTS.items()` 当场 `AttributeError`。
    `[实测]` 沙箱把 SLOTS 改成 `[1,2]`：
        AttributeError: 'list' object has no attribute 'items'  → 进程 rc=1
    而 plist 是 `KeepAlive{SuccessfulExit:false}` —— **无节流重启**，
    面板永久不可用，且每 10 秒刷一条 launchd 日志。

    为什么不在 `_sources_sane()` 里列 analyze.py 挡住？那段 docstring 写得很
    清楚：**故意的**。analyze.py 有语法错误时，webui 靠兜底副本仍能启动，
    这正是"改代码改到一半"时面板不该跟着一起死的原因。形状错与语法错是
    **同一类**（analyze.py 不可信），所以修法也必须同源：**逐项回退**，
    而不是拒绝启动。语法错由 `_analyze_consts()` 的 except 兜住，形状错
    由本函数兜住 —— 两条路径汇到同一个"用兜底副本"，语义一致。
    """
    v = _A_CONST.get(name)
    return v if isinstance(v, dict) else {}


_A_SLOTS = _dict_const("SLOTS")
_A_SLOT_TTL = _dict_const("SLOT_TTL")
# ⚠️ 这里必须用显式类型判断，**不能**写 `or _FALLBACK_MAX_PUSH`：
# 用户把推送上限调成 0（= 当天不推事件类）是合法配置，而 `0 or 6` 会
# 把它当成"读不到"回退成 6 —— 面板显示 "0 / 6"，实际配额却是 0。
_A_MP_RAW = _A_CONST.get("MAX_PUSH")
_A_MAX_PUSH = _A_MP_RAW if isinstance(_A_MP_RAW, int) else _FALLBACK_MAX_PUSH

# 各时段的时效上限。analyze 侧为准，读不到时用兜底值。
# deadline 的触发窗口是 13:30~14:45 整段（每轮都可能命中，推成功后靠
# fired 去重），容差给足 —— 这个键 analyze 侧没有，是 webui 特有的展示参数。
# ⚠️ 解析失败（analyze.py 有语法错误）时 _A_SLOT_TTL 是空 dict ——
# 此时必须回退到兜底副本，否则面板上所有时段的容差都取默认值，
# 会把成功的推送判成 missed（假告警）。实测踩过这个漏：只写了
# _FALLBACK_SLOT_TTL 却没接上，回退时 _SLOT_TTL["15:00"] 是 None。
# ⚠️ 值的形状也要校验（2026-10-29 修，P2）：`dict()` 只保证**外层**是 dict。
# 若 analyze.py 里写成 `SLOT_TTL = {"09:25": "5"}` 或 `{"09:25": [5]}`，
# 这里会原样收下，然后 `/api/slots` 里 `deadline - ttl` 抛 TypeError →
# 500 → 前端 `Promise.all` 整页白。启动时不炸、**请求时才炸**，
# 排查成本高得多。非数值一律丢弃，让 setdefault/兜底副本接管。
# ⚠️ 以**兜底副本为底**、用解析出的合法值覆盖（2026-10-29 再修）：
# 只丢弃非法值会留下"部分时段缺容差"的空洞 —— 缺失的键走调用点的
# `.get(key, 20)` 默认值，那是另一个魔数，与兜底副本的值可能不同，
# 于是同一个时段在"整体解析失败"和"个别值写错"两种情形下容差不一样。
# 以兜底副本打底就消除了这个分叉：合法值覆盖、非法值静默保留兜底值。
_SLOT_TTL = dict(_FALLBACK_SLOT_TTL)
_SLOT_TTL.update({k: v for k, v in _A_SLOT_TTL.items()
                  if isinstance(v, (int, float)) and not isinstance(v, bool)})
_SLOT_TTL.setdefault("summary", 13)
_SLOT_TTL.setdefault("deadline", 95)

# ⚠️ 与 _SLOT_TTL 同理：解析失败时必须回退，否则回退期容差下界恒为 -5，
# 提前开放的那几分钟全被判成"无推送记录"。
_A_OPEN_EARLY = _dict_const("SLOT_OPEN_EARLY")
# ⚠️ 与 _SLOT_TTL 同款：以兜底副本打底 + 只让合法值覆盖（同属"请求时才炸"
# 的 500 源，也同属"部分缺失会漂到另一个魔数默认值"的分叉）。
_SLOT_OPEN_EARLY = dict(_FALLBACK_SLOT_OPEN_EARLY)
_SLOT_OPEN_EARLY.update({k: v for k, v in _A_OPEN_EARLY.items()
                         if isinstance(v, (int, float)) and not isinstance(v, bool)})

# 语义时段。计划时刻（第三列）是 webui 特有的展示文案；
# 时段名（第二列）从 analyze.SLOTS 的 role 取，保证与推送标题一致。
_PLANNED = {"09:25": "09:25", "10:00": "10:00", "11:30": "11:30",
            "13:30": "13:30", "14:30": "14:30", "summary": "14:45~14:58",
            "deadline": "13:30 后（观察期最后一日）", "15:00": "15:00"}
# ⚠️ 逐项校验 `v` 是 dict（2026-10-29 修，P1）：外层是 dict 不够 ——
# `SLOTS = {"09:25": "开盘"}` 这种"把 role 直接写成字符串"的简写会让
# `v.get("role", k)` 抛 AttributeError，同样是 import 期崩溃。
if _A_SLOTS:
    SLOTS = [(k, v.get("role", k), _PLANNED.get(k, k))
             for k, v in _A_SLOTS.items()
             # event 是事件类推送的内部定位，不属于任何整点时段 ——
             # 面板按时段列行，本就没有它的位置。
             if k in _PLANNED and isinstance(v, dict)]
else:
    SLOTS = _FALLBACK_SLOTS

# 日志行：`[MM-DD HH:MM:SS][TAG] message`
LOG_RE = re.compile(r"^\[(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\](?:\[([A-Z]+)\])? (.*)$")

# 折叠规则：连续重复的噪音行合并成一个可视条目（不丢数据，点开能看全部）
# ⚠️ 每条都必须是**锚定**的完整模式。
# 原先最后一条写 `^\[?RATE\]?|熔断|日配额已耗尽` —— 正则里 `|` 优先级最低，
# 所以它是三个分支：`^\[?RATE\]?`、`熔断`、`日配额已耗尽`。
# 后两个是**裸子串**，于是任何提到"熔断"的正常日志都被折进"限流/熔断"组：
#   `[FEED] 备用源配额/熔断限制，跳过: ...`  -> 被当成"限流/熔断"
#   `主机 push2delay 1小时内失败6次，熔断5分钟` -> 同上
# 前者其实是**本地配额耗尽**（零网络请求、不记主机故障），与"主机熔断"
# 是两回事；折叠组标签会误导排查方向。
# 逐条锚定到消息开头（日志正文就是这些固定前缀），消除误折叠。
FOLD_PATTERNS = [
    ("save_st 失败", re.compile(r"^save_st 失败")),
    ("quotes 降级到备用源", re.compile(r"^quotes 降级到备用源")),
    ("主源不可用/切换", re.compile(r"^主源不可用，行情主机切换到")),
    # 限流/熔断：只认真正由限流器/熔断器写出的那几条前缀
    ("限流/熔断", re.compile(
        r"^(?:\[RATE\]|RATE |限流器熔断|日配额|主机 .* 1 ?小时内失败)")),
]

_cache = {}
# read_jsonl 的解析缓存：键 = (path, limit, mtime_ns, size)。见该函数 docstring。
_JSONL_CACHE: dict = {}
# read_jsonl 缓存淘汰的**专用锁**（2026-10-30）。不能复用 `_CACHE_LOCK`：
# 那把锁保护 `_cache`，而 `_cached(key, ttl, fn)` 持有它期间会执行 `fn()`，
# `fn()` 里再走 read_jsonl 就是**同一线程重入** → 死锁。
# ⚠️ 2026-11-05（P2）：**模块加载时就建**，不再懒建。原 `if _X is None:
# _X = Lock()` 在 ThreadingHTTPServer 下是竞态：冷启动的并发请求可能
# 各自看到 None、各建一把锁，双检保护当场失效（两线程各持自己的锁）。
_JSONL_LOCK = threading.Lock()


def _cfg_list(cfg, seg):
    """取配置段里结构完整的项（dict），跳过其余。

    ⚠️ webui 刻意不在顶层 import marketlib（它只读文件、不跑行情代码），
    所以这里自带一份，语义与 marketlib._cfg_items 一致。
    必要性：sectors.json 是**用户可编辑**的（其 _说明 明写"改此文件即时生效"），
    段落里漏写花括号或写成裸字符串时，下面 `x.get(...)` 会 AttributeError ——
    实测 2026-09-26：混入一个裸字符串就让 /api/summary 与 build_slots 双双抛异常
    （首页全白）。改错一个字段不该让面板瘫掉。
    """
    lst = cfg.get(seg) if isinstance(cfg, dict) else None
    if not isinstance(lst, list):
        return []
    return [x for x in lst if isinstance(x, dict)]


def _cfg_id(item, *keys):
    """取标的标识（code/key 任一）。**非字符串一律当没有**，返回 None。

    ⚠️ 与 `marketlib._cfg_id` 逐字同款（webui 刻意不在顶层 import marketlib）。
    为什么必须有：`_cfg_list` 只过滤「元素不是 dict」，**不校验值** ——
    于是 `sectors.json` 里 `{"code": ["BAD"]}` / `{"code": {"a":1}}` 这类
    「形状合法的 JSON、类型错的值」会一路带下去：

      · `webui.py` 建索引时拿它当 **dict 的键** → `TypeError: cannot use
        'list' as a dict key`（unhashable）→ `/api/slots` 500；
      · `M._sid(['BAD'])` **不抛异常**，而是拼出 `"90.['BAD']"` —— 一个
        永远匹配不上任何事件的键（清单里凭空多一行 `not_yet`，排查时
        会当成「真漏推」）。

    `[实测]` 2026-10-29：`sectors.json` 的 `watch[0].code = ["BAD"]` →
    `build_slots` 抛 TypeError；`deadline_dates: [[]]` → `set()` 同型。
    而 `sectors.json` 是**用户可编辑的**（`_说明` 明写"改此文件即时生效"）。
    """
    for _k in keys:
        _v = item.get(_k)
        if isinstance(_v, str) and _v.strip():
            return _v.strip()
    return None


def _num_or_none(v):
    """有限实数才返回，否则 None。

    ⚠️ 与 `marketlib.isnum` **同判据**（webui 刻意不顶层 import marketlib）：
    排除 `bool`（是 int 子类，`true.toFixed` 会抛 TypeError）与
    `nan`/`inf`（进响应体后浏览器 `JSON.parse` 抛 `Unexpected token 'N'`）。
    两处都会让前端 `Promise.all` 整页白 —— 面板是用来发现故障的，
    不该被一条脏数据打白。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


# 同 `_JSONL_LOCK`：模块加载时就建，避免"懒建双检"在并发首用下各建一把锁。
_CACHE_LOCK = threading.Lock()


def _cached(key, ttl, fn):
    """带 TTL 的小缓存。**加锁双检**（2026-10-29 修，P3）。

    ⚠️ 原先无锁的 check-then-act：`ThreadingHTTPServer` 每请求一线程，
    冷缓存下并发的两路请求（面板 `Promise.all` 同时打 `/api/summary` +
    `/api/launchd`）会**各跑一遍** `fn()` —— 而 `launchd_info` 的 fn 是
    `subprocess.run(["launchctl", ...], timeout=10)`。`[实测]` 2 个并发
    冷请求 → 8 次 `launchctl` 调用；6 个并发 → 24 次，最坏可让请求线程
    阻塞 40s。锁只覆盖"检查+执行"，命中路径不进锁。
    """
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with _CACHE_LOCK:
        # 双检：等锁期间别人可能已经填好了
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        _cache[key] = (time.time(), val)
        return val


# ---------------- 数据读取 ----------------

def _jsonable(v):
    """把非有限浮点（nan/inf）递归换成 None，供出站 `json.dumps` 使用。

    ⚠️ 为什么在出口做（2026-11-01 修，P2）：Python 的 `json.loads` **接受**
    `NaN`/`Infinity` 返回 float，而 `json.dumps(..., allow_nan=False)` 会
    **拒绝**它们 —— "读得进、写不出"。一条脏数据让端点 500、前端
    `Promise.all` 整页白，直到坏行被截尾（可能数天）。
    `[实测]` 修复前：runs 行 `dur_ms=NaN` → `/api/runs` 500；
    pushes 行 `resp=Infinity` → `/api/pushes` 500；
    sectors thresholds 写 NaN → `/api/summary` 500。

    为什么不在 `read_json/read_jsonl` 入口拒：入口**不知道**哪些字段有
    逐字段守卫。`/api/boards` 的守卫本意是"坏行剔掉、好行留下"，
    入口一律拒会让整份文件不可读、好行一起丢（实测自检当场报红）。
    出口有一个天然优势 —— 它是所有端点共用的唯一收口，且
    "把不可序列化的值降级成 None"与逐字段守卫**同语义**（都是"当没有"），
    不会互相打架。

    只递归 list/dict；其它类型原样（`json.dumps` 自己会拒绝不可序列化的
    对象，那是另一类缺陷，不该在这里被静默吞掉）。
    """
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, str):
        # ⚠️ 2026-11-05（P2）：孤立代理码点（Cs，半截 UTF-16 转义等）会让
        # `json.dumps(...).encode("utf-8")` 抛 UnicodeEncodeError —— 与 nan
        # 同属"读得进、写不出"：一个脏字段让整个端点 500、整页白。
        # 替换成 U+FFFD 与 marketlib.safe() 的拒收口径一致（那边拒整条，
        # 这边是展示层，降级不影响合规判定）。
        if any(0xD800 <= ord(_c) <= 0xDFFF for _c in v):
            return "".join("�" if 0xD800 <= ord(_c) <= 0xDFFF else _c
                           for _c in v)
        return v
    if isinstance(v, dict):
        return {k: _jsonable(_x) for k, _x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(_x) for _x in v]
    return v


def read_jsonl(path, limit=None):
    """读 JSONL。坏行跳过，不抛。

    ⚠️ `errors="replace"` 是必需的，与 runlog.read 同因：半截多字节字符
    （截尾被 kill / 磁盘写满）会让默认的严格解码抛 UnicodeDecodeError，
    被下面 `except Exception: return []` 吞成**空列表** —— 一个坏字节就
    让整个流水表格变空白，而不是只跳过那一行。

    ⚠️⚠️ **结果按 (mtime_ns, size) 缓存**（2026-10-27 加）。
    面板一次刷新会打 `/api/runs` + `/api/slots` + `/api/pushes`，
    其中 `runs.jsonl`（实测 30545 行 / 3.6MB）被**全量 json.loads 两遍**、
    `pushes.jsonl` 两遍 —— 每行一次 `json.loads`，单次刷新近 6 万次解析。
    随流水增长（约 900 行/天）线性放大。
    缓存判据用 `(st_mtime_ns, st_size)` 而不是 TTL：流水是 append-only，
    文件一变 mtime/size 必变，所以**不存在读到陈旧数据**的可能（TTL
    缓存才会有这个风险）。文件被截尾（runlog 4MB 上限）时 size 变小，
    同样会失效重读。

    ⚠️ **返回值必须当只读**：命中的是同一份 list 对象，调用方**不得**
    原地 append/修改元素（现有 6 个调用点全是 `[... for r in read_jsonl(...)]`
    形式的只读用法）。要改就先 `list(...)` 复制一层。
    """
    try:
        _st = os.stat(path)
        _key = (path, limit, _st.st_mtime_ns, _st.st_size)
    except Exception:
        _key = None
    if _key is not None:
        _hit = _JSONL_CACHE.get(_key)
        if _hit is not None:
            return _hit
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    # ⚠️ 只收 dict（2026-10-29 修，P3）：`json.loads("5")` / `"[]"`
                    # / `'"x"'` 都是**合法 JSON 但不是对象**，append 进 out 后
                    # 下游一律 `r.get(...)` → `AttributeError` → 五个端点 500。
                    # docstring 承诺"坏行跳过，不抛"，而原先只 except 解析异常、
                    # 不校验类型 —— 承诺与实际差一截。
                    # 判据与 [市场快照]/[板块列表] 的 `isinstance(list)` 同一口径：
                    # **形状不符就当这一行不存在**。
                    _obj = json.loads(line)
                    if isinstance(_obj, dict):
                        out.append(_obj)
                except Exception:
                    continue
    except Exception:
        return []
    out = out[-limit:] if limit else out
    if _key is not None:
        # ⚠️ **按 path 分组**，每个 path 只留最近 2 代（2026-10-29 修，P2）。
        # 原实现是「攒够 17 个键就整体 clear()」，而键里含 `(mtime_ns, size)`
        # —— 流水是 append-only，**每追加一行就是一个新键**。于是操作员开着
        # 面板刷新 16 次（跨约 80 分钟）就能同时存活 17 份完整解析结果：
        # `[实测]` 31538 行的 runs.jsonl 一份约 29MB RSS → 峰值约 460MB。
        # 注释当时写的是「只保留最近几个键（同一文件不同 limit 各一份 +
        # 上一代）」，实现差一个量级。
        # ⚠️⚠️ 淘汰块必须在**同一把锁**里做（2026-10-30 修，P2 竞态）。
        # 本函数是 `ThreadingHTTPServer` 每请求一线程调用的，而淘汰块
        # **迭代** `_JSONL_CACHE` 并逐键 `pop` —— 与并发线程的赋值/迭代
        # 交错时抛 `RuntimeError: dictionary changed size during iteration`。
        # 该块不在任何 try 内，异常直达 `do_GET` → 500 → 前端 `Promise.all`
        # 一挂整页白。`[实测]` 沙箱用真实 `webui.read_jsonl` 复现过
        # （6 线程 × 30 轮命中 1 次 RuntimeError）。10-28 备份版是
        # `if len(_JSONL_CACHE) > 16: clear()`（不迭代，不会抛）——
        # 这个竞态是 10-29 改成按 path 分组时**我自己引入的**。
        # 复用的 `_cached()` 那把锁不可用：它同时保护 `_cache`，
        # 而 `fn()` 可能再进 `read_jsonl` → 递归取锁即死锁。
        # 故本函数自带一把专用锁，只覆盖「读-淘汰-写」这三步。
        with _JSONL_LOCK:
            # 命中要复查：等锁期间别的线程可能已经填好同一键
            _hit2 = _JSONL_CACHE.get(_key)
            if _hit2 is not None:
                return _hit2
            _grp = [k for k in _JSONL_CACHE if k[0] == path]
            if len(_grp) > 1:
                # 按 mtime_ns 排序，保留最新的一个（本次要写的覆盖旧的后
                # 最多剩 2 份：当前 + 上一代，供并发请求读到一致快照）。
                _grp.sort(key=lambda k: (k[2], k[3]))
                for _old in _grp[:-1]:
                    _JSONL_CACHE.pop(_old, None)
            _JSONL_CACHE[_key] = out
    return out


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_json_obj(path):
    """读 JSON 并**保证返回 dict**（顶层不是 dict 时返回空 dict）。

    ⚠️ 2026-10-27 修（P1）：原先各调用点写的是 `read_json(X, {}) or {}` ——
    那个 `or {}` 只兜「读不到/解析失败」，**兜不住「读到了但不是 dict」**：
    文件被手写坏成 `[1]` 时 `read_json` 原样返回 list（非空即真值），
    随后 `st.get("date")` 抛 `AttributeError`，`/api/slots` 500，而前端是
    `Promise.all` 七路并发 —— 任一路失败整页只剩「载入失败」。
    `[实测]` state.json/pending.json/sectors.json 顶层写成 `[1]` 时，
    build_slots / collect_health / config_summary 全部抛 AttributeError。
    而这三个文件都是「改了即时生效、无需重启」的运维可写文件。
    本函数与同文件对 `pending.events` 的 `isinstance(list)` 守卫同一口径：
    **形状不符就当没有**，让面板降级显示而不是整页白。
    """
    d = read_json(path, None)
    return d if isinstance(d, dict) else {}


class LaunchdJob(TypedDict, total=False):
    """一个 launchd job 的解析结果。runs/last_exit 来自 launchctl 输出里
    的 `runs = N`，可能解析失败故允许 None；state 是字符串。
    标注它是因为下面 info["runs"] = int(...) 这类赋值 —— 裸 dict 会让
    int 赋给 object 被报 reportArgumentType（实测 2 条）。
    """
    label: str
    loaded: bool
    runs: int | None
    last_exit: int | None
    state: str | None
    sched: str | None
    sched_warn: bool
    designed_rc: list[int]


class LogRow(TypedDict):
    """watch.log 的一行。"""
    md: tuple[int, int]
    hm: str
    d: str
    tag: str
    msg: str
    extra: list[str]
    y: NotRequired[int]
    ts: NotRequired[str]


class Fold(TypedDict, total=False):
    """折叠组：连续同类行合并成一条。row/rows 是代表性行与全部行。

    rows_total / rows_truncated 由 api() 在响应时补上（前端展示用）——
    必须列在这里，否则 `_f["rows_total"] = ...` 会被报
    "Could not assign item in TypedDict"（实测 3 条）。
    """
    label: str
    n: int
    first: str
    last: str
    row: LogRow
    rows: list[LogRow]
    rows_total: int
    rows_truncated: int


class ParseResult(TypedDict):
    """parse_log 的返回：两个 list + 一个 stats dict。

    ⚠️ lines 是 Required 而不是 NotRequired —— 它是**构造时**必然存在的键。
    api() 出于响应体积考虑**不把它放进 HTTP 响应**，但那是**另建一个
    LogResponse**（只取 lines_count/folds/stats），并不修改本 dict。
    这里曾写"api() 会 pop 掉 lines"，与实现不符（那个方案试过，因
    `pop` 带默认值会把返回类型拓宽成 object|list[Any] 而放弃）。
    """
    lines: list[LogRow]
    folds: list[Fold]
    stats: dict[str, int | dict[str, int] | list[str]]


class LogResponse(TypedDict):
    """/api/log 的响应：ParseResult 去掉 lines、加上 lines_count。

    单独一个类型是因为 `dict(d)` 会被推成 dict[str, object]，
    随后 _resp["lines_count"] = int 报参数类型不符（实测 1 条）。
    显式列出后每个键的类型都确定。
    """
    lines_count: int
    folds: list[Fold]
    stats: dict[str, int | dict[str, int] | list[str]]
    folds_total: int          # 折叠组总数（folds 只回最近 600 组）
    folds_truncated: int      # 未传输的组数（>0 时页面必须写明）


def parse_log():
    """解析 watch.log（+ .1），返回 {lines, folds, stats}。

    年份回退（**从末尾回溯**，两级）：
      1) 以 watch.log 的 mtime 所在年为锚，**从末行开始**向前回溯
         （末行最接近 mtime，用它定锚最稳）
      2) 相邻两行 (month, day) 由后向前比：只有**天数差 > 30** 才
         year -= 1 —— 无脑 `prev > cur → year-1` 会把 12-31 本身也判成
         上一年（已实测），这是跨年才暴露的坑

    ⚠️ 这里原写"三级回退"，第 3 条声称用 runs.jsonl 里自带完整年份的
    `t` 做交叉校验 —— 但函数体里**从未读取 runs.jsonl**（实测：body 中
    不出现 RUNS / read_jsonl）。该步只是设计意图，未实现；保留这句会
    让后续维护者以为已有兜底而放松警惕。若日后要加，做法是：取
    runs.jsonl 里与末行同日同时刻的记录，用它的 t 校准 year。
    """
    files = [LOG1, LOG] if os.path.exists(LOG1) else [LOG]
    rows, cont, tags = [], 0, {}
    for path in files:
        if not os.path.exists(path):
            continue
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for raw in fh:
                line = raw.rstrip("\n")
                m = LOG_RE.match(line)
                if not m:
                    # 无时间戳（traceback 续行 / 空行）→ 归属上一条
                    cont += 1
                    if rows:
                        rows[-1]["extra"].append(line)
                    continue
                mm, dd, hh, mi, ss, tag, msg = m.groups()
                tag = tag or ""
                tags[tag] = tags.get(tag, 0) + 1
                rows.append({
                    "md": (int(mm), int(dd)),
                    "hm": f"{hh}:{mi}:{ss}",
                    "d": f"{mm}-{dd}",
                    "tag": tag, "msg": msg, "extra": [],
                })

    # ---------- 定年：**从末尾回溯**，不要从头递增 ----------
    # ⚠️ 原实现以 watch.log 的 mtime 所在年为起点、从头递增，隐含假设
    # 「日志首行就在 mtime 那年」。该假设在**跨年**时失效：日志可以始于
    # 上一年（跨年期间未轮转，或轮转周期超过一年）。实测：
    #   mtime = 2026-01-05，日志首行 12-20（实为 2025-12-20）
    #   -> 解析成 2026-12-20（错一年），其后全部顺移，末行变 2027-01-05。
    # 而且不会报错、不会告警，只是面板上所有历史行的日期都错一年。
    #
    # 正确做法：末尾那行最接近 mtime，用它定锚，再**向前**回溯。
    # 前向跨年判据与原先一致（同日历年下 a-b > 30 天），只是方向反过来。
    _mt = datetime.datetime.fromtimestamp(
        os.path.getmtime(LOG) if os.path.exists(LOG) else time.time())
    year = _mt.year
    if rows:
        _mm, _dd = rows[-1]["md"]
        try:
            _a = datetime.date(year, _mm, _dd)
            _b = datetime.date(_mt.year, _mt.month, _mt.day)
            # 末行"晚于"mtime 超过 30 天 -> 它是上一年的（日志陈旧未更新）
            if (_a - _b).days > 30:
                year -= 1
        except Exception:
            pass
        rows[-1]["y"] = year
        for _i in range(len(rows) - 1, 0, -1):
            _prev, _cur = rows[_i - 1]["md"], rows[_i]["md"]
            if _prev > _cur:
                try:
                    _a = datetime.date(year, _prev[0], _prev[1])
                    _b = datetime.date(year, _cur[0], _cur[1])
                    if (_a - _b).days > 30:
                        year -= 1        # 这两个日期之间跨了一次年
                except Exception:
                    pass
            rows[_i - 1]["y"] = year
    for _r in rows:
        _y = _r.pop("y", _mt.year)
        _r["ts"] = f"{_y:04d}-{_r['md'][0]:02d}-{_r['md'][1]:02d} {_r['hm']}"
    folds = _fold(rows)
    # 标注成 ParseResult 而不是裸 dict：本行构造出的键和值类型都确定
    # （lines/folds 是 list、stats 是 dict），写成裸 dict 会让 api() 里
    # `d["lines"]` / `d["folds"][-600:]` 被报成参数类型不符
    # （实测 3 条 reportArgumentType）。
    _res: ParseResult = {
        "lines": rows,
        "folds": folds,
        "stats": {
            "total_lines": len(rows) + cont,
            "parsed": len(rows),
            "continuation": cont,
            "tags": tags,
            "files": [os.path.basename(f) for f in files],
            "folded_away": sum(f["n"] - 1 for f in folds if f["n"] > 1),
        },
    }
    return _res


def _fold(rows):
    """把连续重复的噪音行合并。**只影响展示，不丢数据**。"""
    out = []
    for r in rows:
        label = None
        for name, pat in FOLD_PATTERNS:
            if pat.search(r["msg"]):
                label = name
                break
        if label and out and out[-1]["label"] == label:
            out[-1]["n"] += 1
            out[-1]["last"] = r["ts"]
            out[-1]["rows"].append(r)
        else:
            out.append({"label": label, "n": 1, "first": r["ts"], "last": r["ts"],
                        "row": r, "rows": [r]})
    return out


def launchd_info():
    """读 launchctl print 的权威字段。实测 0.04s，加 15s 缓存稳妥。"""
    def _get():
        out = {}
        for label in JOBS:
            info: LaunchdJob = {"label": label, "loaded": False, "runs": None,
                                "last_exit": None, "state": None, "sched": None,
                                "sched_warn": False,
                                "designed_rc": sorted(_DESIGNED_RC.get(label, ()))}
            try:
                r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    info["loaded"] = True
                    for line in r.stdout.splitlines():
                        line = line.strip()
                        if line.startswith("runs = "):
                            info["runs"] = int(line.split("=")[1].strip())
                        elif line.startswith("last exit code = "):
                            info["last_exit"] = int(line.split("=")[1].strip())
                        elif line.startswith("state = "):
                            info["state"] = line.split("=")[1].strip()
            except Exception:
                pass
            # 调度方式：从 plist 读。**这是本面板最该显示却一直没显示的一项** ——
            # 2026-09-24 实测本机 StartCalendarInterval 完全失效（launchd 注册了
            # calendarinterval 事件流、watching=1、descriptor 正确，但事件从不
            # 投递，runs 永远 0）。任务"已加载"却是死的，而面板上
            # 「runs=N / exit 0」看起来一切正常 —— 光看这两个字段发现不了。
            try:
                import plistlib
                _f = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
                if os.path.exists(_f):
                    with open(_f, "rb") as _fh:
                        _pl = plistlib.load(_fh)
                    if "StartCalendarInterval" in _pl:
                        info["sched"] = "calendar"
                        # calendar 任务若 runs=0，要么还没到点，要么就是没投递
                        if info["runs"] == 0:
                            info["sched_warn"] = True
                    elif _pl.get("StartInterval"):
                        info["sched"] = f"interval {_pl['StartInterval']}s"
                    elif _pl.get("WatchPaths"):
                        info["sched"] = "watch"
                    else:
                        info["sched"] = "keepalive"
            except Exception:
                pass
            out[label] = info
        return out
    return _cached("launchd", 15, _get)


def _collect_fresh_limit():
    """盘中新鲜度阈值（秒）—— 从 collect.py 静态读取，与 selfcheck 同源。

    ⚠️ 原先这里和 selfcheck 各写死 600，而 600 是按 collect 的 **120s 节拍**
    定的（=5 轮余量）。2026-09-30 节拍放宽到 300s 后 600 只剩 2 轮，一次被
    跳过的慢轮就会把面板标成「停摆」。改为统一读 collect.FRESH_LIMIT_IN_SESSION
    （= 3× 节拍），消除这份手工副本 —— 与本文件读 analyze 常量同一模式。
    读不到时退回 900（当前节拍下的已知值）。
    """
    import ast as _ast
    fp = os.path.join(MAIN_ROOT, "collect.py")
    try:
        with open(fp, encoding="utf-8") as _fh:
            _tree = _ast.parse(_fh.read())
        # ⚠️ 先收集模块级 int 字面量常量（SCHED_INTERVAL 等）。原实现只做
        # `literal_eval(_n.value)`，而 FRESH_LIMIT_IN_SESSION = SCHED_INTERVAL*3
        # 是 **BinOp** -> literal_eval 必抛 -> 恒退回硬编码 900（"消除手工副本"
        # 是死代码，与 docstring 声称的相反）。2026-10-08 审查实测。
        _consts: dict = {}
        for _n in _tree.body:
            if isinstance(_n, _ast.Assign) and isinstance(_n.value, _ast.Constant) \
                    and isinstance(_n.value.value, int):
                for _t in _n.targets:
                    if isinstance(_t, _ast.Name):
                        _consts[_t.id] = _n.value.value

        def _ev(node):
            if isinstance(node, _ast.Constant) and isinstance(node.value, int):
                return node.value
            if isinstance(node, _ast.Name) and node.id in _consts:
                return _consts[node.id]
            if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Mult):
                return _ev(node.left) * _ev(node.right)
            raise ValueError("unsupported expr")

        for _n in _tree.body:
            if isinstance(_n, _ast.Assign):
                for _t in _n.targets:
                    if isinstance(_t, _ast.Name) and _t.id == "FRESH_LIMIT_IN_SESSION":
                        return int(_ev(_n.value))
    except Exception:
        pass
    return 900


def collect_health():
    """采集心跳新鲜度 —— 阈值与 selfcheck.py 同源（见 _collect_fresh_limit）。"""
    p = read_json_obj(PENDING)
    t = p.get("time")
    age = None
    if t:
        try:
            dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M")
            age = (datetime.datetime.now() - dt).total_seconds()
        except Exception:
            age = None
    now = datetime.datetime.now()
    hm = now.hour * 60 + now.minute
    # ⚠️ 交易时段单一真值（2026-10-08 审查）：原先硬编码 9:25/11:35/13:00/15:10，
    # 改 TH["session"] 时面板不跟随。时段与午休都从 marketlib 读；
    # 午休豁免沿用 marketlib._LUNCH（11:30~13:00），午休后 5 分钟缓冲保留。
    try:
        import marketlib as _M
        _s0, _s1 = _M.TH["session"]
        _l0, _l1 = _M._LUNCH
    except Exception:
        _s0, _s1, _l0, _l1 = 9 * 60 + 25, 15 * 60 + 5, 11 * 60 + 30, 13 * 60
    # ⚠⚠ 2026-11-10（Codex P3）：先判**交易日**，再判时段。
    # 原来只看墙钟：周六 10:00 也会得到 `in_sess=True`，
    # 配上周五收盘快照 → `stale=True` → 面板显示「采集疑似停摆」。
    # 实测：2026-11-07（周六）10:00 得到 in_session=True、stale=True、limit=900。
    # 非交易日本来就不采集 —— 把它当故障是对环境的误读。
    try:
        import marketlib as _M2
        _is_td = bool(_M2.is_trading_day(now.date(), warn=False))
    except Exception:
        _is_td = now.weekday() < 5
    in_sess = _is_td and (_s0 <= hm <= _s1 + 5) and not (_l0 + 5 <= hm < _l1)
    limit = _collect_fresh_limit() if in_sess else 3600
    _hist = p.get("hist")
    _evs = p.get("events")
    _nws = p.get("news")
    return {
        "last": t, "age_sec": age, "limit_sec": limit,
        "stale": (age is not None and age > limit),
        # ⚠️ hist 形状守卫（2026-10-08 审查）：pending.json 是外部文件，
        # hist 写成 int 时 len() 抛 TypeError -> /api/summary 500 -> 首页全白。
        # 同文件 1179 行读 events 有 isinstance 守卫，hist 没有。
        "hist": len(_hist) if isinstance(_hist, list) else 0,
        # ⚠️ events/news 同样必须形状守卫（2026-10-11 审查）：原先这两行是
        # 裸 `len(p.get("events", []))` —— 默认值只兜"键不存在"，兜不住
        # "键存在但值是 int"。实测 `{"events":5}` -> TypeError ->
        # `/api/summary` 500 -> 首页全白。与上面 hist 是同一类缺陷，
        # 同一函数内两套标准。
        "events": len(_evs) if isinstance(_evs, list) else 0,
        "news": len(_nws) if isinstance(_nws, list) else 0,
        "in_session": in_sess,
    }


def is_trading_day(d=None):
    d = d or datetime.date.today()
    try:
        import marketlib as M
        # warn=False：面板是只读的，不能因为查一下就写 holiday_warn.json /
        # 往 watch.log 插日志（2026-10-08 审查的"只读面板未来写路径"）。
        return M.is_trading_day(d, warn=False)
    except Exception:
        return d.weekday() < 5


def holidays():
    try:
        import marketlib as M
        return sorted(getattr(M, "HOLIDAYS", set()))
    except Exception:
        return []


def config_summary():
    """只暴露安全字段。**绝不输出 webhook key**。

    ⚠️ 2026-10-21：`poll_seconds` / `market_open` / `market_close` 三个字段
    已从本响应里**移除** —— 它们来自 config.json，而该文件的 `_note` 自己
    写明这 4 个字段「当前不生效」（调度间隔在 plist 的 StartInterval、
    交易时段在 marketlib.TH['session']）。面板/API 把它们当可调配置展示，
    会让读者改一个**毫无效果**的值然后以为改了行为。真值请查：
      · 调度间隔 → launchctl print gui/$UID/com.mison.marketwatch
      · 交易时段 → marketlib.TH
    """
    c = read_json_obj(CONFIG)
    s = read_json_obj(SECTORS)
    return {
        "webhook_configured": bool(c.get("webhook")),
        # 只取结构完整的项：sectors.json 用户可编辑，混进裸字符串/null 时
        # x.get() 会 AttributeError，整个 /api/summary 变 500（面板首页全白）。
        "watch": [{"code": x.get("code"), "name": x.get("name")}
                  for x in _cfg_list(s, "watch")],
        "push": [{"code": x.get("code"), "name": x.get("name")}
                 for x in _cfg_list(s, "push")],
        "thresholds": s.get("thresholds", {}) if isinstance(
            s.get("thresholds"), dict) else {},
        # ⚠️ 顶层非 list 时回空表（2026-11-01 修）：原样透传 `"2027-01-05"`
        # 会让前端拿到一个字符串、`.length` 与 `.map` 的语义都不是"几个日期"。
        "deadline_dates": (s.get("deadline_dates")
                           if isinstance(s.get("deadline_dates"), list) else []),
        "holidays": holidays(),
    }


def rate_info():
    r = read_json_obj(RATE)
    try:
        import marketlib as M
        cap = M.RL.daily_cap
    except Exception:
        cap = 2000
    return {"date": r.get("date"), "count": r.get("count"), "cap": cap}


# ---------------- 时段完成表 ----------------

# 从 watch.log 的 PUSH OK 行提取结论。
# 埋点（pushes.jsonl）上线前的历史推送，正文只存在于日志里 ——
# 而且是 repr 截断到 50 字。用 ast.literal_eval 把 repr 还原成真串，
# 取首行作为"结论"。这样今天及以前的推送也能在面板上看到结论，
# 而不是一片空白。
_PUSH_LOG_RE = re.compile(
    r"^\[(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\] "
    r"PUSH (OK|FAIL) :: '(.*?)'(?:\s|$)")

def _push_conclusions_from_log():
    """返回 {(MM-DD, HM): {"conclusion":..., "ok":..., "at":...}}。

    解析失败一律跳过，绝不让日志格式的小变动搞崩整个面板。
    """
    out = {}
    files = [LOG1, LOG] if os.path.exists(LOG1) else [LOG]
    for path in files:
        if not os.path.exists(path):
            continue
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for line in fh:
                if "PUSH " not in line:
                    continue
                m = _PUSH_LOG_RE.match(line)
                if not m:
                    continue
                mm, dd, hh, mi, ss, verdict, raw = m.groups()
                # 日志里存的是 repr 文本（analyze.push 用 {t[:50]!r} 写入），
                # 所以换行是**字面的两个字符 \ 和 n**，不是真换行。
                # 用 literal_eval 还原转义序列（它同时处理 \n / \' / \uXXXX）。
                try:
                    text = ast.literal_eval("'" + raw + "'")
                except Exception:
                    # literal_eval 可能因截断的半截转义失败 —— 退化为手工替换
                    text = (raw.replace("\\n", "\n").replace("\\t", "\t")
                               .replace("\\'", "'").replace('\\"', '"'))
                first = ""
                for ln in text.split("\n"):
                    ln = ln.strip().lstrip(">").strip().strip("*").strip()
                    if ln:
                        first = ln
                        break
                key = (f"{mm}-{dd}", f"{hh}:{mi}")
                out[key] = {"conclusion": first[:80], "ok": verdict == "OK",
                            "at": f"{hh}:{mi}:{ss}"}
    return out

def _slot_conclusion(slot_key, pushes, log_map, date):
    """取某个时段的结论。优先级：pushes.jsonl 全文 > 日志首行 > 无。"""
    # 1) pushes.jsonl（埋点后的权威来源，有完整正文）
    # ⚠️ 只认 **ok 的** 记录。原先不判 ok，于是被 safe() 闸门拦下的推送
    # （analyze.py 记 kind="blocked"/ok=False，**也带 text**）照样返回结论，
    # 而调用方 `if done or concl` 把非空结论一律当 done —— 结果是
    # "被安全闸门拦下来"在面板上显示为绿色 done，比不显示更危险。
    # 未成功的那些仍会出现在 pushes.jsonl 全量列表里（不丢痕）。
    for p in pushes:
        if (p.get("slot") or "") == slot_key and p.get("text") and p.get("ok"):
            for ln in str(p["text"]).split("\n"):
                ln = ln.strip().lstrip(">").strip().strip("*").strip()
                if ln:
                    return {"conclusion": ln[:80], "full": p["text"],
                            "ok": bool(p.get("ok")), "src": "pushlog"}
    # 2) 日志（**埋点之前**的历史推送）
    #
    # ⚠️ 埋点覆盖该日时必须**整段跳过** —— 否则会认领到别的类型的正文。
    # 日志行里**没有** kind/slot 信息（analyze.push 只写 `PUSH OK :: {text!r}`），
    # 于是这里只能按时间就近认领，把落在容差窗 [-5, TTL+5] 内的**任何**
    # 推送都算作该槽位的结论 —— 包括事件类（slot=""）和 urgent。
    # 实测（2026-09-28，生产真实数据）：10:00 槽位从未认领（fired 里没有
    #   hourly_10:00、pushes.jsonl 里没有 slot='10:00'），但 09:58:02 有一条
    #   **event** 推送落在窗口内，被认领成 10:00 的结论
    #   （src=watchlog, at=09:58:02）-> 调用方 `if done or concl` 把非空
    #   结论当 done -> **面板报"已完成"，而该槽位一条都没推过**。
    # 实测对照：把 fired 里的 hourly_10:00 摘掉后重跑，status 仍是 done，
    #   正文来自 09:58 那条 event —— 这是与"fired 被污染"**独立**的第二个
    #   假 done 通道。
    #
    # 判据用"当日是否有推送记录"，而不是"日志是否比 pushes 新"：
    # 实测（2026-09-28）两者的 PUSH OK 条数逐日对齐 ——
    #   09-22 日志 4 / pushes 0（埋点前）
    #   09-23 日志 8 / pushes 0（埋点前）
    #   09-24 日志 14 / pushes 14（埋点后，pushes 完全覆盖）
    #   09-28 日志 12 / pushes 12（同上）
    # 因为 runlog.push() 写在 subprocess.run **之前**，它是日志的**超集**
    # —— 只要当日有记录，日志能提供的结论必然已由第 1 步给出。
    if any(str(_p.get("t", "")).startswith(date) for _p in pushes):
        return None
    md = date[5:]                    # "2026-09-23" -> "09-23"
    cands = [(k, v) for k, v in log_map.items()
             if k[0] == md and v.get("ok")]
    if cands:
        # 时段 key 形如 "11:30"/"summary"/"14:30"；用计划时刻最近的匹配
        target = None
        for k, _role, planned in SLOTS:
            if k == slot_key:
                target = _slot_min(planned)
                if target is None:
                    target = 9999
                break
        best = None
        for (_dd, hmt), v in cands:
            try:
                h, mi = hmt.split(":")
                cur = int(h) * 60 + int(mi)
            except Exception:
                continue
            if target is None:
                continue
            # 容差收紧到 [-5, +SLOT_TTL+5] 分钟。
            # 原先写 [0, 60] 太宽：10:24 那条同时落进 09:25 和 10:00 两个
            # 时段的窗口，于是两个时段显示了**同一条结论**。
            # 现在按该时段自己的 TTL 划界（外加 5 分钟生成耗时余量）。
            ttl = _SLOT_TTL.get(slot_key, 20)
            # 下界必须覆盖**提前开放**的那几分钟：09:25 槽位的窗口是
            # [09:17, 09:33)，落在 09:20 的推送距计划时刻是 -5 分钟以外。
            # 原先写死 -5，提前开放后那些轮次认领不到，会被判成
            # "当日无该时段推送记录"（假告警）。
            _early = _SLOT_OPEN_EARLY.get(slot_key, 0)
            if -(5 + _early) <= cur - target <= ttl + 5:
                # 标题里写明本时段时刻的，优先认领
                if slot_key[:5] in str(v.get("conclusion", "")):
                    return {"conclusion": v["conclusion"], "ok": True,
                            "src": "watchlog", "at": v.get("at")}
                if best is None or abs(cur - target) < abs(best[0] - target):
                    best = (cur, v)
        if best:
            return {"conclusion": best[1]["conclusion"], "ok": True,
                    "src": "watchlog", "at": best[1].get("at")}
    return None

def cfg_deadline_dates():
    """sectors.json 的 deadline_dates（计划窗口最后一日）。读不到返回 []。

    ⚠️ 必须过滤**非字符串**（2026-10-29 修，P1）：调用方拿它直接建 `set()`，
    而 `set([[]])` / `set([{}])` 抛 `TypeError: unhashable type` ——
    `/api/slots` 500、整页白。`sectors.json` 是用户可编辑文件，
    "值类型写错"是最常见的编辑事故形态。
    """
    try:
        # ⚠️ 顶层必须是 **list**（2026-11-01 修，P4）：写成裸字符串
        # （`"deadline_dates": "2027-01-05"`）时下面的列表推导会**逐字符**
        # 遍历它，产出 {"2","0","7",…} —— 每个元素都是 str，isinstance
        # 过滤拦不住。后果是 deadline 槽静默显示 n/a（`date in _dl_dates`
        # 恒 False），面板上看不出配置写错了。
        # `[实测]` 沙箱 `{"deadline_dates": ["2","0","2"]}` 与
        # `"2027-01-05"` 两种形态都产出无意义集合。
        _dd = read_json_obj(SECTORS).get("deadline_dates")
        if not isinstance(_dd, list):
            return []
        return [x for x in _dd if isinstance(x, str)]
    except Exception:
        return []


def _fired_labels(fired, cfg):
    """把 state.json 的内部键转成人类可读的标签。

    面板 chips 原先直接渲染 fired 的原始键，读者看到 `watch_90.BK0465`、
    `hourly_09:25`、`idx_1.000001_2026-09-24` 这类内部标识 —— 无法理解。
    这里按三类映射：
      · hourly_<时段>      -> 「整点播报 09:25」/ 语义时段 -> role 名
      · watch_<secid>      -> 该板块的中文名（查 sectors.json）
      · idx_<secid>_<日期> -> 「指数异动 上证指数」（alert 紧急通道）
      · sec_<code>_<日期>  -> 「板块资金 半导体」
      · mkt_<日期>         -> 「大盘资金」
      · 其余（idx_weak 等）-> 事件区已有对应行，不必重复展示
    返回 [{label, raw}]，按标签排序。
    """
    roles = {}
    for k, role, _p in SLOTS:
        roles[k] = role
    # ⚠️ 用 `_cfg_id` 取值、丢掉取不到名字的（2026-10-29 修，P1）：
    # 原先直接 `x.get("code")` 当 dict 的键 —— `code` 被写成数组/对象时
    # 是 unhashable，整个 `/api/slots` 500，而前端 `Promise.all` 七路并发
    # 会让整页只剩「载入失败」。
    names = {_c: x.get("name") for x in _cfg_list(cfg, "watch")
             if (_c := _cfg_id(x, "code"))}
    # 指数 secid -> 名称（alert 的 idx_ 键用的是完整 secid）
    idx_names = {}
    try:
        import marketlib as M
        for sid, (nm, _t) in M.STATIC_SIGMA.items():
            idx_names[sid] = nm
    except Exception:
        pass
    # analyze.detect 的事件键 -> 中文说明（与事件区同一套语义）
    EV_LABELS = {
        "idx_weak": "大盘走弱", "idx_strong": "指数走强",
        "hs300_weak": "沪深300走弱",
        "hstech_up": "恒生科技上破", "hstech_down": "恒生科技下破",
        "hstech_drop": "恒生科技单日大跌", "storage_rev": "存储冲高回落",
        "semi_out": "半导体资金流出", "semi_in": "半导体资金流入",
        "semi_surge": "半导体异动", "gold_up": "黄金上破", "gold_dn": "黄金下破",
    }
    out = []
    for k in sorted(fired):
        raw = k
        label = None
        if k in EV_LABELS:
            label = EV_LABELS[k]
        elif k.startswith("hourly_"):
            t = k[len("hourly_"):]
            label = f"整点播报 {t}"
        elif k in roles:
            label = roles[k]
        elif k.startswith("watch_"):
            code = k[len("watch_"):]
            label = f"板块触发 {names.get(code) or code}"
        elif k.startswith("idx_"):
            # idx_<secid>_<YYYY-MM-DD>
            rest = k[4:]
            sid = rest.rsplit("_", 1)[0] if "_" in rest else rest
            label = f"指数异动 {idx_names.get(sid) or sid}"
        elif k.startswith("sec_"):
            rest = k[4:]
            code = rest.rsplit("_", 1)[0] if "_" in rest else rest
            label = f"板块资金 {names.get(code) or names.get('90.' + code) or code}"
        elif k.startswith("mkt_"):
            label = "大盘资金异动"
        # ⚠️ 认不出的键**必须原样显示**，不能静默丢弃。
        # 原实现是 `if label:` —— 所有映射都不匹配的键直接从 chips 里消失，
        # 而 fired 是"当天该事件已推送过、不会再推"的**去重记录**：
        # 它上面每多一个认不出的键，就说明有一条消息发过却查不到轨迹。
        # 实测（2026-09-27，沙箱 state.json）：
        #   fired = [deadline, hourly_09:25, idx_1.000001_2026-09-24,
        #            watch_99.BK9999, totally_bogus_key, some_new_event_v2]
        #   chips = [deadline, hourly_09:25, idx_..., watch_99.BK9999]
        #   -> totally_bogus_key / some_new_event_v2 **无声消失**
        # 新增事件类型（analyze.detect 加一个键）或写进污染数据时，
        # 面板会显示成"今天什么都没发生"，而事实相反。
        # 用 raw 当标签并标注「未识别」，让读者知道有东西没被归类，
        # 而不是让面板替他做"这条不重要"的判断。
        if not label:
            label = f"未识别 {raw}"
        out.append({"label": label, "raw": raw})
    return out


def _ev_alias(k):
    """把 collect 的事件键映射成事件表认得的键。

    collect.py 与 analyze.detect() 用**两套不同的键**描述同一批异动：
      collect 写入 pending.json 的   ->  analyze/webui 使用的
        fund_90.BK1036              ->  watch_90.BK1036
        idx_1.000001                ->  idx_weak（指数 3σ 异动只有这一行）
        hstech_up/down、storage_rev  ->  同名（这一半本来就一致）
    不映射的后果：**快照通道触发的异动在面板上一条都不显示** ——
    实测（生产 pending.json，09-24）7 条事件里 6 条是 fund_*，
    面板事件表里全无踪影（它们被拿去查 analyze 写的 watch_<code>）。
    而 collect 是**唯一**记录 at/msg 的通道，等于面板丢掉了大部分信息。

    放在模块级而不是 build_slots 内部，是为了让 selfcheck 能**行为级**
    验证它（嵌套函数外部取不到，只能退回源码级字符串比对，
    而字符串比对恰恰抓不到"两套命名没映射"这类问题）。
    """
    if k.startswith("fund_"):
        return "watch_" + k[5:]      # fund_90.BK1036 -> watch_90.BK1036
    # ⚠️ 指数的 `idx_weak`/`idx_strong` 是**同名穿透**，不走这里。
    # 历史上 collect 一律产 `idx_<短码>` 且**双向触发**（abs(f3)>=thr），
    # 而这里把所有 `idx_*` 折叠成 `idx_weak`（标签「大盘走弱」）——
    # 于是恒生指数**上涨** 4.04% 会在面板显示成「大盘走弱/已触发」，
    # 附言却写着 +4.04%，且冒占了 analyze 那条真 idx_weak 的行。
    # collect 现在按方向产出 idx_weak/idx_strong，这里的兜底只处理
    # **历史遗留**的 `idx_<短码>` 键（旧 pending.json 里的），
    # 按 chg 无法在此判断，故归到 idx_weak 并在面板上带出 raw_key 供溯源。
    if k.startswith("idx_") and k not in ("idx_weak", "idx_strong"):
        return "idx_weak"
    return k


class BadDate(Exception):
    """`?date=` **给了值但值非法**。由 do_GET 译成 400。

    ⚠️ 必须与"没给 date"（返回 None，表示不过滤）区分开 —— 见 `_q_date`。
    """


def _q_date(q):
    """`?date=` 查询参数的统一入口。

    返回：校验通过的 ISO 日期字符串；**参数未给**时返回 None（调用方不过滤）。
    非法值**抛 `BadDate`** —— 由 do_GET 译成 400。

    ⚠️ 非法值绝不能返回 None（2026-10-12 复查）：`None` 在调用方就是
    "没传 date、不要过滤"，于是 `?date=xyz` 会返回**全部历史行** ——
    比原先"返回空结果"更糟（空结果至少不冒充别的日期）。实测：
    `api("/api/runs", {"date":["xyz"]})` 返回 800 行（全部保留量）。
    同族的 `build_slots` 走的是"非法即回退今天"（那是**主视图**，
    一个手误不该让面板整页报错），两者**刻意不同**，别照搬。
    """
    _d = (q.get("date") or [None])[0]
    if not _d:
        return None
    try:
        # ⚠️ 往返比对：`fromisoformat` 还接受**基本格式**（`20261012`）与
        # ISO 周日期（`2026-W41-1`），但它们与过滤用的 `startswith("YYYY-MM-DD")`
        # 形状不符 → 静默返回空。只放行**恰好等于**规范形式的输入，
        # 保证"校验通过"就意味着"过滤能匹配上"。
        if datetime.date.fromisoformat(_d).isoformat() != _d:
            raise ValueError(_d)
    except Exception:
        raise BadDate(_d)
    return _d


def build_slots(date=None):
    """核心视图：某个交易日的各时段/各事件完成情况。

    当日：state.json 的 fired[] 是权威。
    历史日：state.json 已被 load_st() 重置（date != today 时全清），
            必须从 runs.jsonl + pushes.jsonl 重建 —— 这正是埋点的意义。
    """
    date = date or datetime.date.today().isoformat()
    # ⚠️ date 来自 `?date=` 查询参数，**必须校验**。原先直接把它当字符串
    # 用（startswith 过滤）本不会出错，但下面有 datetime.date.fromisoformat
    # 解析——畸形输入（"2026-13-45" / "not-a-date" / "../../etc/passwd"）
    # 会抛 ValueError，被 do_GET 的兜底 except 包成 HTTP 500，响应体里
    # 还带着原始异常消息（`ValueError: month must be in 1..12, not 13`）。
    # 实测四种畸形输入全部 500。
    # 面板是本机只读工具，但"任何输入都不该 500"是基本契约 —— 且这个
    # 端点是**主视图**（前端下拉切日期就用它），一个手误的日期就会让
    # 整个面板报错。非法日期一律回退到今天，而不是把异常抛给调用方。
    try:
        datetime.date.fromisoformat(date)
    except Exception:
        date = datetime.date.today().isoformat()
    today = datetime.date.today().isoformat()

    st = read_json_obj(STATE)
    if st.get("date") != date:
        st = {}                       # 历史日：state.json 无效

    # 同 pushes：标注消除 Unknown 扩散（实测本处 -6 条）。
    # ⚠⚠ 2026-11-10（Codex P2）：这个列表被当成「analyze 轮次」展示，
    # 但 `runs.jsonl` 里**同时有 collect 的记录**，而且 analyze 自己
    # 有 start/end **两条**。实测：一对 analyze 首尾 + 一条 collect end
    # 会显示成 **3 个轮次**（应为 1）；而 collect end 还会把
    # 孤儿 analyze start 的数量差抹平，让真实的中断看不出来。
    # 只取 analyze 的 `phase=end`：它对应“一轮真跑完”，
    # 首尾成对的关系也由它自己的 dur_ms 体现。
    # ⚠⚠ 2026-11-10（Codex P2）：这个列表被**两种用法**消耗：
    #   ① 当「analyze 轮次」展示 —— 应只看 `script=="analyze" and phase=="end"`；
    #   ② `_orphan_start`（判“执行了但被 kill”）—— **必须**看到 `phase=="start"`，
    #      而且不能先按 script 过滤（collect 的记录与它无关，但遇到残缺文件时不该引入新假阳）。
    # 故分成两份：`runs`（全量、保留首尾）+ `runs_rounds`（仅 analyze 的 end）。
    # 原写法只有一份且未过滤 —— 实测：一对 analyze 首尾 + 一条 collect end
    # 会显示成 **3 个轮次**（应为 1），且 collect end 会把孤儿 analyze start
    # 的数量差抹平，真正的中断反而看不出来。
    runs: list[dict[str, Any]] = [
        r for r in read_jsonl(RUNS)
        if str(r.get("t", "")).startswith(date)]
    runs_rounds: list[dict[str, Any]] = [
        r for r in runs
        if r.get("script") == "analyze" and r.get("phase") == "end"]
    # 标注成 list[dict[str, Any]]：read_jsonl 返回裸 list，元素是 Unknown，
    # Unknown 会顺着下面每一处 p.get(...) 扩散成一片 reportUnknownMemberType。
    # 这里标注是如实的 —— 过滤条件已经保证每个元素都是带 "t" 的 dict。
    pushes: list[dict[str, Any]] = [
        p for p in read_jsonl(PUSHES) if str(p.get("t", "")).startswith(date)]

    # 三个集合都标注 set[str]：里面装的确实是字符串（pushes.jsonl 的
    # slot 字段）。不标注时是 set[Unknown]，Unknown 会顺着 add/get 扩散。
    pushed_slots: set[str] = set()
    push_fail: set[str] = set()
    push_dry: set[str] = set()
    # ⚠️ DRY_RUN 演练必须与"推送失败"分开统计。
    # analyze.push() 在 DRY_RUN 生效时写的是 kind="dry" / ok=False，
    # 而原实现把**所有 ok=False 一律归入 push_fail** —— 于是一次演练之后，
    # 那个时段在面板上显示成红色的 `failed`，理由写着
    # 「推送未成功（被拦截或发送失败）：dry」。而 runlog.Status 里
    # skip_dry 的定义恰恰是「人工暂停，**不是故障**」（webui 自己的状态
    # 文案表也照抄了这句）。演练不是故障，面板却报成故障 —— 用户为了
    # 测试推送而按的 DRY_RUN，反而制造了一个需要排查的假告警。
    # 实测（2026-09-27，沙箱，pushes.jsonl 里放一条 slot=10:00 的 dry 记录）：
    #   修复前 -> status=failed，reason="推送未成功（被拦截或发送失败）：dry"
    # 归入 push_dry 后不参与 done/failed 判定，该时段按"确实没推"处理，
    # 理由里明确写出是 DRY_RUN 人工暂停。
    for p in pushes:
        # str() 是如实的：slot 本来就是字符串字段，而 pushes.jsonl 是外部
        # 文件（手工编辑 / 旧版本格式都可能写出非字符串）。转成 str 后
        # key 的类型确定，不必让 Unknown 顺着 add()/比较扩散出去。
        key = str(p.get("slot") or "")
        if p.get("ok"):
            pushed_slots.add(key)
        elif p.get("kind") == "dry":
            push_dry.add(key)
        else:
            push_fail.add(key)

    # ⚠️ fired 的元素必须逐个归一为 str（2026-10-08 审查，两个 agent 独立复现）：
    # state.json 的 fired 混入 dict/list（手工编辑/旧格式/半截写入）时
    # `set()` 抛 TypeError: unhashable -> /api/slots 500 -> 面板主视图全白。
    # analyze.load_st 早就做过同样的元素级过滤，webui 绕过了那道归一；
    # 同文件 1053 行对同一集合也有 isinstance，唯独这里没有 —— 同一文件内不一致。
    fired = {x for x in (st.get("fired") or []) if isinstance(x, str)}
    reasons = {}
    for r in runs:
        s = r.get("status")
        if r.get("phase") == "start":
            continue
        reasons[s] = reasons.get(s, 0) + 1

    # ⚠️ 非交易日必须单独判定，不能落到 missed。
    # 原先只看 is_today 与当前时刻：假期里 09:25/10:00 这些**已过时刻**的
    # 槽位全部被判成红色 missed（"该时段窗口内无执行记录"），而当天
    # runs.jsonl 里明明有几十条 not_trading_day。读者看到一屏红色告警，
    # 会以为系统在假期集体故障 —— 实际是设计如此（休市不推送）。
    # 判据用 runs.jsonl 的实际状态，而不是自己再算一遍交易日 ——
    # 后者会与 analyze/collect 的判定重复，且节假日表更新时两边可能不同步。
    # ⚠️ 必须传**被查看的那一天**（date 是字符串），不能调 is_trading_day()
    # 用默认值 —— 那读的是"真实的今天"，于是回看历史交易日时会把整天
    # 误判成休市（实测：看 09-24 时 09:25 被判 n/a「非交易日」）。
    try:
        _d_obj = datetime.date.fromisoformat(date)
    except Exception:
        _d_obj = None
    _is_holiday = (reasons.get("not_trading_day", 0) > 0) or (
        _d_obj is not None and not is_trading_day(_d_obj))

    now = datetime.datetime.now()
    is_today = (date == today)
    # ⚠️ 2026-11-05（P2）：**未来日期**不能按"过去的日子"判。原先
    # `is_today` 为假就一路落到 missed —— 手动看 `?date=<明天>`（或本机
    # 时钟回拨、数据里出现未来日期）时整页红色"漏投"，而那一天什么都
    # 还没到点。未来日与今天同属"尚未发生"，用同一个 not_yet 口径。
    is_future = bool(_d_obj is not None
                     and _d_obj > datetime.date.today())
    hm = now.hour * 60 + now.minute

    # 结论来源：pushes.jsonl（埋点后，全文）> watch.log（埋点前，首行）
    log_map = _push_conclusions_from_log()

    # 「为什么没推」的逐轮归因：从 runs.jsonl 取该日最后一条 end 的状态
    run_status = {}
    # ⚠️ 孤儿 start：有 start 记录、却没有配对的 end。
    #
    # 成因是进程在 try 里被 **SIGKILL / OOM / 手动 kill** —— analyze.main()
    # 的埋点写在 `finally` 里（一个埋点覆盖全部 5 条静默 return），而
    # finally 在 SIGKILL 下**不执行**，于是磁盘上只剩一条
    # `phase=start status=ok`。实测（2026-09-28，沙箱）：子进程写 start 后
    # `os.kill(os.getpid(), 9)` -> 落盘 1 条 `phase=start status=ok`，
    # 无配对 end。
    #
    # 危害是**归因错误**：原先下面 `elif key in run_status` 只认 end 记录，
    # 孤儿 start 落进 else 分支，reason 写成「该时段窗口内无执行记录」——
    # 而那一轮**确实执行过**（start 记录就是证据）。两句指向完全不同的排查：
    #   「没执行」   -> 查 launchd 是否触发、plist 是否加载
    #   「执行未完成」-> 查进程为何被杀（OOM / 超时 / 手动）
    # 面板给出前者会把人引向错误方向。实测对照（同一份 runs.jsonl）：
    #   09:25 仅 start    -> status=missed  reason=该时段窗口内无执行记录
    #   10:00 start+end   -> status=done
    _orphan_start: set[str] = set()
    for r in runs:
        if r.get("phase") != "end":
            continue
        slot = r.get("slot")
        if slot:
            # ⚠️ 键归一为 str（2026-11-01 修，P3）：runs.jsonl 是外部可编辑
            # 文件，`slot` 混入 list/dict 时 `run_status[slot] = …` 抛
            # `TypeError: unhashable type` → `/api/slots` 500 → 整页白。
            # 与 `pushed_slots` 的 `str(p.get("slot"))` 口径一致。
            run_status[str(slot)] = r.get("status")
    # `--dry` 演练轮次的槽位（analyze 把它们的 end 状态记成 skip_dry）。
    #
    # ⚠️ 与上面 push_dry 是**两个不同来源**，缺一不可：
    #   · push_dry —— pushes.jsonl 里 kind=="dry" 的记录。只有 DRY_RUN
    #     **标志文件**那条路径会写，因为那条路径真的调用了 push()。
    #   · _run_dry —— `--dry` **命令行参数**。它连 push() 都不调，一条推送
    #     记录都不会写，只在 runs.jsonl 落一条 skip_dry。
    # 原先只有 push_dry 一条通道 → `--dry` 演练的槽位在面板上落进 missed，
    # 理由显示 run_status 的原值 "ok"（前端 rmap 译作「已执行」）——
    # 「未推送」与「已执行」自相矛盾，读者看不出这是人工演练。
    # 实测（2026-10-07，10-08 复市预演沙箱）：--dry 轮后 runs.jsonl 末条为
    # {"status":"ok","slot":"09:25","events":[...]}，pushes 无记录。
    _run_dry: set[str] = {k for k, v in run_status.items() if v == "skip_dry"}
    # 当日有 start 记录数 > end 记录数 -> 有轮次没跑完（被 kill）。
    #
    # ⚠️ 判据是**计数差**，不是"有没有 end"。第一版写成"一条 end 都没有"，
    # 那太严格：真实场景是**部分轮次**被 kill（如 11:30 那轮 OOM，前后轮次
    # 都正常），此时 _ended 非空，孤儿被判成不存在 —— 实测（2026-09-28）：
    #   仅 09:25 start + 10:00 start/end -> 09:25 仍报「无执行记录」
    # 而 start 记录**不带 slot**（analyze.py:1700 只传 script/status/phase），
    # 所以无法精确配对到槽位，只能得出"当日存在未完成的轮次"这个结论，
    # 再把所有"missed 且无 end 归因"的槽位指向它。
    #
    # ⚠️ 只对**当日**判定：历史日的计数差也可能是流水被截尾（MAX_BYTES 到顶
    # 后按字节保留尾部，start/end 可能被截在两侧），那时报"未完成"是假告警。
    if is_today:
        # ⚠⚠ 2026-11-11（审查，P2）：**必须限定 script=="analyze"**。
        # 原写法对 `runs`（全量）数头尾，而 `collect` 只写 **end**、
        # `analyze` 才写 **start+end** —— 于是 `_n_end` 恒大于 `_n_start`
        # （实测 2026-11-10：n_start=295、n_end=580），`_n_start > _n_end` **永不成立**。
        # 后果：下面那条「该轮进程已启动但未正常结束」的归因是**死代码**，
        # 被 kill 的轮次仍告知读者「该时段窗口内无执行记录」——
        # 把人引向“launchd 没触发”，而实际是“进程被杀”。两条排查路径完全不同。
        # `[沙箱实测]` 同一份假数据：
        #   含 collect 记录 -> 「该时段窗口内无执行记录」（错）
        #   只留 analyze    -> 「该轮进程已启动但未正常结束」（对）
        _az = [r for r in runs if r.get("script") == "analyze"]
        _n_start = sum(1 for r in _az if r.get("phase") == "start")
        _n_end = sum(1 for r in _az if r.get("phase") == "end")
        if _n_start > _n_end:
            _orphan_start.add("_any")

    # deadline 不是每日都有的槽位 —— 它由 sectors.json 的 deadline_dates
    # 指定「计划窗口最后一日」。原先这里只看 fired，于是非 deadline 日
    # （如 9/28、9/29）过了 13:30 就恒定显示一条红色 missed，
    # 而那天本就不该有这条提醒 —— 面板天天假警报，真出问题时反而看不见。
    # 权威来源与 analyze.detect() 一致：sectors.json 的 deadline_dates。
    _dl_dates = set(cfg_deadline_dates())
    _deadline_applies = date in _dl_dates

    # ⚠️ fired 与推送记录的**交叉校验**。
    # `fired` 有两个来源，只有一个意味着"真的发出去了"：
    #   ① _commit()：push 成功后 inflight -> fired（消息已发出）
    #   ② load_st() 的陈旧清理：进程被 SIGKILL/OOM 打断，把遗留的
    #      inflight **转正**到 fired（消息可能压根没发出去）
    # 而下面 `done = ... or (f"hourly_{key}" in fired)` 对 fired 完全信任，
    # 于是 ② 那类污染会让面板报「已完成」—— 而推送记录里根本没有它。
    # 实测（2026-09-28）：state.json 的 fired 含 hourly_10:00，而
    #   pushes.jsonl 当日 12 条记录里**没有** slot='10:00'
    #   （09:25 / 11:30 / 13:30 / 14:30 都在，独缺 10:00）。
    #   成因是 10:03 那轮被 urgent 分支提前 return 吞掉了 hourly 键
    #   （见 analyze.py 的修复注释），下一轮 load_st 把它转正成 fired。
    #   结果：**消息从未发出，面板却报已完成**，排查者不会再看第二眼。
    #
    # 判据限定两条，避免假警报：
    #   · 仅**当日** —— st 只在 st["date"]==date 时非空，历史日本就走
    #     runs.jsonl/pushes.jsonl 重建，fired 为空集，天然不触发。
    #   · 当日**已有**推送记录 —— 证明埋点在覆盖。埋点 2026-09-24 才上线，
    #     更早的日子 pushes.jsonl 为空，不加这道限定会把它们全部误报。
    # 时序上也安全：runlog.push() 写在 subprocess.run **之前**，而
    # _commit() 在推送成功之后才写 fired —— "有 fired 无记录"不可能由
    # 正常成功路径产生，只可能来自 ② 那类转正。
    _fired_no_push: set[str] = set()
    # ⚠️ 限定条件从 `is_today` 放宽为"st 仍然有效"（2026-10-25 审查）：
    # 原注释声称"历史日 fired 为空集、天然不触发"，但那只对**更早**的日子
    # 成立 —— state.json 要到下一个交易日才被 load_st 重置，所以**最近一个
    # 交易日**回看时 st 仍匹配该日、fired 被全信，而 is_today 已是 False
    # → 交叉校验被跳过 → 「inflight 转正」型假 done 重现。
    # `[实测]` state.json date=2026-10-23 且 fired 含 hourly_09:25，而
    # 10-23 的 pushes.jsonl 里没有任何 slot=09:25 的成功记录 ——
    # 面板该槽却显示 done（正是本函数要防的那类污染）。
    # 第二个条件 `and pushes` 已排除"埋点前的历史日"（那时 pushes 为空，
    # 不会误报），故放宽 is_today 无副作用。
    if st and pushes:
        _ok_slots = {str(_p.get("slot") or "") for _p in pushes if _p.get("ok")}
        for _k in fired:
            if isinstance(_k, str) and _k.startswith("hourly_"):
                _lbl = _k[7:]
                if _lbl not in _ok_slots:
                    _fired_no_push.add(_lbl)

    rows = []
    for key, role, planned in SLOTS:
        done = (key in fired) or (f"hourly_{key}" in fired) or (key in pushed_slots)
        concl = _slot_conclusion(key, pushes, log_map, date)
        # ⚠️ 顺序要紧：**failed 必须在 done 之前**。
        # 原先 `if done or concl` 在最前，而 concl 只看有没有正文、不看 ok，
        # 于是"被拦下"的记录把状态顶成 done，`elif key in push_fail`
        # 这条分支永远不可达 —— 死代码。
        if key == "deadline" and not _deadline_applies \
                and key not in pushed_slots and not concl \
                and key not in push_fail and key not in _fired_no_push:
            # 当日不是计划窗口最后一日，本无此槽位。
            # ⚠️ 但**必须**先让位于"实际推过/有记录"的事实（2026-10-25 审查）：
            # 原判据只看 `date in deadline_dates`，而那个配置会随窗口前移
            # 而改写 —— 10-23 真的推过「截止提醒」（pushes.jsonl 有
            # slot=deadline, ok=true），之后 deadline_dates 改成 11-06，
            # 面板回看 10-23 就把这个槽显示成 n/a（"本无此时段"），
            # 与推送记录直接矛盾。以记录为准，配置只用于"从未有过"的情形。
            status = "n/a"
        elif key in push_dry and key not in pushed_slots:
            # DRY_RUN 演练：不是故障，也不能算 done（确实没推出去）。
            # 用 skipped（既有状态）而不是 failed —— 与 skip_dry 的
            # "人工暂停，不是故障"语义一致。
            status = "skipped"
        elif key in push_fail and key not in pushed_slots:
            status = "failed"       # 推过但**没成功**：拦截/超时/缺 webhook
        elif key in _fired_no_push:
            # 状态文件说"推过了"，推送记录说"没有" —— 以**推送记录**为准：
            # 消息是否送达才是读者关心的，而记录是 push() 亲手写的。
            status = "missed"
        elif key in _run_dry and key not in pushed_slots:
            # `--dry` 演练（runs.jsonl 落 skip_dry）：人工暂停，不是故障，
            # 也不能算 done —— 确实一条都没推出去。与上面 push_dry 同语义，
            # 区别只在来源（命令行参数 vs DRY_RUN 标志文件）。
            #
            # ⚠️ 位置在 push_fail **之后**（不像 push_dry 那样在其之前）：
            # 若该槽位先有真实推送失败、后又跑了演练，两个信号都在 ——
            # 此时"推送失败"才是要人处理的事实，演练只是事后排查动作，
            # 不能被 skipped 顶掉（失败信号被静默 = 最坏的方向）。
            # 演练单独出现时（无失败、无成功）才落到这里。
            status = "skipped"
        elif done or concl:
            status = "done"
        elif is_future:
            # 未来日：一切槽位都还没到点（与当日时刻无关）
            status = "not_yet"
        elif is_today and _slot_min(planned) is not None \
                and hm < _slot_min(planned):
            status = "not_yet"
        elif is_today and _slot_min(planned) is not None \
                and hm < _slot_min(planned) + _slot_window_grace(key):
            # ⚠⚠ 2026-11-10（Codex P2）：**到点不等于漏推**。
            # 原判据只看“计划时刻是否已到”，于是 10:05 时
            # 10:00 那一格（还有 20 分钟有效窗口）已经被标成
            # 「漏推」—— 而它可能正在运行、或下一轮就推。
            # 与 `analyze.SLOT_TTL` / `HOURLY_RETRY_GRACE` 同一口径：
            # 窗口内是“等待/运行中”，过了窗口才叫漏推。
            status = "pending"
        elif _is_holiday:
            # 非交易日：本就不该有推送，不是"漏推"
            status = "n/a"
        else:
            status = "missed"
        row = {"key": key, "kind": "slot", "role": role,
               "planned": planned, "status": status}
        # 窗口起点可能**早于**计划时刻（见 analyze.SLOT_OPEN_EARLY）。
        # 面板只显示 planned 会让读者困惑："计划 09:25，为什么 09:20 就推了？"
        # —— 那一轮是**设计内**的，不是调度漂移。把窗口一并给出，
        # 面板才能如实回答"这个时段什么时候可以推"。
        _eo = _SLOT_OPEN_EARLY.get(key, 0)
        if _eo:
            _t = _slot_min(planned)
            if _t is None:
                _t = 9999       # 仅供窗口回显的兜底显示（不参与状态判定）
            row["window_from"] = f"{(_t - _eo)//60:02d}:{(_t - _eo)%60:02d}"
            # ⚠⚠ 2026-11-10：上限必须用 `_slot_window_grace()`，
            # **不能**用裸 `_SLOT_TTL`。两者差一个 `_RETRY_GRACE`（15 分钟）：
            # 上面判 `pending` 用的是宽限，而这里凭显示只写 TTL。
            # `[实测]` 10:00 槽：判定 35 分钟内都算「等待中」，
            # 而面板写「窗口 09:43~10:20」—— 10:20~10:35 区间里状态还是 pending、
            # 界面却已经说窗口过了。读者只能看到这两个数，
            # 不一致的话他会以为面板坏了。
            _wto = _slot_min(planned)
            _wto = (9999 if _wto is None else _wto) + _slot_window_grace(key)
            row["window_to"] = f"{_wto//60:02d}:{_wto%60:02d}"
        # ⚠️ `concl` 只在**状态确实是 done** 时才有意义。
        # 原先无条件 `if concl:` 在最前，于是被判 n/a 的槽位（当日非计划窗口
        # 最后一日）也会带上它"就近认领"到的正文，而 `elif status == "n/a"`
        # 那条 reason 分支因此**永远不可达**（死代码，同 failed 那条的历史）。
        # 实测（2026-09-28，看 09-23）：deadline 行 status=n/a、reason=None，
        # 却挂着 conclusion="13:30 午后开局 · 资金未接力上午方向"。
        # 前端虽已改成原因优先，但数据层不该把这种自相矛盾的组合发出去
        # —— 别的消费方（API 调用者）看到的是同一份 JSON。
        if concl and status == "done":
            row["conclusion"] = concl.get("conclusion")
            row["src"] = concl.get("src")
            row["at"] = concl.get("at")
            if concl.get("full"):
                row["full"] = concl["full"]
        elif status == "n/a":
            row["reason"] = ("非交易日（休市），本无此时段推送" if _is_holiday
                             else "当日不是计划窗口最后一日")
        elif status == "failed":
            # 说明是"推了没推成"，并带上具体原因（blocked/no_webhook/超时）
            _why = ""
            for _p in pushes:
                if (_p.get("slot") or "") == key and not _p.get("ok"):
                    _why = str(_p.get("note") or _p.get("kind") or "")
                    break
            row["reason"] = "推送未成功（被拦截或发送失败）" + (f"：{_why}" if _why else "")
        elif status == "skipped" and key in push_dry:
            row["reason"] = "DRY_RUN 标志生效，本轮人工暂停（非故障）"
        elif status == "skipped" and key in _run_dry:
            # 与上一条分开写：来源不同（命令行 --dry vs 标志文件），
            # 排查时要能一眼看出是哪种演练留下的。
            row["reason"] = "--dry 演练轮（runs.jsonl: skip_dry），本轮人工暂停（非故障）"
        elif status == "missed" and key in _fired_no_push:
            # ⚠️ 必须在下面通用的 missed 归因**之前** —— 否则会落到
            # 「该时段窗口内无执行记录」，而那一轮其实执行过、只是没推成，
            # 归因方向完全不同（查进程为何被打断，而非查 launchd 是否触发）。
            row["reason"] = ("状态文件已标记完成，但当日无该时段的推送记录"
                             "—— 疑似进程被打断后 load_st 把 inflight 转正到 "
                             "fired（消息实际未发出）")
        elif status == "missed":
            # 没推成 —— 说清为什么
            if not is_today:
                row["reason"] = "当日无该时段推送记录"
            elif key in run_status:
                row["reason"] = run_status[key]
            elif "_any" in _orphan_start:
                # 该轮进程起了（有 start）但没写完 end —— 多半被 SIGKILL/OOM
                # 打断。必须与"根本没执行"区分开，否则排查方向会错。
                row["reason"] = ("该轮进程已启动但未正常结束（有 start 无 end）"
                                 "—— 查是否被 OOM/超时/手动 kill 打断，"
                                 "而非 launchd 未触发")
            else:
                row["reason"] = "该时段窗口内无执行记录"
        rows.append(row)

    # 事件类：13 个固定键 + sectors.json 里每个 watch 板块一个动态键
    cfg = read_json_obj(SECTORS)
    # deadline / summary 已在上面的时段表里各占一行，此处不再重复 ——
    # 否则页面上同一件事显示两行，且两处状态口径不同
    # （时段行认 pushes.jsonl，事件行只认 fired），看起来像两条独立记录。
    fixed = ["hstech_up", "hstech_down", "hstech_drop", "storage_rev",
             "semi_out", "semi_in", "semi_surge", "idx_weak", "idx_strong",
             "gold_up", "gold_dn", "hs300_weak"]
    # 事件触发时刻：pending.json 的 events 带 at 字段
    # 标注成 dict[str, dict[str, object]]：值确实是 {at, msg} 两个任意 JSON 值。
    # 不标注时是 dict[Unknown, Unknown]，Unknown 会顺着下面的
    # _alias_at 一路扩散（实测本处一带 +16 条 reportUnknown*）。
    ev_at: dict[str, dict[str, object]] = {}
    pj = read_json_obj(PENDING)
    _pj_events = pj.get("events")
    if pj.get("date") == date and isinstance(_pj_events, list):
        for e in _pj_events:
            if isinstance(e, dict) and e.get("k"):
                # ⚠️ `at` 归一为 str（2026-11-01 修，P3）：pushes/pending 是
                # 外部可编辑文件，同一个别名混入 str 与 int（`"09:44"` 与
                # `944`）时，下面的 `> "99:99"` 比较抛
                # `TypeError: '>' not supported between 'int' and 'str'`
                # → `/api/slots` 500 → 整页白。同 `pushed_slots` 的
                # `str(p.get("slot"))` 口径。
                _at_v = e.get("at")
                ev_at[str(e["k"])] = {
                    "at": str(_at_v) if _at_v is not None else None,
                    "msg": e.get("msg")}

    # ⚠️ **命名空间映射**：collect.py 与 analyze.detect() 用**两套不同的事件键**
    # 描述同一批异动，而事件表只认 analyze 那一套：
    #   collect 写入 pending.json 的  ->  analyze/webui 使用的
    #     fund_90.BK1036             ->  watch_90.BK1036
    #     idx_1.000001               ->  idx_weak（上证）/ hstech_down 等
    #     hstech_up/down、storage_rev ->  同名（这一半是对的）
    # 后果：**快照通道触发的异动在面板上一条都不显示**。
    # 实测（生产 pending.json，09-24）：7 条事件里 6 条是 fund_*、
    # 1 条 storage_rev —— fund_* 那 6 条（白酒/贵金属/化学制药/PCB/
    # 医疗研发外包/CPO 的主力资金异动）在事件表里全无踪影，
    # 因为它们被拿去查 `watch_<code>` 的 fired，而那个键是 analyze 写的。
    # 而 collect 恰恰是**唯一**记录"什么时候触发了什么"的通道
    # （analyze 只写 fired，不写 at/msg），所以这是面板信息量的净损失。
    # 先把 pending.json 的事件按别名归并（同一别名的多条只留最早一条）
    _alias_at: dict[str, dict[str, object]] = {}
    for _raw, _info in ev_at.items():
        _a = _ev_alias(_raw)
        if _a not in _alias_at:
            _alias_at[_a] = dict(_info)
            _alias_at[_a]["raw_key"] = _raw
        elif _info.get("at") and (_alias_at[_a].get("at") or "99:99") > _info["at"]:
            _alias_at[_a]["at"] = _info["at"]
    # ⚠️ 与时段区同一套口径。原先这里只判 is_today，于是**回看假期**
    # （如国庆 10-01）时事件区整片显示红色 missed —— 而时段区已按
    # _is_holiday 判成 n/a，同一页面两处口径不一致，读者会以为
    # "时段没推是对的，但事件漏了一堆"。休市日本就不该有事件。
    def _ev_status(k):
        # ⚠️ `_alias_at` 里有记录 = **collect 已经观测到并记下了这次触发**
        # （pending.json 的 events 带 at/msg，collect 是唯一记录它的通道）。
        # 而 fired 只有 analyze 走完整条推送链路后才会写。
        # 两者不等价：analyze 可能因为抢锁失败/配额/异常**根本没跑到 detect**，
        # 此时事件在快照里明明触发了、面板却显示"未触发" —— 且下面的
        # at/msg 会把触发时刻照常打在"触发"列，同一行自相矛盾。
        # 实测（2026-09-27）：pending 有 fund_90.BK0896@09:44、fired 空
        # -> status='n/a' 而 at='09:44'。
        _k_has_ev = k in _alias_at
        if k in fired or _k_has_ev:
            return "done"
        if _is_holiday:
            return "n/a"
        return "not_yet" if (is_today or is_future) else "missed"

    ev_rows = []
    for k in fixed:
        row = {"key": k, "kind": "event", "status": _ev_status(k)}
        if _is_holiday and row["status"] == "n/a":
            row["reason"] = "非交易日（休市），本无此事件"
        if k in _alias_at:
            row["at"] = _alias_at[k].get("at")
            row["msg"] = _alias_at[k].get("msg")
            # 原始键一并带出：collect 的 fund_* 与 analyze 的 watch_* 是两套
            # 命名，排查时要知道这条记录**实际来自哪条通道**。
            if _alias_at[k].get("raw_key") != k:
                row["src_key"] = _alias_at[k]["raw_key"]
        ev_rows.append(row)
    for x in _cfg_list(cfg, "watch"):
        # ⚠️ 必须经 M._sid 归一（2026-10-08 审查）：detect() 生成的键是
        # `watch_{_sid(code)}`（短码会自动补市场前缀 90.），而这里原先裸读
        # x['code'] —— sectors.json 写短码 BK1036 时事件行键成 watch_BK1036，
        # 与 fired 里的 watch_90.BK1036 不匹配 -> 永远显示 not_yet。
        _wcode = x.get("code")
        # ⚠️ code 必须是**非空字符串**才建行（2026-11-01 修，P4）。
        # `_cfg_id` 的同类修复没覆盖到这里：`code` 是 None / `["BAD"]` 时
        # `_sid` 拿不到字符串，键变成 `watch_None` / `watch_90.['BAD']` ——
        # 事件表里凭空多出一行没有任何意义的"幽灵行"，且它永远 not_yet
        # （读者会以为漏拉了一个事件）。`sectors.json` 是用户可编辑文件，
        # "值类型写错"是最常见的编辑事故形态。
        # `[实测]` 沙箱：`{"code": null}` -> `watch_None`；
        # `{"code": ["BAD"]}` -> `watch_90.['BAD']`。
        if not isinstance(_wcode, str) or not _wcode.strip():
            # 只读面板不写 watch.log（本模块刻意不依赖主链路），
            # 静默跳过即可 —— 面板显示的是"这个配置项没法建事件行"，
            # 而不是一个永远 not_yet 的幽灵行。
            continue
        try:
            import marketlib as _M
            _wcode = _M._sid(_wcode) if _wcode else _wcode
        except Exception:
            pass
        if not isinstance(_wcode, str) or not _wcode:
            continue
        k = f"watch_{_wcode}"
        row = {"key": k, "name": x.get("name"), "kind": "event",
               "status": _ev_status(k)}
        if _is_holiday and row["status"] == "n/a":
            row["reason"] = "非交易日（休市），本无此事件"
        if k in _alias_at:
            row["at"] = _alias_at[k].get("at")
            row["msg"] = _alias_at[k].get("msg")
            if _alias_at[k].get("raw_key") != k:
                row["src_key"] = _alias_at[k]["raw_key"]
        ev_rows.append(row)

    return {
        "date": date, "today": today, "is_today": is_today,
        "trading_day": is_trading_day(datetime.date.fromisoformat(date)),
        "slots": rows, "events": ev_rows,
        # ⚠️ fired 是**内部键**（hourly_09:25 / watch_90.BK0465 /
        # idx_1.000001_2026-09-24），前端把它整列渲染成 chips，读者看到的是
        # 一堆 `watch_90.BK0465` 这种没有意义的面板内部标识。
        # 这里转成人类可读的标签：时段键 -> 时段名，watch_ 键 -> 板块名，
        # idx_/sec_/mkt_（alert 的紧急推送键）-> 带上原始键以便溯源。
        # 保留 raw 字段供需要精确定位的人使用。
        "fired": _fired_labels(fired, cfg),
        # ⚠️ 不能硬编码 6。这曾是一份**手工副本**：_analyze_consts 的
        # 全部意义就是消灭"面板显示的值与 analyze 真值漂移"（见其上方注释
        # 记的三次事故），而 max_push 恰好漏在外面 —— 改 analyze.MAX_PUSH
        # 后面板仍显示 6，配额用满时看着像"还有额度"。实测（沙箱）把
        # analyze.MAX_PUSH 改成 8，面板照旧显示 "count / 6"。
        "count": st.get("count"), "max_push": _A_MAX_PUSH,
        "inflight": st.get("inflight", []),
        "pending_idx": st.get("pending_idx", []),
        "run_reasons": reasons,
        # 轮次数只算 analyze 的 end 记录 —— 里面混进 collect 的记录与
        # analyze 的 start，会把 1 轮显示成 3 轮（见 `runs` 处注释）。
        "run_count": len(runs_rounds),
        # 全量行数单独给出，便于对账“底下到底写了多少行”
        "run_rows": len(runs),
        # ⚠️ 与时段表同一口径：DRY_RUN 演练**不算"尝试"**。
        # 原先 push_count = len(pushes)，而演练也写一条记录 —— 于是手动跑
        # 一次 DRY_RUN 之后，卡片显示「今日推送 1 / 3」，读者以为有 2 次
        # 推送失败，实际上其中 1 次是演练、1 次才是真失败。演练单独计数。
        # 实测（2026-09-27，沙箱）：1 成功 + 1 blocked + 1 dry -> 显示 1/3。
        # ⚠️ 三个计数必须**互斥且完备**（分子分母同口径）。
        # 原先 push_ok 只判 `p.get("ok")`，**不排除 dry** —— 它依赖
        # "analyze.push 在 DRY 时一定写 ok=False" 这个**跨模块约定**
        # （实测 analyze.py:312 确实是 `_rl.push(slot,"dry",t,False,...)`）。
        # 约定成立时口径正确；但一旦有人把那个 False 改成 True，
        # 分子就会含演练、分母不含 —— 卡片显示「1/1」而实际是 0 次真推送。
        # 这种"依赖另一模块的写入约定"的判据在跨模块重构时必然失守，
        # 所以在**消费点**显式排除，不再依赖上游的 ok 取值。
        # 三式互斥（dry / ok / 其余），且 push_count = push_ok + 失败数，
        # 三者相加恒等于 len(pushes)。
        # ⚠️ kind="agent"（调研 Agent 的摘要，2026-10-15 起记账进此文件）
        # 不计入主系统推送口径 —— 否则面板「X / Y」每日多出一条 Agent 记录。
        "push_count": sum(1 for p in pushes
                          if p.get("kind") not in ("dry", "agent")),
        "push_ok": sum(1 for p in pushes
                       if p.get("kind") not in ("dry", "agent")
                       and p.get("ok")),
        "push_dry": sum(1 for p in pushes if p.get("kind") == "dry"),
    }


def _slot_window_grace(key):
    """该槽位的**有效窗口**宽度（分钟）。

    直接取 `analyze.SLOT_TTL`，逼不得已时就用默认 20 +
    `HOURLY_RETRY_GRACE`。两处各写一份就会出现
    “面板说漏推、实际还在窗口内”的分叉。
    """
    # ⚠ 用本文件的 `_FALLBACK_SLOT_TTL`，**不要 import analyze**：
    # 那样会触发连网、写 state.json 等副作用（见文件头注释）。
    # 两份副本的一致性由 selfcheck 的 [常量一致性] 比对。
    try:
        return int(_FALLBACK_SLOT_TTL.get(key, 20)) + _RETRY_GRACE
    except Exception:
        return 35


def _slot_min(planned):
    """计划时刻 -> 距 0 点的分钟数。**无计划时刻返回 None**，不是 9999。

    ⚠️ 原返回 9999（2026-10-12 审查 P3-35 改）：`event`/`deadline`/`summary`
    这类**无固定时刻**的槽位 planned 是空串，`hm < 9999` 恒真 -> 状态被钉在
    `not_yet`（"还没到"）—— 而当天已经过去、事件就是没触发时，"还没到"是
    **误导**（读者会以为再等等），正确语义是"未触发"（n/a 语义）。改返回
    None：调用点用 `is not None` 分支，无计划时刻的槽位**跳过** not_yet
    判定，落到其后的状态推导（未触发 -> done? concl? -> 今日未触发）。"""
    m = re.match(r"(\d{2}):(\d{2})", planned or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


# ---------------- API ----------------

def api(path, q):
    if path == "/api/summary":
        lc = launchd_info()
        return {
            "now": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "today": datetime.date.today().isoformat(),
            "trading_day": is_trading_day(),
            # ⚠️ 不回吐数据目录的**绝对路径**（2026-10-29 删，P3）：
            # `do_GET` 的 500 分支专门写了长注释——「异常消息里可能带绝对
            # 路径…同机任何本地进程/浏览器标签都能打这个端口，等于把内部
            # 路径结构免费送出去」，据此把异常详情改成了固定文案。
            # 同一个威胁模型下这里却主动送出 `DIR`，而前端 `render()`
            # 从未使用过 `sum.dir` —— 属自相矛盾。要排查时看 `webui.out`
            # 的启动行（那里有「数据目录: …」），不需要对外暴露。
            "launchd": list(lc.values()),
            "health": collect_health(),
            "rate": rate_info(),
            "config": config_summary(),
            "files": _file_list(),
        }
    if path == "/api/runs":
        date = _q_date(q)
        status = (q.get("status") or [None])[0]
        script = (q.get("script") or [None])[0]
        # ⚠️ limit 必须在**过滤之后**才截尾，不能先截尾再过滤。
        # read_jsonl 的 limit 是从文件**尾部**取 N 行，而过滤是按日期筛选 ——
        # 先取尾 5000 行会把更早的日期整段丢掉。实测（2026-09-26）：
        # runs.jsonl 的保留量约 20000 行（≈24 天，runlog 按 60%×4MB 截尾），
        # 每日约 839 条，尾部 5000 行只覆盖最近 ~6 天 —— 面板要查第 7 天
        # 以前的记录时，数据明明在盘上却显示空。而本文件上限 2.4MB，
        # 全量解析只要几十毫秒，对本地只读工具完全可接受。
        rows = read_jsonl(RUNS)
        if date:
            rows = [r for r in rows if str(r.get("t", "")).startswith(date)]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if script:
            rows = [r for r in rows if r.get("script") == script]
        return {"rows": rows[-800:], "total": len(rows)}
    if path == "/api/slots":
        # ⚠️ 必须与 `/api/runs`、`/api/pushes` 同源走 `_q_date`（2026-10-29 修，P2）：
        # 原先直接取原始字符串，而 `build_slots` 里的 `fromisoformat` 会
        # **接受**基本格式（`20261012`）与 ISO 周日期（`2026-W41-1`）——
        # 校验通过后，所有过滤却都是 `startswith("YYYY-MM-DD")`，形状不匹配
        # → 全空 → 每个时段被判 `missed`。`[实测]` 生产数据：
        # `?date=2026-10-12` → 8 个时段 done；`?date=20261012` → 7 个 missed。
        # 主视图凭空报出一整页「漏推」，比 500 更能误导排查。
        # 非法形状抛 `BadDate`，由 `do_GET` 统一译成 400（与另两个端点同一出口）；
        # `build_slots` 内部保留"畸形即回退今天"的兜底（手误不该让整页报错），
        # 两者分工不冲突：这里挡的是**形状不符**，那里兜的是**语义非法**。
        return build_slots(_q_date(q))
    if path == "/api/pushes":
        date = _q_date(q)
        # limit 同理必须在过滤之后：pushes.jsonl 行长 7 倍（含推送全文），
        # 保留量约 2900 行，虽然当前还没超过 5000，但一旦超过就会重演
        # /api/runs 那个"更早日期查不到"的问题。统一按全量读 + 过滤 + 截尾。
        rows = read_jsonl(PUSHES)
        if date:
            rows = [r for r in rows if str(r.get("t", "")).startswith(date)]
        # blocked 记录含被拦截原文 → 打警示标记
        # ⚠️ 必须**另建 dict**，不能原地 `r["warning"] = …`（2026-10-27 改）：
        # `read_jsonl` 现在按 (mtime, size) 缓存解析结果，原地写会把标记
        # **留在缓存的对象上**，污染后续所有读同一份缓存的请求 ——
        # 这里恰好写的是同一个常量所以不至于出错，但"请求处理函数原地改
        # 共享解析结果"是迟早要炸的形态（下一个字段就是脏数据）。
        rows = [
            (dict(r) | {"warning": "blocked_content"}
             if r.get("kind") in ("blocked", "no_webhook") else r)
            for r in rows
        ]
        return {"rows": rows[-300:], "total": len(rows)}
    if path == "/api/log":
        d = parse_log()
        # ⚠️ 响应里**不带 lines** —— 前端只消费 folds（见 render 里的
        # `for(const f of log.folds...)`），而 lines 是每行一条完整记录的
        # 原始数组，占响应体积的 95% 以上。实测（2MB 轮转满的日志）：
        #   lines 27235 条 -> 序列化 10.08 MB，单次 HTTP 响应
        # 当前生产日志 0.24MB 时也有 0.74MB。面板是本地只读工具，
        # 每次刷新传 10MB 纯属浪费，且 lines 里每条都带 extra[] 续行数组。
        # 保留 lines_count 供展示，数据本身可从 watch.log 直接看。
        #
        # 实现方式是**另建一个 LogResponse**，而不是从 d 里 pop 掉 lines：
        #   · lines_count 是**派生**键（不来自 parse_log），而 ParseResult
        #     是 TypedDict —— 往 d 上加未声明的键会被报
        #     "Could not assign item in TypedDict"。
        #   · `d.pop("lines", [])` 带默认值会把返回类型拓宽成
        #     object | list[Any]，于是 len(...) 报 "object 不能赋给 Sized"。
        # 新建对象两个问题都不存在：原 d 不再被复用，lines 自然不会
        # 出现在响应里。（这段注释此前还停留在已放弃的 pop 方案上。）
        # ⚠️ 2026-11-05（P2）：folds 只回最近 600 组 —— 原先**没有任何
        # 提示**，页面看起来就像"日志只有这些"。附带总数/截断数，由
        # 前端显式说明（与 rows_truncated 同一原则：只截展示，不丢数据）。
        _all_folds = d["folds"]
        _resp: LogResponse = {
            "lines_count": len(d["lines"]),
            "folds": _all_folds[-600:],
            "stats": d["stats"],
            "folds_total": len(_all_folds),
            "folds_truncated": max(0, len(_all_folds) - 600),
        }
        # ⚠️ 单个折叠组的 rows 也要**限量**。前端把整组渲染进一个 <pre>
        # （`f.rows.map(...).join("\n")`），而折叠是"连续同类行"——
        # 最坏情形（某条噪音模式吞掉整个日志）实测 2MB 日志折叠成 **1 组
        # 41943 行**，一次性进 DOM 会卡死浏览器。生产当前最大组 628 行。
        # 只截断**展示**，不丢数据：完整内容仍在 watch.log 里，
        # 且 rows_total 保留了真实条数。
        _CAP = 300
        for _f in _resp["folds"]:
            _rs = _f.get("rows") or []
            _f["rows_total"] = len(_rs)
            if len(_rs) > _CAP:
                _f["rows"] = _rs[:_CAP]
                _f["rows_truncated"] = len(_rs) - _CAP
        return _resp
    if path == "/api/launchd":
        return {"jobs": list(launchd_info().values())}
    if path == "/api/analyze_health":
        # analyze 链路健康度：与 collect 心跳分开看。
        # collect 只管采集，analyze 才是推送链路 —— 二者会独立失效
        # （实测：marketlib 语法错误会让 analyze 每轮崩在 import 阶段，
        #   而 collect 同样崩，但若只坏在 analyze 独有的依赖上，
        #   采集心跳会显示正常、推送却已全停）。
        # 判据：取 runs.jsonl 里 analyze 的最近一条 end 记录，
        # 看它的时间距今多久、状态是否 import_fail/error。
        rows = [r for r in read_jsonl(RUNS)
                if r.get("script") == "analyze" and r.get("phase") == "end"]
        last = rows[-1] if rows else {}
        ts = last.get("ts")
        age = (time.time() - ts) if isinstance(ts, (int, float)) else None
        st = last.get("status")
        return {
            "last": last.get("t"),
            "age_sec": age,
            "status": st,
            "note": last.get("note"),
            "bad": st in ("import_fail", "error"),
            "stale": (age is None or age > 1800),
        }
    if path == "/api/health":
        return {"health": collect_health(), "rate": rate_info(),
                "days": _available_days()}
    if path == "/api/boards":
        # 全市场涨跌榜 + 算法热点榜。数据源是 hot_cache.json —— 与推送
        # 用的**同一份**（`hot.analyze_cached` 落盘），不另发请求、不另算。
        # 面板只读它，不写（写入只在 hot.analyze）。
        #
        # ⚠️ 形状守卫必须**逐个键、逐条、逐字段**判（hot_cache.json 在磁盘上，
        # 可能被手工编辑 / 旧版本 schema / 半截写入）—— 这与
        # analyze.build_brief 里对 hot 的四层守卫是同一条契约：面板是
        # 127.0.0.1 上任何人/任何标签页都能打的端口，一个 TypeError 就让
        # 整个面板 500 全白（同 [[market-watch-webui-attr-injection]] 的教训：
        # 面板崩了排查能力就归零）。
        try:
            import time as _time_bd
            _hb = read_json(HOT_CACHE_PATH, None)
            _age = None
            _d = {}
            if isinstance(_hb, dict):
                _ts = _hb.get("ts")
                if isinstance(_ts, (int, float)) and not isinstance(_ts, bool):
                    _age = max(0, int(_time_bd.time() - _ts))
                _dd = _hb.get("data")
                if isinstance(_dd, dict):
                    _d = _dd
            _rows = {"top_up": [], "top_dn": [], "strong": [], "start": []}
            for _k in _rows:
                _v = _d.get(_k)
                if not isinstance(_v, list):
                    continue
                for _r in _v[:8]:
                    if not isinstance(_r, dict):
                        continue
                    _nm = _r.get("name")
                    _ch = _r.get("chg")
                    if not isinstance(_nm, str) or not _nm:
                        continue
                    # chg 必须是真的数：bool 是 int 子类，nan/inf 会让
                    # 前端 toFixed 出 "NaN%"/"Infinity%"
                    if not isinstance(_ch, (int, float)) or isinstance(_ch, bool):
                        continue
                    if _ch != _ch or _ch in (float("inf"), float("-inf")):
                        continue
                    _rows[_k].append({
                        "name": _nm, "chg": _ch,
                        # ⚠️ pct/amt/vr 必须与上面的 `chg` **同一套判据**
                        # （2026-10-29 修，P2）：原先只写 `isinstance(_, (int,float))`
                        # —— `bool` 是 int 子类、`nan`/`inf` 也是 float，两者都能
                        # 通过。前端直接 `r.pct.toFixed(1)`：
                        #   · `true.toFixed` → TypeError → `render()` reject →
                        #     整页只剩「载入失败」；
                        #   · 裸 `NaN`/`Infinity` 进响应体（`json.dumps` 默认
                        #     `allow_nan=True`）→ 浏览器 `JSON.parse` 抛
                        #     `Unexpected token 'N'` → 同上整页白。
                        # 本函数自己的注释写着「形状守卫必须逐个键、逐条、
                        # 逐字段判」，实际只判了 chg —— 这正是本条修的东西。
                        "pct": _num_or_none(_r.get("pct")),
                        "amt": _num_or_none(_r.get("amt")),
                        "vr": _num_or_none(_r.get("vr")),
                        "stage": _r.get("stage") if isinstance(
                            _r.get("stage"), str) else None,
                    })
            return {"age_sec": _age,
                    "pool": _d.get("pool") if isinstance(
                        _d.get("pool"), int) else None,
                    "dedup": _d.get("dedup") if isinstance(
                        _d.get("dedup"), int) else None,
                    "rows": _rows}
        except Exception:
            # 面板端点**永不抛**：读失败一律回空榜，让其余段照常渲染
            return {"age_sec": None, "pool": None, "dedup": None,
                    "rows": {"top_up": [], "top_dn": [], "strong": [],
                             "start": []}}
    return None


def _file_list():
    out = []
    for name in ("watch.log", "watch.log.1"):
        p = os.path.join(LOGS_DIR, name)
        if os.path.exists(p):
            try:
                st = os.stat(p)
                out.append({"name": name, "size": st.st_size,
                            "mtime": datetime.datetime.fromtimestamp(
                                st.st_mtime).strftime("%m-%d %H:%M:%S")})
            except Exception:
                pass
    for name in ("state.json", "pending.json", "rate.json",
                 "hot_cache.json", "vr_base.json"):
        p = os.path.join(STATE_DIR, name)
        if os.path.exists(p):
            try:
                st = os.stat(p)
                out.append({"name": name, "size": st.st_size,
                            "mtime": datetime.datetime.fromtimestamp(
                                st.st_mtime).strftime("%m-%d %H:%M:%S")})
            except Exception:
                pass
    for name in ("runs.jsonl", "pushes.jsonl"):
        p = os.path.join(HIST, name)
        if os.path.exists(p):
            try:
                st = os.stat(p)
                out.append({"name": "history/" + name, "size": st.st_size,
                            "mtime": datetime.datetime.fromtimestamp(
                                st.st_mtime).strftime("%m-%d %H:%M:%S")})
            except Exception:
                pass
    return out


def _available_days():
    # ⚠️ 必须**全量读**，不能带 `limit`（2026-10-27 修）。
    # `read_jsonl` 的 limit 是**从尾部截**（`out[-limit:]`），所以
    # `limit=20000` 在本文件上限 2.4MB / 实测 30545 行的 runs.jsonl 上
    # 只覆盖到第 10545 行 —— 那一条的时间是 2026-10-03，即本函数**看不见
    # 24 天以前**的日子，而它的作用恰恰是"告诉前端哪些日期有数据"。
    # `/api/runs` 已经因为同一个原因把 limit 去掉过（那里的注释写着
    # "以前的记录时，数据明明在盘上却显示空"）—— 本函数是那次修改漏掉的
    # 另一半。现在 `read_jsonl` 带 (mtime, size) 缓存，全量读的代价只在
    # 文件变更那一次刷新发生。
    days = set()
    for r in read_jsonl(RUNS):
        t = str(r.get("t", ""))[:10]
        if t:
            days.add(t)
    for p in read_jsonl(PUSHES):
        t = str(p.get("t", ""))[:10]
        if t:
            days.add(t)
    return sorted(days, reverse=True)[:60]


# ---------------- 页面 ----------------

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>盯盘执行情况</title>
<style>
:root{
  --bg:#0f1115; --fg:#e6e6e6; --dim:#8b93a0; --card:#171a21; --line:#252a34;
  --red:#e5484d; --green:#3dd68c; --yellow:#f5a623; --blue:#4a9eff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:19px;margin:0 0 4px;font-weight:600}
h2{font-size:14px;margin:26px 0 10px;color:var(--dim);font-weight:600;
  text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
  padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.kv{font-size:12px;color:var(--dim)}
.kv b{display:block;font-size:17px;color:var(--fg);font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
td.mono,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.badge{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11px;
  font-weight:600;white-space:nowrap}
.b-done{background:rgba(61,214,140,.16);color:var(--green)}
.b-failed{background:rgba(229,72,77,.16);color:var(--red)}
.b-missed{background:rgba(139,147,160,.16);color:var(--dim)}
.b-not_yet{background:rgba(74,158,255,.16);color:var(--blue)}
.b-na{background:transparent;color:var(--dim);opacity:.55}
.b-muted{background:rgba(139,147,160,.14);color:var(--dim);font-weight:400}
.b-warn{background:rgba(245,166,35,.18);color:var(--yellow)}
.b-ok{background:rgba(61,214,140,.16);color:var(--green)}
.b-err{background:rgba(229,72,77,.16);color:var(--red)}
.pill{display:inline-block;padding:1px 7px;border-radius:10px;background:var(--line);
  color:var(--dim);font-size:11px;margin-right:5px}
details{margin:2px 0}
summary{cursor:pointer;color:var(--dim);font-size:12px}
summary:hover{color:var(--fg)}
pre{background:#0b0d11;border:1px solid var(--line);border-radius:6px;
  padding:10px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word;
  margin:6px 0}
.fold{background:#0b0d11;border:1px solid var(--line);border-left:3px solid var(--yellow);
  border-radius:6px;padding:8px 10px;margin:4px 0;font-size:12px}
.fold .t{color:var(--dim);font-family:ui-monospace,monospace}
.warn{background:rgba(245,166,35,.1);border:1px solid rgba(245,166,35,.35);
  border-radius:6px;padding:8px 10px;margin-bottom:10px;font-size:12px;color:var(--yellow)}
.filters{margin:8px 0 12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
select,button{background:var(--card);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:5px 9px;font-size:12px;font-family:inherit}
button{cursor:pointer}
button:hover{border-color:var(--dim)}
.msg{font-family:ui-monospace,monospace;font-size:12px;word-break:break-word}
.tag{display:inline-block;min-width:42px;color:var(--yellow);font-weight:600}
.empty{color:var(--dim);font-size:13px;padding:14px 0}
.chip{display:inline-block;padding:2px 8px;border-radius:4px;background:var(--line);
  color:var(--dim);font-size:11px;margin:2px 4px 2px 0}
.chip.on{background:rgba(61,214,140,.16);color:var(--green)}
.err{color:var(--red)}
.ok{color:var(--green)}
footer{margin-top:36px;color:var(--dim);font-size:11px;border-top:1px solid var(--line);
  padding-top:12px}
</style></head><body><div class="wrap">

<h1>盯盘执行情况</h1>
<div class="sub" id="sub">载入中…</div>
<div id="app"></div>

<footer>
  本机只读面板 · 仅监听 <span class="mono">127.0.0.1</span> · 数据源
  <span class="mono">history/runs.jsonl</span>（执行流水）+
  <span class="mono">history/pushes.jsonl</span>（推送全文）+
  <span class="mono">watch.log</span>
  <div style="margin-top:6px">诊断（不在此处执行，避免阻塞 HTTP）：
    <span class="mono">python3 ~/.market-watch/src/selfcheck.py</span></div>
</footer>
</div>
<script>
// ⚠️ 必须**同时**转义引号。原先只转 & < >，而 esc() 也被用在
// **属性值**里（`title="${esc(raw)}"`，见下方 fired chips）——
// 属性值里的引号不转义就能闭合属性并注入新的属性/标签。
// 实测（2026-09-28，沙箱）：
//   state.json 的 fired 写入键 `x" onmouseover="alert(1)`
//   -> 渲染成 `<span class="chip on" title="x" onmouseover="alert(1)">`
//   即 onmouseover 成了**真实的事件处理器**（鼠标划过即执行）。
// raw 的来源是 state.json 的 fired[]，其键由 collect.evaluate 从
// sectors.json 的 code 拼出（`f"fund_{c}"`）—— sectors.json 是
// **用户可编辑**的（文件头明写"改此文件即时生效"），且 fired 也可能
// 被手工编辑/旧版本格式污染。所以这不是理论风险。
// 另外 `'` 也要转：属性若写成单引号形式（或未来有人改用单引号），
// 只转双引号同样会被闭合。两个都转，代价为零。
const esc = s => String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
// runlog.Status 的枚举 → 中文文案。**单一真值**：时段表里"状态列"与
// "结论列"两处都要用，原先逐字抄了两份 —— 两份漂移是这类缺陷的经典形态
// （改一份忘另一份，面板一半显示中文、一半裸显英文枚举）。而自检的
// 覆盖判据用 `findall` 会拿到两份值，`any()` 只要一份对就判绿，等于给
// 第二份开免检通道。现在只留一处定义，锚点也唯一。
// ⚠️ 键必须覆盖 runlog.Status **全部**取值：前端用法是
// `rmap[s.reason] || s.reason`，漏一个就把英文枚举原样显示给读者。
const RMAP = {no_trigger:"无触发（监控正常运行）",skip_lock:"抢锁失败，被上一轮占用",
              skip_quota:"当日配额已用尽",out_of_window:"不在交易时段",
              not_trading_day:"非交易日",error:"执行出错",ok:"已执行",
              skip_dry:"--dry / DRY_RUN 演练，人工暂停（非故障）",
              push_ok:"已推送成功",push_fail:"尝试推送但失败（拦截/超时/缺 webhook）",
              no_data:"行情取数失败，本轮跳过",
              import_fail:"依赖导入失败（进程崩在日志代码之前）",
              slot_expired:"内容已过失效时刻，按设计整条不发"};

const get = async (u) => (await fetch(u)).json();
// 面板"今天"（本地时区，YYYY-MM-DD）—— 与后端 `date` 字段同格式，
// 用于配额卡判断 rate.json 的计数日期是不是今天（跨日未轮转时提示）。
const D_TODAY = () => {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
};
const fmt = (ts) => String(ts||"").slice(5);

function badge(s){
  // `pending` = 已到点但**窗口未过**（等待/运行中）。
  // 与 `not_yet`（还没到点）同色，但语义不同：它已在该推的时间区间内。
  const m={done:"b-done",failed:"b-failed",missed:"b-missed",not_yet:"b-not_yet",
           pending:"b-not_yet",
           ok:"b-ok",push_ok:"b-ok",error:"b-err",push_fail:"b-err",
           no_trigger:"b-missed",skip_lock:"b-missed",skip_quota:"b-warn",
           skip_dry:"b-warn",import_fail:"b-err",
           // 过期未发不是故障（设计内），但也不是"无触发"——
           // 槽位确实认领了、AI 也跑了，只是跑完时内容已过期。
           slot_expired:"b-warn",
           // 时段表里的 DRY_RUN 演练。与 skip_dry 同色（黄=人工暂停，
           // 不是故障）；不给它映射会落到默认的 b-missed，看着像漏推。
           skipped:"b-warn",
           out_of_window:"b-missed",not_trading_day:"b-missed",
           "n/a":"b-na"};
  return `<span class="badge ${m[s]||"b-missed"}">${esc(s)}</span>`;
}
function dur(v){ return v==null?"—":(v<1000? v+"ms" : (v/1000).toFixed(1)+"s"); }

async function render(){
  const [sum, slots, runs, pushes, log, lc, boards] = await Promise.all([
    get("/api/summary"), get("/api/slots"), get("/api/runs"),
    get("/api/pushes"), get("/api/log"), get("/api/launchd"),
    get("/api/boards")
  ]);

  document.getElementById("sub").textContent =
    `${sum.now} · ${sum.trading_day?"交易日":"非交易日"} · 今日 ${slots.date}`;

  let h = "";

  /* ---- 1. 总体状态 ---- */
  h += `<h2>总体状态</h2><div class="card"><div class="grid">`;
  for(const j of lc.jobs){
    /* ⚠️ 设计内的非零退出（webui 的 os._exit(3) 自重启、selfcheck 的 rc=1
       结论）原先**一律显示成红色 b-err** —— 面板长期挂着一个"故障"标记，
       读者会去排查一个不存在的问题（2026-11-01 修）。 */
    const designed = Array.isArray(j.designed_rc) && j.designed_rc.includes(j.last_exit);
    const cls = j.loaded ? (j.last_exit===0?"b-ok":(designed?"b-warn":"b-err")) : "b-missed";
    h += `<div class="kv">${esc(j.label.replace("com.mison.",""))}
      <b>${j.loaded? (j.runs!=null? j.runs+" 次":"已加载") : "未加载"}
      <span class="badge ${cls}">${j.loaded? ("exit "+j.last_exit+(designed?"（设计内）":"")) : "—"}</span>
      ${j.sched? `<span class="badge ${j.sched_warn?"b-warn":"b-muted"}">${esc(j.sched)}${j.sched_warn?" ⚠ runs=0":""}</span>`:""}</b>
      <span style="font-size:11px">${esc(j.state||"")}</span></div>`;
  }
  const hl = sum.health;
  h += `<div class="kv">采集心跳
      <b class="${hl.stale?"err":""}">${esc(hl.last||"无")}</b>
      <span style="font-size:11px">${hl.age_sec!=null?
        (Math.round(hl.age_sec/60)+" 分钟前"+(hl.stale?" · 疑似停摆":"")):"—"}</span></div>`;
  h += `<div class="kv">今日推送配额
      <b>${esc(slots.count==null?"—":slots.count)} / ${esc(slots.max_push)}</b>
      <span style="font-size:11px">事件类计数（整点槽不占配额）</span></div>`;
  h += `<div class="kv">今日推送
      <b>${esc(slots.push_ok)} / ${esc(slots.push_count)}</b>
      <span style="font-size:11px">成功 / 尝试${
        slots.push_dry ? `（另有 ${esc(slots.push_dry)} 次 DRY_RUN 演练）` : ""}</span></div>`;
  const rt = sum.rate;
  h += `<div class="kv">接口配额
      <b>${esc(rt.count==null?"—":rt.count)} / ${esc(rt.cap)}</b>
      <span style="font-size:11px">每日请求上限${
        rt.date? `（计数日期 ${esc(rt.date)}）` : ""}${
        (rt.date && rt.date !== D_TODAY())? " ⚠ 非今日" : ""}</span></div>`;
  h += `<div class="kv">今日执行轮数
      <b>${esc(slots.run_count)}</b>
      <span style="font-size:11px">analyze 进程轮次</span></div>`;
  h += `</div></div>`;

  /* ---- 1b. 全市场涨跌榜 / 算法热点 ---- */
  /* 数据源与推送**同一份**（hot_cache.json），面板只读展示，不另算。
     放在「总体状态」之后、「时段完成」之前 —— 它是"此刻市场长什么样"，
     与后面的"系统跑得怎么样"是两个层次。 */
  h += `<h2>全市场板块榜 · 算法热点</h2><div class="card">`;
  {
    const bd = boards.rows || {};
    const up = bd.top_up || [], dn = bd.top_dn || [];
    const st = bd.strong || [], sg = bd.start || [];
    const ageTxt = boards.age_sec==null ? "—"
        : (boards.age_sec<90 ? boards.age_sec+" 秒前"
           : Math.round(boards.age_sec/60)+" 分钟前");
    const stale = boards.age_sec!=null && boards.age_sec>1200;
    h += `<div class="kv">榜单数据
        <b class="${stale?"err":""}">${esc(ageTxt)}</b>
        <span style="font-size:11px">候选池 ${boards.pool==null?"—":boards.pool} 个板块${
          boards.dedup==null?"":`（去重后 ${boards.dedup}）`}${
          stale?" · 疑似过期（盘中应每 10 分钟内更新）":""}</span></div>`;
    const boardRow = (r, cls) => `<span class="chip">${esc(r.name)}
        <b class="${cls}">${r.chg>=0?"+":""}${r.chg.toFixed(2)}%</b></span>`;
    if(!up.length && !dn.length && !st.length){
      h += `<div class="empty">暂无榜单数据（hot_cache.json 尚未生成，或板块接口本轮不可用）</div>`;
    } else {
      if(up.length){
        h += `<div class="kv" style="display:block">今日涨幅 TOP3
            <div style="margin-top:4px">${up.slice(0,3).map(r=>boardRow(r,"err")).join("")}</div></div>`;
      }
      if(dn.length){
        h += `<div class="kv" style="display:block">今日跌幅 TOP3
            <div style="margin-top:4px">${dn.slice(0,3).map(r=>boardRow(r,"ok")).join("")}</div></div>`;
      }
      if(st.length){
        h += `<details><summary>算法热点榜（${st.length}）· 含量比/主力占比/阶段</summary>`;
        h += `<table><tr><th>板块</th><th>涨跌</th><th>主力占比</th><th>量比</th><th>阶段</th></tr>`;
        for(const r of st){
          h += `<tr><td>${esc(r.name)}</td>
              <td class="mono ${r.chg>=0?"err":"ok"}">${r.chg>=0?"+":""}${r.chg.toFixed(2)}%</td>
              <td class="mono">${r.pct==null?"—":(r.pct>=0?"+":"")+r.pct.toFixed(1)+"%"}</td>
              <td class="mono">${r.vr==null?"—":r.vr.toFixed(1)}</td>
              <td>${esc(r.stage||"—")}</td></tr>`;
        }
        h += `</table></details>`;
      }
      if(sg.length){
        h += `<details><summary>启动板块榜（${sg.length}）· 放量+吸筹+位置低</summary>`;
        h += `<table><tr><th>板块</th><th>涨跌</th><th>主力占比</th><th>量比</th></tr>`;
        for(const r of sg){
          h += `<tr><td>${esc(r.name)}</td>
              <td class="mono ${r.chg>=0?"err":"ok"}">${r.chg>=0?"+":""}${r.chg.toFixed(2)}%</td>
              <td class="mono">${r.pct==null?"—":(r.pct>=0?"+":"")+r.pct.toFixed(1)+"%"}</td>
              <td class="mono">${r.vr==null?"—":r.vr.toFixed(1)}</td></tr>`;
        }
        h += `</table></details>`;
      }
    }
    h += `<div class="kv" style="font-size:11px;color:var(--dim)">
        涨=红 / 跌=绿（A 股惯例）。此榜与推送里「今日涨幅/跌幅 TOP3」同源。</div>`;
  }
  h += `</div>`;

  /* ---- 2. 时段完成表 ---- */
  h += `<h2>时段完成情况 · ${esc(slots.date)}</h2>`;
  if(!slots.trading_day){
    h += `<div class="warn">非交易日（休市日/周末）—— 不推送属正常。若下面执行轮数为 0，说明进程根本没被启动。</div>`;
  }
  h += `<div class="card"><table><tr><th>时段</th><th>定位</th><th>计划</th>
        <th>状态</th><th>结论 / 未推原因</th></tr>`;
  for(const s of slots.slots){
    let concl = "";
    // ⚠️ 状态是 n/a（本无此槽位）或 missed（没推成）时，**原因优先于结论**。
    // 原先无条件 `if(s.conclusion)` 在前，于是一个被判 n/a 的槽位仍会显示
    // 它"认领"到的正文 —— 状态列写着"不适用"，结论列却是一段像模像样的
    // 播报正文，读者会以为它推过。实测（2026-09-28，看 09-23 历史日）：
    //   deadline 行 status=n/a（当日非计划窗口最后一日），
    //   结论列却显示 "13:30 午后开局 · 资金未接力上午方向"（来自日志就近认领）
    // 而 missed + conclusion 的组合更危险：正文会盖住"没推成"这个事实。
    if(s.reason && (s.status === "n/a" || s.status === "missed")){
      const rmap = RMAP;
      const reason = rmap[s.reason] || s.reason;
      concl = `<span style="color:var(--dim)">${esc(reason)}</span>`;
    } else if(s.conclusion){
      const tag = s.src === "watchlog" ? `<span class="pill" title="来自 watch.log（埋点前）">log</span>` : "";
      const at  = s.at ? `<span class="mono" style="color:var(--dim)">${esc(s.at)}</span> ` : "";
      if(s.full){
        concl = `${at}${tag}<details style="display:inline"><summary>${esc(s.conclusion)}</summary><pre>${esc(s.full)}</pre></details>`;
      } else {
        concl = `${at}${tag}${esc(s.conclusion)}`;
      }
    } else if(s.reason){
      // ⚠️ 必须覆盖 runlog.Status 里的**全部**取值，否则漏掉的那个会
      // 原样显示成英文枚举（`rmap[s.reason] || s.reason` 的兜底）。
      // 实测（2026-09-28）：10:00 有 push_ok 的 end 记录、却因 pushes.jsonl
      // 无对应条目而落到 missed 分支，reason 直接显示成 "push_ok" ——
      // 给读者的解释是一串机器枚举值。原先这里只列了 8 个，缺
      // push_ok / push_fail / no_data / import_fail 四个。
      const rmap = RMAP;
      const reason = rmap[s.reason] || s.reason;
      concl = `<span style="color:var(--dim)">${esc(reason)}</span>`;
    }
    // 窗口起点早于计划时刻时，把窗口标出来 —— 否则读者会以为
    // "09:20 就推了 09:25 的播报"是调度出了偏差。
    const plan = s.window_from
      ? `<span class="mono">${esc(s.planned)}</span>
         <span style="color:var(--dim);font-size:12px">窗口 ${esc(s.window_from)}~${esc(s.window_to)}</span>`
      : `<span class="mono">${esc(s.planned)}</span>`;
    h += `<tr><td class="mono">${esc(s.key)}</td><td>${esc(s.role)}</td>
      <td>${plan}</td><td>${badge(s.status)}</td>
      <td>${concl}</td></tr>`;
  }
  h += `</table></div>`;

  h += `<div class="card"><div class="kv" style="margin-bottom:8px">
    事件类触发（13 个固定 + ${slots.events.filter(e=>e.name).length} 个 watch 板块）</div>`;
  if(slots.fired.length){
    h += `<div style="margin-bottom:8px">`;
    for(const f of slots.fired){
      const lbl = (f && typeof f === "object") ? f.label : f;
      const raw = (f && typeof f === "object") ? f.raw : f;
      h += `<span class="chip on" title="${esc(raw)}">${esc(lbl)}</span>`;
    }
    h += `</div>`;
  } else {
    h += `<div class="empty">今日尚无已触发事件</div>`;
  }
  const doneEv = slots.events.filter(e=>e.status==="done");
  if(doneEv.length){
    h += `<details open><summary>已触发的事件（${doneEv.length}）</summary>
      <table style="margin-top:6px"><tr><th>事件键</th><th>板块</th><th>触发</th><th>说明</th></tr>`;
    for(const e of doneEv){
      h += `<tr><td class="mono">${esc(e.key)}${
        e.src_key?`<br><span style="color:var(--dim);font-size:11px">${esc(e.src_key)}</span>`:""}</td>
        <td>${esc(e.name||"—")}</td>
        <td class="mono">${esc(e.at||"—")}</td><td>${esc(e.msg||"")}</td></tr>`;
    }
    h += `</table></details>`;
  }
  const pendEv = slots.events.filter(e=>e.status!=="done");
  if(pendEv.length && slots.is_today){
    h += `<details><summary>未触发的事件键（${pendEv.length}，均未达阈值）</summary>
      <div style="padding:6px 0">`;
    for(const e of pendEv) h += `<span class="chip">${esc(e.key)}${e.name?" "+esc(e.name):""}</span>`;
    h += `</div></details>`;
  }
  h += `</div>`;

  if(slots.run_reasons && Object.keys(slots.run_reasons).length){
    h += `<div class="card"><div class="kv" style="margin-bottom:6px">
      今日每轮结果分布（这是"为什么没推"的答案）</div>`;
    for(const [k,v] of Object.entries(slots.run_reasons))
      h += `<span class="chip">${badge(k)} ${v} 次</span>`;
    h += `</div>`;
  }

  /* ---- 3. 推送记录 ---- */
  h += `<h2>推送记录 · 全文可查</h2><div class="card">`;
  if(!pushes.rows.length){
    h += `<div class="empty">尚无推送记录（埋点上线前不回溯，历史推送正文只存在于 watch.log 且被截断到 50 字）</div>`;
  } else {
    for(const p of pushes.rows.slice().reverse()){
      // ⚠️ DRY_RUN 演练不能标成 FAIL。它是**人工暂停**，不是故障 ——
      // 与时段表里的 skipped / runlog 的 skip_dry 同一语义。原先一律
      // ok?OK:FAIL，于是排查推送问题时，一次演练会混在真失败里，
      // 让人以为当天多了一次投递故障。
      const isDry = p.kind==="dry";
      const cls = isDry?"b-warn":(p.ok?"b-ok":"b-err");
      const lbl = isDry?"DRY":(p.ok?"OK":"FAIL");
      h += `<details><summary>
        <span class="mono">${esc(fmt(p.t))}</span>
        <span class="badge ${cls}">${lbl}</span>
        <span class="pill">${esc(p.slot||"—")}</span>
        <span class="pill">${esc(p.kind||"")}</span>
        ${esc((p.text||"").slice(0,44))}</summary>`;
      if(p.warning) h += `<div class="warn">⚠️ 此条含被闸门拦截的原文，仅供本机排查，不要外传</div>`;
      h += `<pre>${esc(p.text)}</pre>`;
      if(p.resp) h += `<div class="kv">响应 <span class="mono">${esc(p.resp)}</span></div>`;
      if(p.note) h += `<div class="kv">备注 <span class="mono">${esc(p.note)}</span></div>`;
      h += `</details>`;
    }
  }
  h += `</div>`;

  /* ---- 4. 执行流水 ---- */
  h += `<h2>执行流水 · ${runs.total} 条</h2><div class="card">`;
  /* 慢轮吞调度：被 launchd 丢弃的轮次**自己没有流水记录**（进程没启动），
     只有上一轮的 end 记录带 `starved_slots`。这里按日汇总，让面板直接
     回答「今天丢了几轮」—— 此前要人肉翻 watch.log 的 ERR 行（P2-6）。
     ⚠️ 只统计 analyze 的 end 记录：collect 也写 run()，但吞槽位是 analyze
     单 job 的行为（launchd 不并发同一 job），把 collect 的也算进来会虚高。 */
  const starvedByDay = {};
  for(const r of runs.rows){
    if(r.script !== "analyze" || r.phase !== "end") continue;
    const n = r.starved_slots|0;
    if(!n) continue;
    const d = String(r.t||"").slice(0,10);
    if(!starvedByDay[d]) starvedByDay[d] = {n:0, rounds:0};
    starvedByDay[d].n += n;
    starvedByDay[d].rounds += 1;
  }
  const starvedDays = Object.keys(starvedByDay).sort().reverse();
  if(starvedDays.length){
    h += `<div class="kv" style="margin-bottom:8px">慢轮吞调度（被 launchd 丢弃的触发）`;
    for(const d of starvedDays.slice(0,7)){
      const s = starvedByDay[d];
      h += `<div>${esc(d)}：吞 <b>${s.n}</b> 次触发（${s.rounds} 轮超 ${esc("300")}s）</div>`;
    }
    h += `</div>`;
  }
  if(!runs.rows.length){
    h += `<div class="empty">尚无执行流水。埋点从本次上线开始记录，不回溯此前 253 次运行（那些在日志里没有痕迹）。</div>`;
  } else {
    h += `<table><tr><th>时间</th><th>脚本</th><th>阶段</th><th>结果</th>
          <th>耗时</th><th>时段/事件</th></tr>`;
    for(const r of runs.rows.slice().reverse().slice(0,220)){
      /* `starved_slots` 只在慢轮（耗时 > 调度间隔）的 analyze end 记录上出现 */
      /* ⚠️ 必须过 `esc()`（2026-10-29 修，P3）：这是全页**唯一**一处没转义的
         服务端插值。写入侧 `runlog.py` 会 `int(starved)`，但威胁模型是
         「流水文件被手工编辑/旧版本格式」—— 而那正是本文件 `esc()` 的注释
         自陈的威胁模型。一个字段漏转义就是一条注入路径，没有"反正是本地"的
         豁免。 */
      const sv = (r.script==="analyze" && r.starved_slots)
        ? `<span style="color:#c0392b"> · 吞${esc(r.starved_slots)}次</span>` : "";
      /* ⚠️ `commit_failed` 必须显示（2026-10-11 审查）：它表示「消息**已发出**
         但 fired 没落盘」—— 这是"同一条异动第二天又推了一遍"的唯一线索。
         原先只在 runs.jsonl 里，前端零引用，排查时得人肉翻 JSON。
         显示成琥珀色标记（与 push_ok 同色，因为它**不是**失败 ——
         消息确实发出去了，只是去重凭据没落盘）。 */
      const cf = r.commit_failed
        ? `<span style="color:#b7791f" title="消息已发出，但 fired 未落盘 —— 该事件当天可能重复推送"> · 落盘失败</span>` : "";
      h += `<tr><td class="mono">${esc(fmt(r.t))}</td>
        <td class="mono">${esc(r.script)}</td>
        <td class="mono">${esc(r.phase)}</td>
        <td>${badge(r.status)}</td>
        <td class="mono">${dur(r.dur_ms)}${sv}${cf}</td>
        <td class="mono">${esc(r.slot||"")}${Array.isArray(r.events)&&r.events.length?
           " · "+esc(r.events.join(",")):""}${r.note?" · "+esc(r.note).slice(0,40):""}</td></tr>`;
    }
    h += `</table>`;
  }
  h += `</div>`;

  /* ---- 5. 日志 ---- */
  const st = log.stats;
  h += `<h2>日志 · ${st.total_lines} 行</h2><div class="card">`;
  h += `<div class="kv" style="margin-bottom:8px">
    文件 ${esc(st.files.join(" + "))} · 解析 ${st.parsed} 行 · 续行 ${st.continuation} ·
    折叠掉 ${st.folded_away} 行冗余（点开可见全部）${
      log.folds_truncated>0?` · ⚠ 仅显示最近 ${log.folds.length} 组，另有 ${log.folds_truncated} 组未传输（完整内容看 watch.log）`:""}</div>`;
  h += `<div style="margin-bottom:8px">`;
  for(const [t,n] of Object.entries(st.tags).sort((a,b)=>b[1]-a[1]))
    h += `<span class="chip">${esc(t||"(无标签)")} ${n}</span>`;
  h += `</div>`;
  for(const f of log.folds.slice().reverse()){
    if(f.label && f.n>1){
      h += `<details class="fold"><summary>
        <span class="t">${esc(fmt(f.first))} → ${esc(fmt(f.last))}</span>
        <span class="badge b-warn">${esc(f.label)}</span>
        <span class="t">共 ${f.n} 条${
          f.rows_truncated?`（仅显示前 ${f.rows.length} 行，另有 ${f.rows_truncated} 行未传输）`:"（已折叠，点开看全部）"}</span></summary><pre>${
        esc(f.rows.map(r=>r.ts+" "+(r.tag?"["+r.tag+"] ":"")+r.msg).join("\n"))}</pre></details>`;
    } else {
      const r=f.row;
      h += `<div class="msg"><span class="t" style="color:var(--dim)">${esc(r.ts.slice(5))}</span> `;
      if(r.tag) h += `<span class="tag">[${esc(r.tag)}]</span> `;
      h += `${esc(r.msg)}`;
      if(r.extra && r.extra.length){
        h += ` <details style="display:inline"><summary>+${r.extra.length} 行</summary>
          <pre>${esc(r.extra.join("\n"))}</pre></details>`;
      }
      h += `</div>`;
    }
  }
  h += `</div>`;

  document.getElementById("app").innerHTML = h;
}

render().catch(e => {
  document.getElementById("app").innerHTML =
    `<div class="warn">载入失败：${esc(e.message)}</div>`;
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketWatchUI/1.0"
    # ⚠️ socket 超时（2026-10-29 修，P3）：`BaseHTTPRequestHandler` 会把这个
    # 类属性设成连接的 socket 超时。原先没有（= 阻塞到对端关闭），而
    # `ThreadingHTTPServer` **每连接一个线程**、无并发上限 —— 几个只连不发
    # 的 socket 就能长期占住线程。`[实测]` 5 个只连不发的连接让
    # `threading.active_count()` 从 2 涨到 7 并保持，关掉才回落。
    # `daemon_threads=True` 只保证退出时不挂，不限制数量。
    # 取 30s：本面板所有端点的实测耗时都远小于它（最慢的 /api/log 约 1.7s），
    # 只用来兜"连接建立了但一个字节都不发"这类。
    timeout = 30

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        try:
            raw = body.encode("utf-8") if isinstance(body, str) else body
        except Exception:
            raw = b"{}"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        # ⚠️ end_headers() 也要在 try 内（2026-10-08 审查）：客户端在响应头
        # 写出前断开（/api/log 解析 2MB 较慢，刷新即取消在途请求）时，
        # end_headers 的 flush 抛 BrokenPipeError —— 原实现只包住了
        # wfile.write，异常冒到 do_GET 兜底 except 又调一次 _send(500)，
        # socket 已断二次抛 -> socketserver.handle_error 把整段 traceback
        # 打进 webui.err（实测累积 109KB / 64 条，且无人轮转）。
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        # ⚠️ Host 头校验（2026-11-01 修，P3）：DNS rebinding 下，外部页面
        # 可以把某个域名解析到 127.0.0.1 再 fetch 本面板 —— 而面板响应**不带
        # CORS 头**，浏览器仍会**发出**请求（简单请求），攻击者读不到响应体，
        # 但**写请求与副作用**照样发生（本面板只读，故危害限于信息探测）。
        # 真正的风险在**将来**：任何一次加 POST/加写端点，这个缺口就变成
        # 可写。现在就钉死：只接受本机 Host（含端口/显式 MW_HOST）。
        # `[实测]` 修复前 `curl -H 'Host: evil.example'` 返回 200。
        _host_hdr = (self.headers.get("Host") or "").strip()
        _host_name = _host_hdr.rsplit(":", 1)[0].strip("[]").lower()
        if _host_name not in ("127.0.0.1", "localhost", "::1",
                             HOST.lower(), "[::1]"):
            return self._send(403, json.dumps(
                {"error": "bad host", "k": "E403"}))
        try:
            if path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path.startswith("/api/"):
                try:
                    data = api(path, q)
                except BadDate as _bd:
                    # 参数**给了但非法** → 400（与"没给"即不过滤区分开，
                    # 否则 `?date=xyz` 会静默返回全部历史行）。
                    return self._send(400, json.dumps(
                        {"error": "bad date", "k": "E400",
                         "date": str(_bd)}))
                if data is None:
                    return self._send(404, json.dumps({"error": "not found"}))
                # ⚠️ `_jsonable()` + `allow_nan=False`（2026-10-29 / 2026-11-01）：
                # `allow_nan=False` 会把 `nan`/`inf` 写成裸 `NaN`/`Infinity`
                # —— 那是**非法 JSON**，浏览器 `JSON.parse` 抛
                # `Unexpected token 'N'` → 前端 `Promise.all` 整页只剩
                # 「载入失败」。`_jsonable()` 先把非有限浮点**递归降级成
                # None**（与逐字段守卫同语义："当没有"），剩下真正不可
                # 序列化的类型才由 allow_nan=False/这里抛出（外层 except
                # 译成 500 + 固定文案）。两道合起来：脏数据不再整页白，
                # 真缺陷仍然显式。
                return self._send(200, json.dumps(
                    _jsonable(data), ensure_ascii=False, allow_nan=False))
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            # ⚠️ 异常详情**只进日志、不进响应**（2026-10-11 审查）：原先把
            # `f"{type(e).__name__}: {e}"` 直接回给调用方 —— 而本面板虽只绑
            # 127.0.0.1，异常消息里可能带**绝对路径**（如
            # `open("~/.market-watch/pending.json")`）、模块名、甚至配置片段。
            # 同机任何本地进程/浏览器标签都能打这个端口，等于把内部路径
            # 结构免费送出去（信息收集的第一步）。
            # 响应只给一个固定文案 + 一个可 grep 的标记；详情落 webui.err。
            try:
                import traceback as _tb500
                # ⚠️ 2026-11-05（P2）：os.open 一步定 0600 —— 原 `open("a")`
                # 在 umask 022 下把 webui.err 建成 0644（[状态文件权限]
                # 扫描的正是 .err 扩展名，靠 DIR 是 700 才没泄漏）。
                import os as _os500
                _fd500 = _os500.open(p_log("webui.err"),
                                     _os500.O_WRONLY | _os500.O_CREAT
                                     | _os500.O_APPEND, 0o600)
                with _os500.fdopen(_fd500, "a", encoding="utf-8") as _f500:
                    _f500.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}]"
                                f" 500 {path}: {type(e).__name__}: {e}\n"
                                + _tb500.format_exc()[-800:] + "\n")
            except Exception:
                pass
            return self._send(500, json.dumps(
                {"error": "internal error", "k": "E500"}))

    # 只读：任何写方法一律 405
    def _readonly(self):
        return self._send(405, json.dumps({"error": "read-only"}),
                          extra={"Allow": "GET, HEAD"})

    def do_HEAD(self):
        """HEAD 走与 GET 完全相同的路径（含 404 / 500 与 Content-Length），
        只是 _send 里按 self.command 跳过响应体 —— 那个分支正是为它写的。

        ⚠️ 不定义这个方法时，BaseHTTPRequestHandler 基类没有 do_HEAD，
        请求会得到 **501 Unsupported method**。而 _readonly 返回的
        `Allow: GET, HEAD` 声明了支持 HEAD —— 声明与实现不一致。
        实测（2026-09-26）：HEAD /api/health -> 501。
        """
        self.do_GET()

    do_POST = do_PUT = do_DELETE = do_PATCH = _readonly

    def log_message(self, fmt, *args):
        pass          # 不污染 stdout；launchd 下也没人看


def _assert_readonly():
    """静态断言：webui 必须是只读的（防以后被改坏）。

    ⚠️ 这个检查此前**从未生效过**。原实现在同一个 `ast.walk` 循环里既查
    FunctionDef 又查 Assign，而遇到那个赋值时直接 `return` —— 于是**永远
    走不到 FunctionDef 分支**，任何真正的 do_POST 定义都会被静默放行。
    实测（2026-09-26）：往 `do_POST = _readonly` 之后追加一个真正的
    `def do_POST(self)`，服务照常启动并对外提供写接口，`_assert_readonly`
    一声不吭。

    正确语义分两步，且**必须各走一遍完整的树**：
      ① 任何位置出现 banned 名字的 FunctionDef/AsyncFunctionDef -> 报错。
         （这是真正的风险：一个可用的写处理器）
      ② 出现 banned 名字的赋值 -> 只允许赋成 `_readonly`；
         赋成别的（如 `do_POST = self._do_write`）同样要拦下。
    """
    import ast
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    banned = {"do_POST", "do_PUT", "do_DELETE", "do_PATCH"}
    tree = ast.parse(src)
    # ① 先扫**全部**函数定义
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in banned:
                raise SystemExit(f"webui 必须是只读的，但发现了 {node.name} 的定义")
    # ② 再扫**全部**赋值：只允许 `do_XXX = _readonly`
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in banned:
                    v = node.value
                    if not (isinstance(v, ast.Name) and v.id == "_readonly"):
                        raise SystemExit(
                            f"webui 必须是只读的，但 {t.id} 被赋成了 "
                            f"{getattr(v, 'id', type(v).__name__)}（只允许 = _readonly）")
    return


def _mtime(p):
    """文件 mtime，取不到返回 0。"""
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0


def _sources_sane():
    """源码是否全部可解析。**启动前的自检**。

    ⚠️ 为什么必须有这道检查：本服务配的是 KeepAlive={SuccessfulExit:false}，
    即**任何非 0 退出都会重启**。若源码有语法错误，进程启动即失败，
    launchd 会立刻拉起、再失败、再拉起 —— 无节流地死循环。
    实测（2026-09-24）故意写入一行语法错误，12 秒内 runs 从 0 涨到 4。

    更糟的是：崩溃循环期间端口是空的，面板完全不可用；而用户改代码时
    出现语法错误是**很常见**的中间状态。

    故：启动时先解析全部被监视的源码，任一不可解析就**正常退出(0)** ——
    0 不触发重启，保留上一次可用状态，等源码修好后再由外部拉起。
    """
    import ast as _ast
    # ⚠️ 这份列表**故意不含 analyze.py**（与下面 _watch_source 的列表不同）。
    # 两者的目的相反：
    #   · 这里（防崩溃循环）—— analyze.py 语法错误时，webui 靠
    #     _analyze_consts() 的异常分支回退到兜底副本，**仍能正常启动**。
    #     若把它列进来，webui 会直接退出不启动，反而废掉了那个回退机制。
    #   · _watch_source（热重载）—— analyze.py 是 webui 的依赖
    #     （常量来源），改了必须重启才能读到新值，所以**必须**列进去。
    # ⚠️ `paths.py` 必须在列表里（2026-11-01 补）：它是本模块的**模块级硬依赖**。
    # 模块顶部已用等待循环拦住"启动时坏掉"；这里是**运行中**被改坏的情形 ——
    # 返回 False 才能让 _watch_source 选择"不退出、继续用旧代码服务"，
    # 而不是杀掉最后一个可用进程去等一个起不来的新进程。
    for f in ("webui.py", "paths.py", "marketlib.py", "runlog.py"):
        fp = os.path.join(MAIN_ROOT, f)
        try:
            with open(fp, encoding="utf-8") as fh:
                _ast.parse(fh.read())
        except FileNotFoundError:
            continue
        except SyntaxError as e:
            print(f"❌ {f} 语法错误（第 {e.lineno} 行），不启动以免崩溃循环: {e.msg}",
                  flush=True)
            return False
        except Exception as e:
            print(f"❌ {f} 无法读取: {e}", flush=True)
            return False
    return True


def _rotate_out(*names):
    """单代轮转 launchd 重定向日志 —— **copy + 原地截断**，不是 `os.replace`。

    ⚠️ 为什么必须自己轮转：`webui.out`/`webui.err` 是 launchd 的
    StandardOutPath，**没有任何消费者、也没有任何轮转**（`marketlib._rotate_log`
    只管 `watch.log`）。而本进程的「源码变更自重启」每次都会打两行，
    实测 2026-10-01 已累积 **1081 行 / 55KB**，其中 363 行是
    「数据目录: …」这种纯重复 —— 内容里真正有价值的那条
    （`❌ webui.py 语法错误（第 1213 行）…`）被埋在 770 行之后。
    不轮转的话它会无界增长，且**排查时先被噪音淹没**。

    ⚠️⚠️ **2026-10-09 修正：原先用 `os.replace`，那是无效轮转。**
    launchd 在 exec 之前就打开这些文件并把 fd 交给子进程（探针实测其
    flags 为 `O_RDWR|O_APPEND`）。`os.replace` 只改目录项，**fd 仍指向
    原 inode** → 轮转后所有后续输出**继续写进 `.1`**，而 `.out` 这个
    路径**整个消失**。既没腾出空间，又把正在看的日志藏了起来。
    `[实测]` 复刻 launchd 语义（O_APPEND fd + rename）：
        os.replace      → base 不存在，gen1 = 旧+新          ❌
        copy+truncate   → base = 只有新内容，gen1 = 旧内容    ✅
    正确做法：先 copy 到 `.1`，再**原地截断**（inode 不变 → fd 继续有效）。
    ⚠️ 顺序必须"先 copy 后 truncate"：反过来在两步之间被 kill 就丢数据；
    这个顺序最坏只是两处内容重复，不丢。

    ⚠️ 路径用 `DIR` 而非源码目录：日志属于**数据目录**。
    ⚠️ 不 import marketlib（本模块刻意只读、不依赖主链路），这里自带
    等价实现；`marketlib.rotate_logs` 是同算法的另一份。
    """
    import shutil as _sh
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    for n in names:
        p = os.path.join(LOGS_DIR, n)
        try:
            if not (os.path.exists(p) and os.path.getsize(p) > 512 * 1024):
                continue
            _sh.copyfile(p, p + ".1")
            _harden_logs(p + ".1")          # copyfile 按 umask 建 → 得自己收紧
            with open(p, "r+b") as f:
                f.truncate(0)
        except Exception:
            pass
    for _fd in (1, 2):
        try:
            os.lseek(_fd, 0, os.SEEK_END)
        except Exception:
            pass


class BoundedHTTPServer(ThreadingHTTPServer):
    """带**并发上限**的 ThreadingHTTPServer（2026-11-01 加，P3）。

    ⚠️ 为什么需要：`ThreadingHTTPServer` 每连接一线程、**无上限**，而
    `timeout=30` 是**每次 recv** 的超时 —— 慢速滴漏的连接可以无限保持。
    `[实测]` 300 个空连接让 `threading.active_count()` 到 302（释放后回落 2）。
    本面板是本地只读工具，正常并发是个位数；上限取 32（余量足够），
    超限直接关闭该连接（不发响应）—— 比拖垮整个面板好。

    ⚠️ 计数必须与线程生命周期配对：`process_request` 里 +1、
    `shutdown_request` 里 -1（它在工作线程末尾被调用，成功/异常路径都会走）。
    用锁保护（多线程）。
    """
    MAXCONN = 32

    def __init__(self, *a, **kw):
        self._cap_lock = threading.Lock()
        self._active = 0
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        with self._cap_lock:
            if self._active >= self.MAXCONN:
                _over = True
            else:
                self._active += 1
                _over = False
        if _over:
            try:
                request.close()
            except Exception:
                pass
            return
        # ⚠️ 这里**不再**用 try/except 自减（2026-11-02 修，P3）：
        # `socketserver.BaseServer._handle_request_noblock` 在
        # `process_request` 抛异常后会先 `handle_error` 再 `shutdown_request`
        # —— 而计数 -1 就挂在 `shutdown_request` 里。若此处再减一次，
        # 一次请求（Thread.start() 失败时，如线程数耗尽）会扣两格，
        # 计数永久偏低、并发上限被静默放大。正常路径的 -1 由工作线程
        # 的 finally 调 `shutdown_request` 完成，两条路径都只减一次。
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            with self._cap_lock:
                self._active = max(0, self._active - 1)


def _harden_logs(*names):
    """把 launchd 重定向出来的日志文件收紧到 600。

    ⚠️ launchd 以 umask 022 **重建** StandardOutPath/StandardErrorPath →
    每次启动都是 644，所以部署时 chmod 一次不顶用，必须在入口收敛一次
    （2026-10-08 安全审查 P2）。

    ⚠️ 不 import marketlib —— 本模块刻意不依赖它（只读面板，见文件头），
    这里自带等价实现。**函数名与 marketlib.harden_logs 保持同形**
    （`harden_logs("a","b")`）是有意的：自检的 [定时任务] 段会拿
    plist 的 StandardOutPath 与各脚本 `harden_logs(...)` 的实参做交叉
    对账，形状一致才能用同一条判据核。改名会让那份对账失效。
    """
    for _n in names:
        try:
            _p = os.path.join(LOGS_DIR, _n)
            if os.path.exists(_p):
                os.chmod(_p, 0o600)
        except Exception:
            pass


def main():
    _assert_readonly()
    _harden_logs("webui.out", "webui.err")
    _rotate_out("webui.out", "webui.err")
    # 语法自检必须在最前：失败就**等待重试**，不退出。
    #
    # ⚠️⚠️ 原先写 `if not _sources_sane(): return 0`（2026-10-30 修，P2）——
    # 那是**永久死亡**：plist 是 `KeepAlive{SuccessfulExit:false}`，
    # 退出码 0 **不会**被 launchd 拉起。于是源码里留一个语法错误
    # （改代码改到一半、编辑器写了半截）就够让面板**该死到你手动
    # launchctl kickstart 为止**，而期间不会有任何自动恢复路径。
    # 「防崩溃循环」的初衷是对的，但退 0 把"短暂不可用"变成了"永久不可用"。
    #
    # 改成**原地等待**：进程活着（launchd 不会拉起第二个）、每 30s 重探一次，
    # 源码一修好就继续正常启动。既不会崩溃循环，也不会永久死亡。
    # 重复打印做去重：只在**状态变化**时打一行，避免日志被 30s 一条刷满。
    _last_report = None
    while not _sources_sane():
        if _last_report != "waiting":
            print("源码暂不可解析 —— 不退出，每 30s 重试（修好后自动启动）",
                  flush=True)
            _last_report = "waiting"
        time.sleep(30)
    print(f"盯盘执行情况 WebUI → http://{HOST}:{PORT}/", flush=True)
    print(f"数据目录: {DIR}", flush=True)
    # ⚠️ 2026-11-05（P2）：面板**没有鉴权**，只靠"绑在本机"这层保护。
    # 若有人图方便把 MW_HOST 改成 0.0.0.0 / 局域网 IP（远程看面板），
    # 同网的任何人都能读全部历史、日志与持仓口径的推送。这不是禁止 ——
    # 是把它**显式说出来**，别让"改一个环境变量"悄悄扩大信任边界。
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️ MW_HOST={HOST!r} 不是回环地址 —— 面板无鉴权，"
              f"绑定到非本机接口后，能访问该地址的任何人都可读全部数据。"
              f"如非明确需要，请改回 127.0.0.1。", flush=True)
    try:
        # ⚠ 2026-11-10（Codex P2）：`ThreadingHTTPServer` 继承 `AF_INET`，
        # 对 `::1` 这类 IPv6 回环地址会 `gaierror (errno 8)`。
        # 按地址**形状**选协议族。
        _srv_cls = BoundedHTTPServer
        if ":" in HOST:
            class _V6Server(BoundedHTTPServer):
                address_family = socket.AF_INET6
            _srv_cls = _V6Server
        srv = _srv_cls((HOST, PORT), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            # ⚠️ **原地等待，不退出**（2026-11-01 修，P3）。
            # 原先 `return 0` —— 而 plist 是 `KeepAlive{SuccessfulExit:false}`，
            # 退出码 0 **不会**被 launchd 拉起：端口一旦被别的进程占住
            # （或上一次的实例还没退干净），面板就**永久死亡**，只能人工
            # `launchctl kickstart`。这与本文件上面刚修过的「语法错误退 0」
            # 是同一个坑的第二个入口（那处已改成原地等待，这处漏了）。
            # 改为每 30s 重试 `BoundedHTTPServer(...)`：端口一空出来就继续
            # 正常启动；进程始终活着，launchd 不会拉起第二个。
            msg = (f"端口 {PORT} 已被占用。可能已有一个实例在跑；"
                   f"用 MW_PORT 换端口后重试。")
            print(msg + " —— 不退出，每 30s 重试绑定", flush=True)
            try:
                # 同上一处：os.open 一步定 0600（umask 022 下 open("a") 建 644）
                import os as _os_pe
                _fd_pe = _os_pe.open(p_log("webui.err"),
                                     _os_pe.O_WRONLY | _os_pe.O_CREAT
                                     | _os_pe.O_APPEND, 0o600)
                with _os_pe.fdopen(_fd_pe, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
            except Exception:
                pass
            while True:
                time.sleep(30)
                try:
                    srv = _srv_cls((HOST, PORT), Handler)
                    print(f"端口 {PORT} 已可用，继续启动", flush=True)
                    break
                except OSError as _e2:
                    if _e2.errno != errno.EADDRINUSE:
                        raise
        else:
            raise
    srv.daemon_threads = True

    # ---------- 源码变更自重启 ----------
    # 本服务是常驻进程，KeepAlive={SuccessfulExit:false}：**成功退出（0）不拉起、
    # 非 0/信号才拉起**（2026-10-08 更正：本文件原注释写 Crashed:true，
    # 与 plist 实际配置不符，且二者语义不同 —— Crashed 只认信号崩溃，
    # os._exit(3) 在 Crashed 下**不会**重启，照注释改 plist 会废掉自重启）——
    # 改了 webui.py 后旧进程会一直跑旧代码，面板上看到的不是最新逻辑。
    # 实测 2026-09-24：进程 10:24 启动，而 webui.py 11:33 之后改过两次，
    # 期间面板一直显示旧行为（deadline/summary 重复行、非 deadline 日误报
    # missed 都没修）。这类"改了但没生效"极难察觉 —— 页面看起来一切正常。
    #
    # 故让进程自己盯源码 mtime：发现比启动时新就**主动退出**，
    # launchd 见非 0 退出码判为崩溃，立刻拉起新代码。
    # 退出码用 3（而非 0）是关键：0 是 SuccessfulExit，
    # 配合 KeepAlive{SuccessfulExit:false} 不会重启。
    # ⚠️ analyze.py 也必须在监视列表里：webui 现在**静态读取 analyze 的
    # 常量**（SLOTS/SLOT_TTL，见 _analyze_consts），所以它已经是 webui 的
    # 依赖。若不监视，改完 analyze 后 webui 会一直用启动时读到的旧值 ——
    # 那正是"副本漂移"换个形式重新出现（实测 2026-09-24 漏了这项）。
    _srcs = {f: _mtime(os.path.join(MAIN_ROOT, f))
             for f in ("webui.py", "paths.py", "analyze.py", "marketlib.py",
                       "runlog.py")}

    def _watch_source():
        while True:
            time.sleep(5)
            try:
                for f, t0 in _srcs.items():
                    t1 = _mtime(os.path.join(MAIN_ROOT, f))
                    if t1 and t0 and t1 > t0:
                        # ⚠️ 退出**前**必须先确认新代码可解析。
                        # _sources_sane() 的 docstring 说它"保留上一次可用状态"，
                        # 但那只有在**旧进程还活着**时才成立 —— 而这里正是要杀掉
                        # 旧进程。若新代码有语法错误（用户编辑到一半的常见状态），
                        # 时序是：旧进程被杀 -> 新进程 _sources_sane() 失败 ->
                        # main() 返回 0 -> launchd 视为正常结束 ->
                        # KeepAlive{SuccessfulExit:false} **不重启** -> 面板永久死亡，
                        # 且没有任何自动恢复路径。实测复现。
                        # 这里先自检：不通过就**不退出**，继续用旧代码服务，
                        # 每 5 秒重试一次，等源码修好再平滑重启。
                        if not _sources_sane():
                            print(f"{f} 已更新但源码不可解析，暂不重启"
                                  f"（继续用当前版本服务，稍后重试）", flush=True)
                            _srcs[f] = t1      # 记账，避免每轮重复打印
                            continue
                        print(f"{f} 已更新，退出以让 launchd 拉起新代码", flush=True)
                        os._exit(3)     # 非 0：触发 KeepAlive{SuccessfulExit:false} 拉起
            except Exception:
                pass

    try:
        threading.Thread(target=_watch_source, daemon=True).start()
    except Exception:
        pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
