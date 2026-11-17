#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据采集与阈值判断（纯采集，不推送）。
把快照和触发事件写入 pending.json，供 analyze/webui 读取。
运行节拍见下方 SCHED_INTERVAL —— 单一真值，plist 与各处阈值都由它推导。
"""
import os, datetime, sys, json, time
from typing import Any, TypedDict

# 采集节拍（秒），必须与 launchd plist 的 StartInterval 一致（默认每 300s）。
#
# ⚠️ 这是**单一真值**：selfcheck 的调度检查、selfcheck/webui 的采集新鲜度
# 阈值都从这里推导。原先它们各自硬编码 120 / 600，改 plist 后两处会静默
# 失配 —— 自检会报「调度节拍变了」，而新鲜度阈值仍按旧节拍留余量。
# 与 analyze.SCHED_INTERVAL 同一模式（那边已是单一真值）。
#
# 历史：原为 120s。2026-09-30 放宽到 300s —— 实测推送正文引用的参照时刻
# 距发送中位 29 分钟、最大 39 分钟，而旧 hist 窗口 12 条 × 120s 仅 24 分钟，
# 参照点已超出窗口；同时「较上一快照」类表述出现 0 次，说明 2 分钟粒度
# 从未被使用。放宽后 hist 窗口改为 24 条 × 300s = 120 分钟，覆盖需求且
# 每日取数轮次由 140 降到 56（少发约 336 个请求）。
SCHED_INTERVAL = 300

# 采集新鲜度阈值（秒）：盘中超过它没更新才算「疑似停摆」。
# 取 3× 节拍 = 900s —— 容忍**连续两轮**被跳过。这是有意留的余量：
# launchd 不并发运行同一 job，单轮耗时超过节拍时会静默跳过后续触发
# （实测一次 675s 的慢轮吞掉 2 轮），按 2× 会在一次慢轮后就误报停摆。
FRESH_LIMIT_IN_SESSION = SCHED_INTERVAL * 3

# 允许 MW_DIR 覆盖：测试可在沙箱里跑而完全不碰线上状态文件
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/
try:
    from paths import DIR  # noqa: E402  （路径单一真值源，见 src/paths.py）
    from paths import p_state, p_log, p_hist
    from paths import LAUNCHD_PREFIX as _LD_PREFIX  # noqa: E402  （标签前缀可配置）
except Exception as _paths_e:      # noqa: BLE001
    # ⚠️ 2026-11-01 修（P2）同 analyze.py：paths.py 不能拖死入口，
    # 否则采集停摆而 logs/history 零留痕。回退到本地最小路径推导。
    DIR = os.environ.get("MW_DIR") or os.path.expanduser("~/.market-watch")
    _LD_PREFIX = os.environ.get("MW_LAUNCHD_PREFIX", "com.mison.market")

    def p_state(_n):
        return os.path.join(DIR, "state", _n)

    def p_log(_n):
        return os.path.join(DIR, "logs", _n)

    def p_hist(_n):
        return os.path.join(DIR, "state", "history", _n)

    print(f"paths.py 导入失败（{type(_paths_e).__name__}: {_paths_e}）"
          f"—— 已回退到本地路径推导", file=sys.stderr)
def _rotate_log_bare(path, max_bytes=512 * 1024):
    """**不依赖 marketlib** 的单代轮转 —— 供"依赖导入失败"的兜底路径使用。

    ⚠️ 为什么需要一份裸实现（2026-10-29 修，P3）：`analyze.py` / `collect.py`
    的 import 兜底直写日志（collect.log），而**那一刻
    `marketlib` 恰恰是导入失败的那一个** —— `M._rotate_log()` 用不了。
    于是这条路径**永不轮转**：只要依赖一直坏着（如 `marketlib.py` 语法
    错误未修），每轮追加 2 行 + 一段 traceback，而 collect 每 5 分钟一轮
    ≈ 288 次/天 —— 真出持续故障时它会一路涨下去，**没有任何清理机制**。

    ⚠️ 这里用 `os.replace` 是**对的**（与 `marketlib._rotate_log` 同理）：
    本路径的写者每轮 `os.open(..., O_APPEND)` **新开 fd**，rename 之后
    下一次写入自然落到新文件 —— 与 launchd 那种"fd 长期持有"的
    `*.out/*.err` 完全不同（那三份必须 copy+truncate，见 `rotate_logs`
    的长注释）。区别在 **fd 的寿命**，不要统一成一种。

    ⚠️ 任何异常都吞掉：这是一条**日志兜底**路径，为轮转而让"记不上日志"
    本末倒置（记不上才是真问题）。
    """
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except Exception:
        pass


# 依赖导入的失败兜底 —— 与 analyze.py 同构。
# marketlib 若有语法错误，这里会崩在任何日志代码之前，导致采集静默停摆：
# watch.log / collect.log / runs.jsonl 全部零留痕，而界面看起来一切正常。
try:
    from marketlib import (TH, HOLIDAYS, quotes as _quotes,
                           trend as _trend, news as _news, log as _log,
                           in_guard,
                           hstech_up_threshold, watch_secids,
                           index_secids, hk_secids, threshold, _cfg_items)
    import marketlib as M   # 原子读写助手 _atomic_write_json / _load_json
    import runlog as _rl    # 执行流水埋点（history/runs.jsonl）
except Exception as _imp_e:
    import traceback as _tb
    _msg = f"{type(_imp_e).__name__}: {_imp_e}"
    try:
        # ⚠️ 必须 os.open(..., 0o600)：`open(..., "a")` 新建文件时权限由
        # **umask** 决定，`[实测]` umask 022 下产出 0o644。
        # 与 analyze.py 同一处的写法对齐 —— 2026-10-08 那轮把主链路的三处
        # 日志创建改成了 os.open，**漏了这条导入失败路径**（此时
        # `import marketlib` 还没成功，用不了 M.log()，只能裸写文件）。
        #
        # 严重性**不是**"正在泄漏"：`.market-watch/` 是 drwx------（700），
        # 别的本地用户进不来，644 的文件他们也读不到。这是**纵深防御** ——
        # 万一哪天目录被放宽（改成 755），文件自身的 600 还得兜住。
        # ⚠️ 另：`os.open` 的 mode 参数**只在创建那一刻**生效 —— 已经存在的
        # 644 文件不会被它改回来，只能显式 chmod（2026-10-09 已做过一次）。
        # ⚠️ 先轮转（2026-10-29）：marketlib 正是导入失败的那个，
        # `M.rotate_logs` 用不了，走本文件自带的裸实现。
        _rotate_log_bare(p_log("collect.log"))
        _fd_l = os.open(p_log("collect.log"),
                        os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(_fd_l, "a", encoding="utf-8") as _f:
            _f.write(f"[{datetime.datetime.now():%m-%d %H:%M:%S}][ERR] "
                     f"依赖导入失败，本轮未执行: {_msg}\n")
            _f.write(_tb.format_exc()[-600:] + "\n")
    except Exception:
        pass
    try:
        # ⚠️ 同样 os.open(..., 0o600)：open("a") 新建走 umask → 644，
        # 而 runs.jsonl 里带 note（异常消息）。正常路径由 runlog._append
        # 以 600 创建，但**这条路是"runlog 还没 import 成功"时的兜底** ——
        # 若 runs.jsonl 恰好不存在，这里就会把它建造成 644。
        _rn = p_hist("runs.jsonl")
        # ⚠ 2026-11-10（Codex P3）：这条路径**绕过了 runlog 的 4MiB 截尾**。
        # 实测：预先填到 4,414,990 字节（已超阈值）后注入 marketlib
        # 导入失败，文件仍被写到 4,415,175 字节 ——
        # 而正常路径（`runlog._append`）会把它截到 60%。
        # 导入失败可能连续发生（每 5 分钟一次），文件会无限长。
        # 这里不能 import runlog（本路径的前提就是它可能导不进来），
        # 故做一次最小截尾：超阈就丢掉前 40%。
        try:
            if os.path.getsize(_rn) > 4 * 1024 * 1024:
                with open(_rn, "r+", encoding="utf-8", errors="replace") as _tf:
                    _all = _tf.readlines()
                    _keep = _all[len(_all) // 2:]
                    _tf.seek(0)
                    _tf.writelines(_keep)
                    _tf.truncate()
        except Exception:
            pass
        _fd_r = os.open(_rn,
                        os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(_fd_r, "a", encoding="utf-8") as _f:
            _f.write(json.dumps({
                "ts": time.time(),
                "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "script": "collect", "phase": "end", "status": "import_fail",
                "note": _msg, "pid": os.getpid()}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    sys.exit(1)

OUT = p_state("pending.json")
# ⚠️ 这里原先还有一个 `LOG = p_log("collect.log")`，2026-10-11 删除：
# 全库零引用（AST 核实），而它指向的文件**真实存在**（510B，10-08 16:01 那次
# marketlib 语法错误的留痕）—— 一个"定义了但没人写"的路径常量，只会让后来者
# 以为 collect.log 是活路径。真正的日志统一走 watch.log（见 _log 的注释）。





# ---- 阈值 ----
# ⚠️ 指数异动阈值**必须**走 M.idx_thresholds()，不能用固定表 TH["idx_3sigma"]。
# 原先这里读固定 3σ 表，而 alert.check_index 用动态 EWMA 3σ ——
# 两条通道对同一指数门槛不同，且方向是反的（推送门槛低于快照门槛）：
#   上证指数  推送 2.25% / 快照 2.70%
#   恒生科技  推送 3.94% / 快照 5.30%
# 于是上证 -2.40% 这种行情：**推送到群了，面板事件区却查不到**。
# idx_thresholds() 与 alert 同源，且 σ 直接读 dcache（alert 早已算好并落盘），
# 不增加任何网络请求。
# ⚠️ 必须是**函数**而不是模块级常量。
# 原先写 `IDX3S = M.idx_thresholds()`，在 import 那一刻求值并冻结 ——
# 而 idx_thresholds() 依赖 alert 落盘的 σ（dcache）。launchd 每天首次
# collect 跑在 alert 之前，那时 daily.json 还是**昨天**的日期，
# dcache_get 一律返回 None，于是 IDX3S 冻结在静态 σ 上：
#   上证指数 冻结 2.70% / alert 实算 2.36%
# 后果与"推送/快照阈值分叉"完全一样 —— [2.36%, 2.70%) 区间内
# 推送到群、快照不记，面板事件区查不到。实测（沙箱，清空 σ 缓存后）：
#   collect.IDX3S['1.000001'] = 2.70%   冻结不变
#   M.idx_thresholds()['1.000001'] = 2.36%  实时
# selfcheck 查不出来：它在 import 时刻比较两边快照，冷缓存下两边
# 都用静态值，仍然"一致"。改成每次求值即可，开销是一次 dcache 读。
def IDX3S():
    return M.idx_thresholds()

def _q(q, key, *alts):
    """按 secid 取值，兼容短码。

    ⚠️ quotes() 建键用的是**请求时的完整 secid**（"124.HSTECH"、"1.000001"），
    只在末尾把"短码 → 完整 secid"补一份别名，**没有反向补**。所以本文件里
    原先那些 q.get("HSTECH") / q.get("000001") 全部返回 None —— 而且不报错、
    不写日志，两个分支静默失效了整整一个生命周期：

      · q.get("HSTECH")  → 恒生科技的上方触发线判定**从未生效**
      · IDX3S 循环的键全是短码 → 7 个指数的 3σ 异动判定 **0/7 命中**

    证据：2026-09-24 全天 pending.json 只产出 fund_* 与 storage_fade，
    hstech_up / hstech_dn / idx_* 一个都没有。
    （同类 bug 在 analyze.build_brief 修过一次，这里是同一根因的另一处。）
    """
    # ⚠️ 返回值必须是 dict 或 None —— 调用方一律写成
    # `x = _q(q, ...)` 然后 `x.get("f2")`，拿到非 dict 会 AttributeError。
    # 而 q 是**外部取数结果**（quotes() → 多主机 + 腾讯 + 备用源 + 降级链），
    # 其 docstring 只承诺"返回 {代码: row}"，并未承诺每个 row 都是 dict。
    # 实测（2026-09-28）：q={"124.HSTECH":"坏"} -> evaluate() 抛
    # AttributeError: 'str' object has no attribute 'get'；q 值给 list 同样抛。
    # 后果：evaluate() 被 _main **裸调用**（无内层 try），异常冒到 main()
    # 的兜底 except，整轮采集记成 "error"，pending.json 一条都写不出、
    # hist 也不更新 —— 与上面 f3/last/open 那几处守卫要防的是同一件事。
    #
    # 可达性（实测）：三条产出路径当前都构造 dict 字面量 ——
    #   _fetch → _valid_quotes 会**真的剔除**非 dict 项（其 docstring 与实现
    #   已对齐）；feeds.backup_quotes 用 dict 字面量；_hk_quotes_via_tencent
    #   同样。所以当前**不可达**。
    # 但本文件对 f2/f3/f14/f184/last/open 都做了逐字段守卫，唯独"值本身是不是
    # dict"漏了 —— 同一函数内两套标准。且降级链任一环换成外部实现（backup_quotes
    # 在本模块之外、可被替换）就会破。守卫在**唯一的取值入口**上，
    # 一处覆盖全部调用点，比在每个 `x.get` 前各写一遍更省也更难漏。
    # ⚠️ q 本身必须**先**验类型：`key in q` / `q[key]` 在 q=None 时抛
    # TypeError、q=list 时 `"x" in [1]` 虽不抛但语义全错。实测：
    #   _q(None, "1.000001") -> TypeError: argument of type 'NoneType'
    #   _q([1],  "1.000001") -> 原样返回 None（但 evaluate 的 q.get 仍会炸）
    # quotes() 的契约是返回 dict，但 _q 是防御层，不该假设调用方一定守约。
    if not isinstance(q, dict):
        return None

    # ⚠️ 内层函数必须标注 `object -> dict[str, Any] | None`。
    # 不标注时 `_ok` 推成 `(Unknown) -> dict[Unknown, Unknown] | None`，
    # Unknown 会顺着返回值扩散到 evaluate() 里每一处 x.get(...)，实测
    # **+5 条诊断**（basedpyright，同一目录 A/B：181 -> 186 warnings）。
    # 这与 `RateLimiter._load_host_fail` 里"把 out 标注成 dict[str, Any]
    # 净减 8 条"是同一个机制 —— 局部变量的类型是 Unknown 扩散的源头。
    def _ok(v: object) -> dict[str, Any] | None:
        return v if isinstance(v, dict) else None
    if key in q:
        return _ok(q[key])
    for a in alts:
        if a in q:
            return _ok(q[a])
    return _ok(q.get(key.split(".")[-1]))


class SnapRow(TypedDict, total=False):
    """快照里单条标的。px/chg 必有；其余只在主机返回时才写。

    ⚠️ 键名是**快照口径**（px/chg），与行情接口的 f2/f3 不同名 ——
    analyze 复用本快照时要做一次映射，见 analyze._quotes_from_snapshot()。
    """
    name: str
    px: float
    chg: float
    f62: float
    f24: float
    # 主力净占比 = f62/f6×100。跨板块唯一可比的资金口径，
    # analyze 的 detect()/build_brief/兜底模板都读它（见下方写入点注释）。
    f184: float
    # 以下三个供 analyze 复用本快照时使用（原先是各取一遍，见写入点注释）：
    f6: float      # 成交额，build_brief 的【量能对比】要用
    f104: float    # 涨家数，仅 A股指数返回
    f105: float    # 跌家数，仅 A股指数返回


class Snap(TypedDict, total=False):
    """pending.json 的当轮快照。

    ⚠️ total=False 在这里是**如实**的，不是偷懒：storage 只在拿到分时数据
    时才写（见下方 `if sg:`），而 idx/sec 在构造时就是空 dict 再逐个填充 ——
    所以"键存在但可能为空"才是真实语义。
    """
    time: str
    idx: dict[str, SnapRow]
    sec: dict[str, SnapRow]
    ev: list[dict[str, Any]]
    storage: dict[str, Any]
    # 下面四个是写完快照后追加的（见 _main 尾部）—— 漏掉它们会让
    # `snap["date"] = ...` 报 "Could not assign item in TypedDict"（实测 4 条）。
    date: str
    hist: list[dict[str, Any]]
    events: list[dict[str, Any]]
    news: list[dict[str, Any]]


def evaluate(q, sg, now):
    ev = []
    # 原先这里还有 `hm = now.hour*60 + now.minute`，但函数体里从未使用
    # （窗口判定在 _main 里，用的是另一个局部 hm）。删掉。
    guard = in_guard(now)
    # ⚠️ 恒生科技的阈值判定**必须在港股开盘后**（09:30），不能跟着 A 股的
    # 采集窗口（09:25）走。
    #
    # `[实测]` 2026-09-30：collect 首轮 09:27 就把**9/29 的收盘价**当成实时
    # 行情判了一遍 —— 写进 pending.json：
    #   {"k":"hstech_down","msg":"恒生科技 4249.62 ≤ 4300","chg":0.0,"at":"09:26"}
    # 4249.62 正是 9/29 的收盘价（港股 09:30 才开盘，此前 f2 是昨收，
    # 连 f3 都是 0.0）。后果有两条：
    #   ① 面板把这个隔夜事实显示成"09:26 触发的异动"，读者以为当时发生了什么
    #   ② `events` 按 key 去重（见下方 `keys` 集合）—— 这条 09:26 的记录
    #      会把 `hstech_down` 这个键**永久占住**，港股开盘后**真的**跌破
    #      也不会再记一条，面板上永远显示的是昨收那个数
    #
    # 与 analyze 侧的盘前闸门（`if hm < TH["session"][0]: hits = []`）
    # 是**同一类**问题：盘前拿到的价格不是有效行情，不能用来判异动。
    # 那边挡的是 A 股（09:25 集合竞价撮合），这里挡的是港股（09:30 开盘）。
    # A 股指数（IDX3S 那一段）不需要另加闸门 —— 它的窗口起点 09:25 就是
    # A 股开盘，晚于 collect 的采集起点。
    #
    # 阈值用字面量，与上方 hstech_up/down 的写法一致（理由见那里的注释）。
    # ⚠️ 判据用 `M.hk_session_open(now)`（2026-11-05 修，P1-13）——
    # 与 analyze.detect / alert.check_index 同一真源。不只是"今天开不开市"
    # （`is_hk_trading_day`）+ "过了 09:30"：
    #   · 整日休市（重阳/佛诞…）：f2 是上一交易日收盘，不能判；
    #   · **半日市 12:00 后**：f2 定格当日收盘，`is_hk_trading_day` 仍 True，
    #     原先这一支整个下午都开着 —— 把收盘值写进 pending.json events、
    #     面板事件区显示一条"午后触发的异动"。
    # `hk_session_open` 已含 09:30 开盘下限，以下判断不需要再写 `_HK_OPEN`。
    _hk_live_now = M.hk_session_open(now)
    # ⚠️ 2026-11-06（全量复核 P1-2）：A 股侧同样需要「此刻是否已开盘」的判据。
    # 采集窗口 09:25 起，而 A 股连续竞价 09:30 才开始 —— 中间那 5 分钟里
    # f2 是集合竞价撮合价、f3 可能仍是**上一交易日**的涨跌幅。
    # `[实测]` `evaluate(q, [], 09:26)` 用昨天的 f3 产出
    # `idx_weak 上证指数 -3.20%`，写进 pending.json 并占住 idx_weak 去重键，
    # 开盘后真的跌破也不再记 —— 与 2026-09-30 港股那次是同一个病。
    _a_live_now = M.a_session_open(now)
    v = _q(q, "124.HSTECH", "HSTECH")
    if v and M.isnum(v.get("f2")) and _hk_live_now:
        # ⚠️ f3 必须单独守卫。上面 isnum(v.get("f2")) 只证明了 **f2** 是数值，
        # 与 f3 无关 —— 主机少返一个字段时 `v["f3"]` 直接 KeyError，
        # 而 evaluate() 是被 _main **裸调用**的（无内层 try），异常冒到
        # main() 的兜底 except，整轮采集记成 "error"：pending.json 一条都
        # 写不出，hist 也不更新。实测：q={"124.HSTECH":{"f2":4200.0}} ->
        # KeyError 'f3'；而缺 f2 时因为有守卫会正常返回 []。
        # 备用源 feeds.backup_quotes 的 'f3': row.get("chg") 同样无默认值。
        _v3 = v.get("f3") if M.isnum(v.get("f3")) else 0.0
        if v["f2"] >= hstech_up_threshold(now.date()):
            ev.append({"k":"hstech_up","msg":f"恒生科技 {v['f2']} ≥ {hstech_up_threshold(now.date())}",
                       "chg":_v3,"prio":"high"})
        # 默认值写**字面量**，与 analyze.detect() 的 T("hstech_down", 3737) 逐字一致。
        # 注：threshold() 的实现是 `TH.get(key, default)` —— 只要 TH 里有该键，
        # 传入的 default 就完全不参与，所以 threshold(k, TH[k]) 与
        # threshold(k, 字面值) 在正常情况下**等价**，不是分叉来源。
        # 改用字面量是为了另一种情况：若某天该键从 TH 里被删掉，
        # 写 TH[k] 会在**求值实参时**直接 KeyError（在 threshold 内部兜底之前就炸），
        # 而字面量会安静地退回一个明确的值。
        if v["f2"] <= threshold("hstech_down", 3737):
            # ⚠️ 键名必须是 hstech_down（与 analyze.detect / webui 一致）。
            # 原先写 hstech_dn —— 同一事件两个名字，pending.json 里叫 dn、
            # fired 里叫 down，面板按 down 查永远查不到。
            # （阈值键 hstech_down 本来就对，只有事件键写错了。）
            ev.append({"k":"hstech_down","msg":f"恒生科技 {v['f2']} ≤ {threshold('hstech_down', 3737)}",
                       "chg":_v3,"prio":"high"})
    if not guard:
        # IDX3S() 返回 {完整 secid: (名称, 阈值)}（来自 M.idx_thresholds()），
        # 而 quotes() 也按完整 secid 建键 —— 直接取即可。
        # 注：事件键**不再用短码**（原为 idx_000001）。见下方按方向拆键的说明
        # —— 短码键在面板上会被无条件折叠成 idx_weak「大盘走弱」。
        for _sid, (_nm, thr) in IDX3S().items():
            x = _q(q, _sid)
            # ⚠️ 港股整日休市时**不得**用它的 f3 判 3σ（2026-10-20 自查）：
            # 这与上面 hstech_up/down 那支是**同一个病**的第三处 ——
            # 2026-10-19 只补了 hstech，这一支漏了。休市日 f3 是**上一
            # 交易日**的涨跌幅，`abs(f3) >= thr` 会把它当成"今日异动"
            # 写进 pending.json 的 events，面板事件区因此凭空多一条
            # 「大盘走弱/走强」，且当天去重键被占住。
            # ⚠️ 2026-11-06 更正：本行原写「A 股指数不受影响」—— 它只说对了一半。
            # 休市日确实不跑，但**09:25–09:30 这 5 分钟是真空档**：
            # 采集已开始而 A 股未连续竞价，f3 可能还是昨日值。
            # 所以 A 股行也需要 _a_live_now（见下方合并判据）。
            # 2026-11-05：与上面 hstech 支改用同一判据 —— 半日市 12:00 后
            # 港股已收盘，f3 定格，同样不能当"午后异动"写进事件。
            # 港股行与 A 股行各用各自的「此刻是否开市」判据，
            # 合并成一个条件，避免两处 startswith 各写一遍（口径容易分岉）。
            _is_hk_row = str(_sid).startswith(("100.", "124."))
            if _is_hk_row:
                if not _hk_live_now:
                    continue
            elif not _a_live_now:
                continue
            if x and M.isnum(x.get("f3")) and abs(x["f3"]) >= thr:
                # ⚠️ 事件键必须**带方向**。原实现一律写 `idx_<短码>`，
                # 而 webui._ev_alias 把所有 `idx_*` 折叠成 `idx_weak`
                # （EV_LABELS 里标签是「大盘走弱」）—— 而本判据是
                # `abs(f3) >= thr`，**上涨下跌都触发**。
                # 实测（2026-09-28）：恒生指数 +4.04% 会让面板事件区显示
                # 「大盘走弱 / 已触发」，附言还写着 +4.04%（自相矛盾），
                # 同时冒占了 analyze 那条真 idx_weak（上证 ≤ -1.0%）的行。
                # 拆成 idx_weak / idx_strong，与 analyze.detect 的命名对齐。
                _up = x["f3"] > 0
                ev.append({"k":("idx_strong" if _up else "idx_weak"),
                           "msg":f"{x.get('f14') or _nm} {x['f3']:+.2f}% (≥{thr:.2f}%)",
                           "chg":x["f3"],"prio":"high"})
    # 板块资金异动：配置驱动，统一用主力净占比(f184)
    FP = threshold("watch_fund_pct", 8.0)
    # 走 _cfg_items 而不是直接迭代 config().get("watch")：sectors.json 是
    # 用户可编辑的，段落里混进非 dict 项时 item.get() 会 AttributeError，
    # 被外层吞成一条无信息的 "error"（整轮采集报废）。实测 2026-09-26。
    for _seg, item in _cfg_items("watch"):
        # 同 analyze.detect()：取值必须走 _sid() 补齐前缀，否则短码写法下
        # q.get("BK1036") 永远 None -> fund_* 事件**整类静默消失**
        # 配置项字段一律 .get()：sectors.json 是用户可编辑的，漏写字段
        # 不该让整轮采集崩掉（裸下标会 KeyError，被外层吞成一条无信息的日志）
        # ⚠️ 必须用 `M._cfg_id` 取值，它会 **strip**。
        # 原先闸门写 `not _c0.strip()`（说明本意就是容忍空格），
        # 但下一行 `c = M._sid(_c0)` 用的是**未 strip 的原值** ——
        # 闸门 strip、取值不 strip，两套口径。
        # 而决定**请求什么**的 `watch_secids()` 走的是 `_cfg_id`（会 strip），
        # 于是取数键 `90.BK1036` 与查表键 `  90.BK1036  ` 对不上。
        # 实测（2026-09-28，沙箱里给 sectors.json 的 watch[0].code 加空格）：
        #   watch_secids() -> '90.BK1036'（已 strip）
        #   evaluate() 产出 -> []（fund_* 事件**静默消失，零日志**）
        # sectors.json 是用户可编辑的（文件头明写"改此文件即时生效"），
        # 复制粘贴带入空格很常见。
        # 这正是「修正写在没调用点的地方」——`_cfg_id` 就是为这件事写的，
        # collect 与 analyze.detect 都没调用。
        _c0 = M._cfg_id(item, "code")
        if not _c0: continue
        c = M._sid(_c0); nm = item.get("name") or _c0
        # ⚠️ 必须走 _q 这个**唯一取值入口**，不能裸 q.get ——
        # _q 负责"值不是 dict 就当没取到"，裸 .get 会把非 dict 原样交给
        # 下面的 x.get("f184") 抛 AttributeError（实测 q={"90.BK1137":"坏"}）。
        # 上面 hstech 段已走 _q，这里漏了，同一函数内两套标准。
        x = _q(q, c, _c0)
        if not x or not M.isnum(x.get("f184")): continue
        if abs(x["f184"]) < FP: continue
        # ⚠⚠ 2026-11-07（复核 F2）：板块资金支**也要开市闸门**。
        # 2026-11-06 只给上面的指数行加了 `_a_live_now`，同一函数里的
        # 板块支漏了 —— 而 f184（主力净占比）是**当日累计值**，
        # 09:25~09:30 拿到的是上一交易日的全天值，写进 pending.json
        # 同样会占住 `fund_<code>` 去重键。
        if not _a_live_now: continue
        f62 = x["f62"]/1e8 if M.isnum(x.get("f62")) else 0.0
        ev.append({"k":f"fund_{c}","msg":f"{nm} 主力{f62:+.1f}亿 占比{x['f184']:+.1f}%",
                   "prio":"high"})
    # high 零值守卫：停牌/集合竞价前 trends2 返回全 0 序列，high==0 可达
    # ⚠️ `sg["last"]` / `sg["open"]` 原先**没有守卫**，只有 high 有。
    # 实测（2026-09-28）trend() 返回缺 last 或缺 open 的 dict 时：
    #   缺 last  -> KeyError 'last'
    #   缺 open  -> KeyError 'open'
    #   last=None -> TypeError（None / int）
    # 可达性：marketlib.trend() 的 return 是**字面量**
    # （`{"last": px[-1], "high": hi, ...}`），所以当前**不可达**。
    # 但 trend() 的 docstring 只承诺"不可用时返回 None"，并未承诺
    # "否则必含这几个键" —— 契约缺口。且本文件对 f2/f3/f14/f184 都做了
    # 逐字段守卫，唯独这里漏了，同一函数内两套标准。
    # 用 isnum 而非 is not None：last=0 同样是无效分时（trend 已挡 hi<=0，
    # 但 last 独立为 0 时 (0/hi-1)*100 = -100%，会推出"存储跌 100%"的假异动）。
    if (sg and M.isnum(sg.get("high")) and sg["high"] > 0
            and M.isnum(sg.get("last")) and sg["last"] > 0
            and M.isnum(sg.get("open")) and sg["open"] > 0):
        fh = (sg["last"]/sg["high"]-1)*100
        # ⚠️ 键名必须是 storage_rev，与 analyze.detect() 一致。
        # 原先这里写 storage_fade，而 analyze 用 storage_rev —— **同一个判定
        # 两个名字**：collect 写进 pending.json 的 events[].k 是 storage_fade，
        # 而 webui 按 storage_rev 查 state.json 的 fired，两边永远对不上，
        # 面板上这个事件恒为"未触发"（实测 2026-09-24：pending.json 里
        # storage_fade 在 11:28 触发过，state.json 的 fired 里是 storage_rev）。
        # 阈值键 storage_fade 保留原名（marketlib.TH 里就是它），只统一事件键。
        # 同 hstech_down：字面默认值，与 analyze 的 threshold("storage_fade", -1.5) 一致。
        if fh <= threshold("storage_fade", -1.5) and sg["last"] < sg["open"]:
            ev.append({"k":"storage_rev","msg":f"存储芯片自高点{fh:.2f}%",
                       "prio":"normal"})
    return ev

def _log_events_once(ev):
    """命中事件组合的日志：同一（本日 × 键组合）只记一次（跨进程）。

    ⚠️ 为什么必须去重：collect 每 5 分钟一轮，而 hstech_down / storage_rev
    这类**持续状态**型命中在条件持续期间**每轮都成立** —— 原实现每轮一行
    （`[实测]` 2026-10-08：同一句「触发 ['hstech_down', 'storage_rev']」
    从 11:14 连写到 12:11；历史日均 99~155 行），把 watch.log（2MB 单代
    轮转）的真实诊断日志刷掉，与 [[market-watch-log-noise-false-signal]]
    是同一类"判据退化 + 日志噪音"。

    ⚠️ 去重键必须**含键组合**：`_daily_log_once` 按 (host,key) 每日一次，
    若 key 只用 "hits"，当天第一次之后的**任何组合变化**都会被吞掉 ——
    而"组合变了"恰恰是这条日志唯一的增量信息。
    """
    if not ev:
        return
    try:
        _keys = sorted({str(e.get("k")) for e in ev
                        if isinstance(e, dict) and e.get("k")})
    except Exception:
        return
    if not _keys:
        return
    M._daily_log_once("collect", "hits:" + ",".join(_keys),
                      f"触发 {_keys}（本日该组合首次，相同组合不再记）")

def main():
  try:
    return _main()
  except Exception as e:
    try:
        import traceback
        from marketlib import log as _l
        # 记 traceback（截尾 500 字）：只记 `KeyError: 'chg'` 一行时，
        # 定位被迫靠沙箱复现（2026-10-08 审查实测）。同文件 import 失败
        # 路径与 _boot 都记了 format_exc，这里原先是第三套标准。
        _l(f"collect 异常: {type(e).__name__}: {e}\n"
           f"{traceback.format_exc()[-500:]}", "COLLECT")
    except Exception:
        pass
    # 必须回状态串：这里原本隐式返回 None，_boot 会把它记成 "ok"，
    # 于是一次取数失败在流水里表现为"正常采集" —— 比不记还糟。
    return "error"

def _as_news_list(v: object) -> list[Any]:
    """外部快讯返回值 -> list。非 list 一律当空（而不是抛 TypeError）。

    ⚠️ 为什么需要：`_news()` 是**模块级导入的外部函数**（marketlib.news），
    其降级链上还有 feeds / 新浪两个源。调用点写的是
    `[_plain(n) for n in _news() if isinstance(n, dict)]` —— 那里的
    isinstance 只保证**元素**是 dict，没保证返回的是**可迭代对象**。
    返回 None 会抛 `TypeError: 'NoneType' object is not iterable`，
    冒到 main() 兜底 except -> 整轮记 "error"；而此刻 pending.json
    已构造完毕、**只差最后一步写入**，本轮快照与事件表一起丢。

    实测（2026-09-28）：_news() 返回 None -> TypeError；
    返回 dict/str -> 0 条（dict 迭代出键字符串，被元素级 isinstance 挡掉）。
    可达性：marketlib.news() 两个 return 都是 list（`return b` / `return out`），
    news_sina() 恒定 `return out[:limit]` —— 当前**不可达**。但同一表达式里
    对元素设防、对容器不设防是两套标准，且外部依赖的返回形状不是本模块的契约。

    ⚠️ 抽成模块级函数而不是内联，是为了让 selfcheck 能调**真实实现** ——
    若在检查项里复刻一份同样的推导式，collect 这行改坏了检查照样报绿。

    ⚠️ 标注 `object -> list[Any]` 是必须的：不标注推成
    `(Unknown) -> list[Unknown]`，Unknown 扩散到消费点。实测这一处
    无论怎么标注都是 +2 条诊断（`object`/`list[Any]`/内联三种写法皆然）——
    因为返回值只被消费一次，标注收益抵不掉扩散。
    **接受这 +2**：缺陷代价是"整轮快照+事件表全丢且无日志"，
    与 2 条静态噪音不是一个量级。
    """
    return v if isinstance(v, list) else []


def _pending_lock(path):
    """对 `pending.json` 取排他锁，返回 fd（失败返 None）。

    ⚠⚠ 2026-11-11（审查，P2）：抽成小函数是为了**能被直接测**。
    上一版把 lock/unlock 写在 `_main()` 中间，测试只能自己靠自己的探针
    加锁、再自己断言——**测的是探针而不是生产代码**，把生产里的
    锁整段删掉也不会变红。
    与「把排序抽成 `_split_boards` 供自检用」是同一个道理。
    锁失败就返 None（开锁）——采集不能因为锁而不工作。
    """
    try:
        import fcntl as _fc
        _fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        _fc.flock(_fd, _fc.LOCK_EX)
        return _fd
    except Exception:
        return None


def _pending_unlock(fd):
    """释放 `_pending_lock` 取得的锁。任何异常都吞掉。"""
    if fd is None:
        return
    try:
        import fcntl as _fc
        _fc.flock(fd, _fc.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass



def _main():
    """返状态串供 _boot 落流水。

    原先两处裸 return 不留任何痕迹。collect 每天被 launchd 启动约 288 次
    （plist 为 StartInterval=SCHED_INTERVAL，即每 5 分钟一轮、全天无休），
    其中约 230 次落在非交易时段 —— 那些空转记录本身有价值
    （能证明 launchd 触发正常），所以在这里区分出 not_trading_day 与
    out_of_window，而不是一起静默掉。

    ⚠️ 空转代价可忽略：窗口检查在最前面，实测单轮仅 1ms
    （2026-09-24 实测 195 个空转轮次合计 <1s），所以不必为省这点开销
    去改 plist 的调度时段 —— 那样反而会让"launchd 是否正常"失去证据。
    """
    _pnd_lk = None          # pending.json 跨进程锁 fd（见下方读改写处）
    now = datetime.datetime.now()
    if now.weekday() >= 5 or now.date().isoformat() in HOLIDAYS:
        return "not_trading_day"
    hm = now.hour*60 + now.minute
    # 用 marketlib.TH["session"]（9:25~15:05）而不是硬编码 —— 那是**行情
    # 交易时段**的权威定义，marketlib.in_session() 也读它。原先两处各写一份，
    # 改一处必忘另一处。
    #
    # ⚠️ 注意这**不等于** analyze 的运行窗口：analyze 的窗口上界是最后一个
    # 槽位的 TTL 结束（15:00+20min = 15:20，见 analyze.SLOT_TTL），那是为了
    # 给「收盘定局」留补推机会。收盘后行情不再变化，采集没有意义，
    # 所以这里保持 15:05 —— 若跟着改成 15:20，只是空转采集 15 分钟。
    _s0, _s1 = TH["session"]
    if not (_s0 <= hm <= _s1):
        return "out_of_window"

    _req = index_secids() + watch_secids() + hk_secids()
    q = _quotes(_req)
    sg = _trend("90.BK1137")
    # ⚠️ 全源失效时必须**早退**，不能照写空快照。
    # 原实现无论取到多少数据都往下走，产出的空壳 pending.json 有三个后果：
    #   ① idx/sec 是**整文件覆盖**（不是 append）-> 取数失败一轮就清空此前累积的
    #      全部板块数据，直到下一轮成功才恢复
    #   ② 空壳进 hist 顶掉真实记录 -> changes() 在"空快照位于末尾"时返回 []
    #      -> build_brief 的【盘中变化】整段消失（AI 只能描述静态现状）
    #   ③ 状态串仍是 "ok" -> selfcheck 只看 pending.json 的 mtime 不看内容
    #      -> **自检也报正常**，真实故障伪装成正常
    # 有效条目太少（不足请求数的一半）视同全源失效 —— 部分失效同样会污染 hist。
    #
    # ⚠️ 分子分母必须按**真实标的**去重，不能直接数 q 的键。
    # marketlib.quotes() 末尾会给每个请求 id 补一份**短码镜像键**
    # （`out[i] = out[i.split(".")[-1]]`，见那里的注释），于是同一个标的
    # 在 q 里最多出现 **2 次**：`1.000001` 与 `000001`。
    # 原判据分子遍历 q.values()（含镜像键）、分母是 len(_req)（不含），
    # 两边口径不一致 —— 真实覆盖率只要到 **29%** 就能让 _valid*2 >= len(_req)
    # 放行。实测（2026-09-28 09:22，精确复刻 EM 部分成功返回短码键）：
    #   len(_req)=21  q 键数=14（别名键 5 个）  _valid=14  去重后真实标的=9
    #   现行判据 28 < 21 = False -> **放行**（9/21 = 43%，本该早退）
    #   去重判据 18 < 21 = True  -> 早退 ✅
    # 后果正是上面注释 ①②③ 自称已修的那三条：空壳覆盖掉此前累积的 12 个
    # 板块、hist 被空快照顶掉、【盘中变化】整段消失，而状态串仍是 ok。
    # 分母同样去重：_req 里 idx 用完整 secid、watch 也是，但去重后
    # 才与分子同口径（原先 len(_req) 恰好没有重复，属侥幸）。
    _uniq = {k.split(".")[-1] for k, x in q.items()
             if isinstance(x, dict) and M.isnum(x.get("f2")) and x["f2"] > 0}
    _den = {i.split(".")[-1] for i in _req}
    _valid = len(_uniq)
    if not _uniq or _valid * 2 < len(_den):
        # ⚠️ 日志必须**指出是哪一段失效**（2026-10-27 修，P2）。
        # 生产实测 24 次「有效 9/21」全部是**板块源单独失效**（12 个板块
        # 只在 HTTPS 主机有可信数据），而 6 指数 + 3 港股是好的 ——
        # 原先一行「全源失效」把"板块单点故障"和"真的什么都没取到"
        # 混成同一句话，排查者会去查指数源（实际没问题）。
        # 这里**只改日志措辞，不改判据**：为什么仍然早退而不是写部分快照，
        # 见上方 ①②③ —— `_quotes_from_snapshot` 只要拿到非空 dict 就会
        # 复用它，一份没有 sec 的快照会让 analyze **整轮丢失板块数据**
        # （比"沿用 5 分钟前的完整快照"更糟）。故保持早退，只把归因写清楚。
        _idx_den = {i.split(".")[-1] for i in index_secids() + hk_secids()}
        _sec_den = {i.split(".")[-1] for i in watch_secids()}
        _idx_ok = len(_uniq & _idx_den)
        _sec_ok = len(_uniq & _sec_den)
        _which = ("指数/港股源正常、**板块源单独失效**"
                  if _idx_ok * 2 >= len(_idx_den) else "指数与板块均失效")
        _log(f"全源失效（有效 {_valid}/{len(_den)} 个标的，q 键 {len(q)} 个"
             f"含别名；其中指数/港股 {_idx_ok}/{len(_idx_den)}、"
             f"板块 {_sec_ok}/{len(_sec_den)} —— {_which}），"
             f"本轮跳过，保留上一轮快照")
        return "no_data"

    ev = evaluate(q, sg, now)
    # snap 的三个容器键在**构造时**就确定了类型（idx/sec 是 dict、ev 是 list），
    # 所以这里的 dict 字面量可以直接标注成 Snap —— 不是"给动态数据硬套"，
    # 而是如实描述本行刚构造出来的东西。不标注的话类型检查器把 snap 当
    # dict[str, object]，下游 snap["idx"][c] = row 会报"可能对 str 做下标"
    # （实测 reportAttributeAccessIssue / reportIndexIssue 共 4 条）。
    snap: Snap = {"time": now.strftime("%Y-%m-%d %H:%M"),
                  "idx": {}, "sec": {}, "ev": ev}
    # ⚠️ 必须跳过**短码镜像键**，否则同一标的在 snap/hist 里出现两次。
    # marketlib.quotes() 末尾补的镜像键（`out["000001"] = out["1.000001"]`）
    # 与完整 secid 指向**同一个 dict 对象**，而这里 `"BK" in c` 判不出重复，
    # 于是两份都进 snap["idx"]/snap["sec"]，再随 hist 落盘。
    # 下游 analyze.build_brief 是"用快照里真实存在的键反查名称"，于是
    # **每个指数/港股名各出现两次**，渲染进【盘中变化】：
    #   上证指数: 现 +0.50%（10:00 时 -1.00%，较之 +1.50pp，较上一快照 +1.50pp）
    #   上证指数: 现 +0.50%（...）      <- 同一行重复
    # 而 `ch[:6]` 只有 6 个槽位，重复行占掉 2/3，真实板块被挤出该段。
    # 实测（2026-09-28，沙箱）：2 个真实标的写成 4 条键。
    # 判据：短码（无 "."）且 q 里存在同后缀的完整 secid -> 它是镜像键，跳过。
    # 保留完整 secid 是因为 sectors.json / analyze 都用完整 secid 查表。
    _full_suffix = {k.split(".")[-1] for k in q if "." in k}
    for c, x in q.items():
        # ⚠️ 非 dict 值必须挡在 `.get` 之前（2026-10-08 审查）：`_q` 的
        # 非 dict 守卫只覆盖它自己的取值入口，本循环直接遍历 q ——
        # 降级链任一环换成外部实现（backup_quotes 可被替换）返回非 dict
        # 值时，`x.get` 抛 AttributeError -> 整轮 error。现网三个产出
        # 路径都构造 dict，属防线补全。
        if not isinstance(x, dict): continue
        if not M.isnum(x.get("f2")): continue
        if "." not in c and c in _full_suffix:
            continue                    # 短码镜像键：完整 secid 已覆盖同一标的
        # ⚠️ 这一行的三个取值**都必须走 .get()**。上方 `isnum(x.get("f2"))`
        # 只证明了 f2「是数值」——它同时排除了 None 与键缺失，所以 f2 的
        # 裸下标此刻是安全的；但 **f3 与 f14 完全没有前置守卫**，主机少返
        # 一个字段就 KeyError，**打断整个快照循环**：本轮 pending.json 一条
        # 都写不出（外层 main() 记成 "error"），此前累积的 hist 也不更新。
        # 实测：主机返回缺 f14 → KeyError 'f14'；缺 f3 → KeyError 'f3'。
        # 备用源（feeds.backup_quotes）的 f3 来自 row.get("chg")，无默认值，
        # 该源解析失败时同样可能是 None/缺键。
        # 标注成 SnapRow：这个字面量的键和值类型在本行就确定了
        # （px 是数值、chg 是数值），写成裸 dict 会让检查器把它当
        # dict[str, Unknown|float]，下游 snap["sec"][c] = row 报参数类型不符
        # （实测 2 条 reportArgumentType）。
        #
        # ⚠️ `chg` **不能**在 f3 缺失时静默写 0.0。
        # 同一个 dict 里 `f62/f24/f184` 都是"缺失就不写键"，唯独 chg 写 0.0 ——
        # 于是盘面上得到 `{"px":3800.0,"chg":0.0}`，下游渲染成
        # 「上证指数 持平 +0.00%」。而"真的收盘价没动"与
        # "这个源根本没给涨跌幅"在 JSON 里**完全不可区分**，
        # 读者看到的是一个**正常数字**，无法察觉数据缺失。
        # 这与 `feeds.py` 里已修好的 `ratio` 默认 "0%" 是同一个错误的两处。
        #
        # 修法与 f62 对齐：缺就不写键。`chg` 在 TypedDict 里是必填，
        # 但 SnapRow 是 `total=False`，下游 `_row(v)` 也一律走 `M.isnum` 守卫
        # （见 __main__ 的消费点），所以不写键是安全的 —— 缺键会被下游
        # 正确识别为"数据缺失"，而不是被误读成"平盘"。
        row: SnapRow = {"name": x.get("f14") or c,
                        "px": x["f2"]}
        if M.isnum(x.get("f3")): row["chg"] = x["f3"]
        # ⚠️ 精度取 4 位而非 2 位：快照会被 analyze 复用（见
        # analyze._quotes_from_snapshot），而复用是**往返**的 ——
        # 落盘时 /1e8 存成亿元、复用时 *1e8 还原。若只存 2 位，往返误差
        # 最大 0.005 亿（50 万元），足以把 `:+.1f` 的显示**翻转一位**：
        # 实测同一份行情，自取路径出「AI应用 -15.6 亿」、复用路径出
        # 「-15.7 亿」（原始值 -15.649 亿）。
        # 4 位的往返误差 ≤0.00005 亿（5 千元），低于任何显示精度
        # （最高用到 :+.2f）的半个刻度，两条路径因此逐字节等价。
        # 代价：每行多 1~2 字节，一个快照约 +30 字节 —— 可忽略。
        if M.isnum(x.get("f62")): row["f62"] = round(x["f62"]/1e8, 4)
        if M.isnum(x.get("f24")): row["f24"] = x["f24"]
        # ⚠️ f184（主力净占比）必须一起带上 —— 它是**唯一**跨板块可比的资金
        # 口径（f62 绝对额对小板块和大板块不是一个尺度：白酒成交约 100 亿、
        # 半导体约 2500 亿，同样"70 亿"前者 69% 后者 2.8%）。
        # 原先这里只写 f62，f184 被丢弃，于是 analyze 的**兜底模板**里
        #   `pct = _num(x.get("f184"))` 恒为 None
        # -> risk/opp 两个列表永远为空 -> 「建议」段固定输出两句空话：
        #     风险 留意资金持续流出的方向
        #     机会 留意资金承接的方向
        # 实测（2026-09-28，AI 超时那轮）：pending.json 全天 12 个快照的
        # sec 条目里 f184 一个都没有（`{"chg":-7.08,"f62":-245.09}`），
        # 而同一时刻实时抓取该字段是有值的（CPO -11.16、半导体 -6.78）——
        # 数据源没问题，是**采集层把它丢了**。
        # 影响面不止兜底模板：analyze 的 detect()/build_brief 也从 q 里
        # 取 f184，缺了它就只能退化成绝对额口径（跨板块不可比）。
        # f184 同理取 4 位（显示精度 :+.1f，边界在 .x5 —— 存 2 位会在
        # -5.749 这类值上把显示从 -5.7 翻成 -5.8）。
        if M.isnum(x.get("f184")): row["f184"] = round(x["f184"], 4)
        # ⚠️ f6/f104/f105 必须一并落盘 —— analyze 复用本文件时会用到。
        #
        # 背景：`analyze.run()` 每轮都取与 collect **完全相同**的 21 个标的
        # （实测两者 ids 集合缺 0 多 0），此前是各取一遍。改为复用本快照后，
        # analyze 需要的字段就必须在这里存全，否则复用处拿到缺字段的行、
        # 静默退化成"没有成交额/涨跌家数"。
        #   · f6   = 成交额 —— build_brief 的【量能对比】("已达上日 63%") 要用
        #   · f104 = 涨家数、f105 = 跌家数 —— 仅 A股指数返回，港股/板块无
        # 这三个字段 collect 本来就取到了（quotes() 的默认 fields 含它们），
        # 原先只是没写进快照。缺失就不写键，与上面 f62/f184 同一约定。
        if M.isnum(x.get("f6")): row["f6"] = x["f6"]
        if M.isnum(x.get("f104")): row["f104"] = x["f104"]
        if M.isnum(x.get("f105")): row["f105"] = x["f105"]
        # 必须判 "BK" in c 而非 c.startswith("BK")：
        # _fetch 返回的键是**完整 secid**（"90.BK1137"），已经带市场前缀，
        # 用 startswith 会把所有板块误判成指数塞进 idx，snap["sec"] 永远为空 ——
        # 下游 build_brief 的【盘中变化·资金变化】完全取不到板块数据。
        if "BK" in c: snap["sec"][c] = row
        else: snap["idx"][c] = row
    if sg: snap["storage"] = sg

    # 累积当日快照（保留最近24条，供 Claude 看趋势）
    hist = []
    if os.path.exists(OUT):
        # ⚠️ 损坏必须留痕。原先 `except Exception: pass` 把"解析失败"和
        # "当天没有历史"混为一谈 —— 实测截断 pending.json 后 7 条 events +
        # 12 条 hist 全部丢失，状态串仍是 "ok"，日志里零记录。
        # 下游表现是【盘中变化】整段消失、事件去重表清空（当天重复推送）。
        # ⚠️ 判据必须看**返回值**，不能靠 except。
        # `M._load_json` 内部已经 try/except 全吞（marketlib.py:328-334，
        # docstring 就写着"任何异常返回 default"），所以它**永不抛异常**：
        # 截断/空文件/非 JSON 全都静默 `return {}`，落到
        # `isinstance(old, dict)` 为真 -> 判成"当天没有历史" -> **不走 except**。
        # 于是上面那段"损坏必须留痕"的注释虽然写对了，实现却是**死代码**：
        # 唯一被拦下的只有"合法 JSON 但非 dict"这一种形态。
        # 实测（2026-09-28，沙箱，截断 pending.json 到 486 字节）：
        #   status='ok'   新日志=''   含 ERR 留痕=False
        #   重建后 hist=1（12 条历史丢了）  events=0（7 条去重表清空）
        # 用 default=None 作哨兵：None = 解析失败（文件存在但读不出），
        # 与"文件不存在"（os.path.exists 已挡）和"合法空对象"区分开。
        # ⚠⚠ 2026-11-11（审查，P2）：**读改写要加锁**。
        # `collect` 对 `pending.json` 是「读历史 → append → 整体写回」，
        # `_atomic_write_json` 只保证**单次写**不撕裂，**不保证读-改-写不丢更新**。
        # 正常情形 launchd 串行调度、不会并发；但两种情形会：
        #   ① 手动跑一次（排障时很常见）恰逢 launchd 那一轮
        #   ② 上一轮因数据源慢而超过 300s，下一轮已启动
        # 两进程各自读到同一份旧历史 → 后写的覆盖先写的 → hist 少一条、
        # events 去重表少一批（去重表丢了会**重复推送**）。
        # 用与 `runlog.beat` 同一套：独立 `.lock` 文件 + flock，锁失败就退回无锁
        # （采集不能因为锁而不工作）。锁覆盖整段读-改-写，到写完为止。
        _pnd_lk = _pending_lock(OUT)
        old = M._load_json(OUT, None)
        if old is None:
            _log("pending.json 无法解析（截断/非 JSON），本轮历史为空", "ERR")
        elif not isinstance(old, dict):
            _log("pending.json 结构异常（非 dict），本轮历史为空", "ERR")
        elif old.get("date") == now.date().isoformat():
            _hist_raw = old.get("hist", [])
            # ⚠️ hist 必须**逐条**校验，与下方 old_ev 同构。
            # 原先只判 `isinstance(hist, list)`，容器对了就整批收下 ——
            # 同一轮里两个外部来源用了两套标准（old_ev 有逐条过滤）。
            # 实测：写 12 条 hist 其中 11 条非 dict，read_history() 过滤后
            # 只剩 1 条 -> `len(h_) >= 2` 不成立 -> 【盘中变化】整段消失，
            # 而这一轮状态串仍是 ok。且坏条目会通过 hist[-12:] 的容量
            # 把真实快照挤出窗口（"一条坏记录顶掉 11 个真实槽位"）。
            hist = [x for x in _hist_raw
                    if isinstance(x, dict) and "time" in x] \
                   if isinstance(_hist_raw, list) else []
            if isinstance(_hist_raw, list) and len(hist) != len(_hist_raw):
                _log(f"pending.json 的 hist 有 {len(_hist_raw)-len(hist)} 条"
                     f"形状异常，已丢弃（其余 {len(hist)} 条保留）", "ERR")
    # hist 存 chg 与 f62（主力净额，亿元）：让下游能看到"资金在如何变化"，
    # 而不仅是价格变化。f62 缺失时省略该键。
    def _row(v):
        # ⚠️ chg 必须与上方快照循环同约定「缺就不写键」：522 行的守卫
        # （`if M.isnum(x.get("f3")): row["chg"] = ...`）保证 f3 缺失的行
        # **没有 chg 键**，而这里原先是裸下标 `v["chg"]` —— 上游少返一个
        # f3 就 KeyError，冒到 main() 兜底 -> 整轮记 error、pending.json
        # 一条都写不出（2026-10-08 审查沙箱实证；同文件注释声称
        # "下游 _row(v) 也一律走 M.isnum 守卫" —— 实现与注释相反）。
        r = {}
        if M.isnum(v.get("chg")): r["chg"] = v["chg"]
        if M.isnum(v.get("f62")): r["f62"] = v["f62"]
        # ⚠️ f184 也要带上。hist 是**快照历史**，analyze 的「盘中变化」段
        # 靠它算"资金较开盘增减"。只带 f62 时，那句变化只能用绝对额表述
        # （跨板块不可比）；带上 f184 才能说"占比从 -3.2% 扩到 -11.2%"。
        # 与上面 `row: SnapRow` 是**同一个字段的两条落盘路径** ——
        # 只修一条，另一条仍然丢（实测 2026-09-28：sec 里补了 f184 之后，
        # hist 里依然没有）。
        if M.isnum(v.get("f184")): r["f184"] = v["f184"]
        return r
    hist.append({"time": now.strftime("%H:%M"),
                 "idx": {k: _row(v) for k, v in snap["idx"].items()},
                 "sec": {k: _row(v) for k, v in snap["sec"].items()}})
    # ⚠️ 窗口长度 12 → 24，与 collect 间隔由 120s 放宽到 300s **配套**。
    #
    # 实测依据（2026-09-30，当日 11 条推送正文）：
    #   · 正文里引用的参照时刻距发送 **中位 29 分钟、最大 39 分钟**
    #     （「较 09:41 再扩 15.3 亿」「自 14:24 再扩 10.7 亿」共 9 处）
    #   · 而旧窗口 12 条 × 120s = **24 分钟** —— 参照点已经**超出窗口**，
    #     AI 只能拿窗口最老那一条硬凑，"较 X 起"里的 X 并非它想指的时刻。
    #   · 同时，「较上一快照」类表述出现 **0 次** —— 逐快照粒度从未被使用，
    #     所以放宽间隔**没有损失**。
    # 24 条 × 300s = **120 分钟**，覆盖 39 分钟的实际需求且留足余量。
    hist = hist[-24:]

    # 累积当日触发事件（去重）
    old_ev = []
    if os.path.exists(OUT):
        # 同 hist：_load_json 不抛异常，必须看返回值
        o = M._load_json(OUT, None)
        if o is None:
            _log("pending.json 无法解析（截断/非 JSON），本轮事件表重建", "ERR")
        elif isinstance(o, dict) and o.get("date") == now.date().isoformat():
            old_ev = o.get("events", [])
            if not isinstance(old_ev, list): old_ev = []
    # ⚠️ old_ev 来自 **pending.json**（外部文件），不是本进程构造的 ——
    # 裸下标 e["k"] 遇到缺 k 的条目（旧版本格式 / 手工编辑 / 半截写入）
    # 会抛 KeyError，冒到 main() 的兜底 except，整轮记成 "error"：
    # pending.json **完全不更新**（hist 与事件表一起停更），
    # 而日志里只有一行无信息的异常。实测：events=[{"msg":"旧格式"}] -> KeyError。
    # 形状不对的条目直接丢弃 —— 它没有 k，本来也无法参与去重。
    keys = {e["k"] for e in old_ev
            if isinstance(e, dict) and isinstance(e.get("k"), str)}
    old_ev = [e for e in old_ev
              if isinstance(e, dict) and isinstance(e.get("k"), str)]
    for e in ev:
        _ek = e.get("k")
        if _ek and _ek not in keys:
            e["at"] = now.strftime("%H:%M"); old_ev.append(e); keys.add(_ek)

    snap["date"] = now.date().isoformat()
    snap["hist"] = hist
    snap["events"] = old_ev
    # news() 的条目含 datetime（供 AI 格式化用），json.dump 无法序列化 ——
    # 直接塞进 snap 会让 pending.json 永远写不出来（被 _atomic_write_json
    # 的 except 吞成 False），导致【盘中变化】与快讯整块功能静默失效
    def _plain(n):
        return {k: (v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime.datetime)
                    else v) for k, v in n.items()}
    # ⚠️ 迭代对象本身也要验：`_plain(n) for n in _news()` 里
    # `if isinstance(n, dict)` 只保证了**元素**是 dict，没保证 `_news()`
    # 返回的是**可迭代**的东西。返回 None 会抛
    # `TypeError: 'NoneType' object is not iterable`，冒到 main() 兜底
    # except -> 整轮记 "error"，而这时 pending.json 已经构造完毕、
    # **只差最后一步写入**，本轮快照与事件表一起丢。
    # 实测（2026-09-28）：_news() 返回 None -> TypeError；返回 dict/str
    # -> 0 条（dict 迭代出键字符串，被 isinstance 挡掉，不抛）。
    #
    # 可达性：marketlib.news() 的两个 return 都是 list 字面量/变量
    # （`return b` / `return out`，实测），news_sina() 恒定 `return out[:limit]`
    # —— 当前**不可达**。但同一行里对元素已经做了 isinstance 守卫，
    # 对容器却不设防，是同表达式内两套标准；且 `_news` 是**模块级导入的
    # 外部函数**（marketlib 的），降级链里还有 feeds/新浪两个源。
    # 守卫成本为零：包一层 isinstance 即可。
    # ⚠️ 迭代对象本身也要验：`if isinstance(n, dict)` 只保证**元素**是
    # dict，没保证 `_news()` 返回可迭代对象。返回 None 会抛
    # `TypeError: 'NoneType' object is not iterable`，冒到 main() 兜底
    # except -> 整轮记 "error"，而此刻 pending.json 已构造完毕、
    # **只差最后一步写入**，本轮快照与事件表一起丢。
    # 实测（2026-09-28）：_news() 返回 None -> TypeError；
    # 返回 dict/str -> 0 条（dict 迭代出键字符串，被 isinstance 挡掉）。
    # 可达性：marketlib.news() 两个 return 都是 list（`return b` / `return out`），
    # news_sina() 恒定 `return out[:limit]` —— 当前**不可达**。但同一表达式里
    # 对元素已设防、对容器不设防，是两套标准；且 _news 是模块级导入的
    # 外部函数，降级链还有 feeds/新浪两个源。
    # ⚠️ 代价已知：这一行让 basedpyright 从 164 → 166 warnings（+2）。
    # 试过三种标注（`object` / `list[Any]` / 抽成带标注的小函数）都是 +2~+3，
    # 因为 `_ns` 是本地量、下游只消费一次，标注收益抵不掉 Unknown 扩散。
    # 与 `_ok` 那处不同（它的返回值被 evaluate 里十几处消费，标注净减 17 条）。
    # 判断：**接受这 +2**。缺陷虽当前不可达，但代价是"整轮快照+事件表全丢"
    # 且无日志，与 +2 条静态噪音不是一个量级。
    snap["news"] = [_plain(n) for n in _as_news_list(_news())
                    if isinstance(n, dict)]
    # 原子写：analyze.py 每 5 分钟并行读此文件，非原子写会让它读到半截 JSON
    # （read_history 的 try/except 会吞掉异常，导致【盘中变化】段静默消失）
    _wok = M._atomic_write_json(OUT, snap, indent=1)
    _pending_unlock(_pnd_lk)
    if not _wok:
        _log("pending.json 写入失败")
        return "error"
    if ev: _log_events_once(ev)
    return "ok"

def _maybe_stall_alert():
    """analyze 心跳超时就推一条告警进群（按天去重）。

    ⚠ 告警路径与业务推送完全分离：它只在“主链路已经不行了”时才发，
    不会因为它多了一条常规推送。失败一律吞掉。
    """
    import json as _json, time as _time, datetime as _dtm
    import runlog as _rl2
    now = _dtm.datetime.now()
    if not M.is_trading_day(now.date()):
        return
    hm = now.hour * 60 + now.minute
    if not (9 * 60 + 35 <= hm <= 15 * 60 + 5):
        return                      # 盘外不判（否则天天假警）
    try:
        with open(_rl2.BEAT_FILE, encoding="utf-8") as f:
            _b = _json.load(f)
        _ts = _b.get("analyze") if isinstance(_b, dict) else None
    except Exception:
        _ts = None
    _age = (_time.time() - float(_ts)) if isinstance(_ts, (int, float)) else 1e9
    if not M.stall_alert_should_fire(now, age_s=_age, trading=True):
        return
    # 按天去重：同一天只告一次，避免每 5 分钟一条刷屏
    _f = M.p_cache("stall_alert_date.json")
    _today = now.strftime("%Y-%m-%d")
    try:
        with open(_f, encoding="utf-8") as fh:
            if (_json.load(fh) or {}).get("date") == _today:
                return
    except Exception:
        pass
    try:
        _cfg = _json.load(open(M.p_conf("config.json"), encoding="utf-8"))
        _hook = _cfg.get("webhook")
    except Exception:
        return
    if not _hook:
        return
    _txt = (f"> **监控告警 · 主链路疑似停摆**\n\n\u3000\n"
            f"**现象** 行情处理进程已 {_age/60:.0f} 分钟没有过认领"
            f"（阈值 {M.STALL_ALERT_MIN_AGE//60} 分钟）。\n\n"
            f"**影响** 期间不会有盘面推送；采集进程本身仍在运行。\n\n"
            f"**建议** 查 `launchctl print gui/$(id -u)/{_LD_PREFIX}watch` "
            f"与 `logs/watch.log` 末尾。\n\n\u3000\n"
            f"触发 {now:%H:%M:%S}")
    # ⚠⚠ 2026-11-11（审查，P1）：**配置中的 webhook 地址不得进进程参数**。
    # 主推送（analyze 侧）早在 2026-10-08 就改成「0600 配置文件 + curl -K」，
    # 这里（停摆告警）漏改了 —— 两条推送链路，只加固了一条。
    # 配置文件的 0600 权限不能保护已经进入 argv 的内容：能读进程参数的
    # 用户/进程都能拿到它。改法与主推送同构：URL 进 0600 文件、
    # 请求体走 stdin（`--data-binary @-`）。建不出临时文件就 fail-closed：
    # 不回退到 argv 写法，宁可不发这条告警。
    _cfg_f = None
    _sent = False
    try:
        import tempfile as _tf
        _cfg_fd, _cfg_f = _tf.mkstemp(prefix=".hookcfg.", suffix=".txt",
                                      dir=os.path.dirname(_f) or ".")
        os.write(_cfg_fd, ('url = "%s\n' % _hook).encode("utf-8"))
        os.close(_cfg_fd)
        os.chmod(_cfg_f, 0o600)
        _body = _json.dumps({"msgtype": "markdown",
                             "markdown": {"content": _txt}},
                            ensure_ascii=False).encode("utf-8")
        r = M.subprocess.run(
            ["curl", "-s", "-K", _cfg_f,
             "-X", "POST",
             "-H", "Content-Type: application/json",
             "--data-binary", "@-"],
            input=_body,
            capture_output=True, text=True, timeout=15)
        _sent = True
    except Exception:
        _sent = False
    finally:
        if _cfg_f:
            try:
                os.unlink(_cfg_f)
            except Exception:
                pass
    if _sent:
        ok = False
        if r.returncode == 0:
            try:
                _rj = _json.loads(r.stdout or "{}")
                ok = isinstance(_rj, dict) and _rj.get("errcode") == 0
            except (ValueError, TypeError):
                # 非 JSON 响应：保留原宽松判据，避免因回包格式微调而反复重发
                ok = '"errcode":0' in (r.stdout or "")
    if ok:
        try:
            M._atomic_write_json(_f, {"date": _today})
        except Exception:
            pass
        _log(f"主链路停摆告警已发出"
             f"（analyze 心跳 {_age/60:.0f} 分钟前）", "ERR")
    else:
        _log("主链路停摆告警发送失败"
             f"（analyze 心跳 {_age/60:.0f} 分钟前）", "ERR")


def _boot():
    """launchd 入口。见 analyze._boot 的说明。

    顺带落一条执行流水。collect 每天被启动 288 次，**只记 end 单条**
    （不记 start）—— 它是纯采集、无推送、失败模式单一，单条足够定位；
    双条会让文件增长快一倍（约 14MB/年 → 28MB/年）而没有额外信息量。
    """
    import traceback, time as _time
    _t0 = _time.time()
    _st = {"status": "error"}
    # ⚠⚠ 2026-11-10：**由还活着的 collect 替 analyze 说话**。
    # analyze 停摆时它自己告警不了（循环依赖），自检一天只跑一次。
    # collect 是独立进程、每 5 分钟一轮（今日实测 183 次）。
    # 全部包装：告警失败绝不能影响采集。
    try:
        _maybe_stall_alert()
    except Exception:
        pass
    M.harden_logs("collect.out", "collect.err")   # launchd 重定向文件收紧到 600
    M.rotate_logs("collect.out", "collect.err")   # 单代轮转（copy+截断，见 marketlib）
    try:
        # main() 透传 _main() 的状态串（ok / not_trading_day / out_of_window / error）。
        # 不能只写 "ok" —— 288 次/天里约 220 次是空转，全记 ok 就分不清
        # "跑完了没事" 和 "压根没到采集那一步"。
        r = main()
        if isinstance(r, str) and r:
            _st["status"] = r
        elif r is None:
            _st["status"] = "ok"
    except Exception as e:
        # 用 _log 而非本地 LOG —— 全库日志统一在 watch.log，
        # 写到 collect.log 等于没人看（该文件此前从未被创建过）
        _log(f"BOOT {type(e).__name__}: {e}", "ERR")
        _log(traceback.format_exc()[-800:], "ERR")
        _st["note"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        # 埋点失败绝不能影响主流程 —— runlog 内部已全吞异常
        _rl.run("collect", _st.get("status", "error"), phase="end",
                t0=_t0, note=_st.get("note"))


if __name__ == "__main__":
    _boot()
