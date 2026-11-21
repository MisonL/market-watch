#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
紧急推送判定。

设计原则（用户要求）：确定性很强 + 有新闻佐证，两者同时满足才推。
  - 价格异动：|涨跌幅| ≥ max(3×σ_EWMA, 静态3σ×0.7)，持续 ≥10 分钟
  - 资金异动：|f62| ≥ 50亿 且 |f184| ≥ 15%（双门槛，f184 可跨板块比较）
  - 新闻佐证：同方向事件须 120 分钟内有该事件名（或板块名）相关的快讯，
    或有宏观政策快讯（宏观类只对 index/market 事件生效，板块事件不算）
  - 源失效 fail-open：新闻源异常 / 返回空 = 按有佐证放行（真异动不因
    快讯缺失而漏报），详见 should_alert()
  - 冷却：同事件 1 个交易日（语义由 fired 键的日期段实现，非独立常量）
  - 交易时段保护：09:30-10:00 不报（开盘量比假象）、尾盘 5 分钟不新开

实测依据：
  - f184 ≡ f62/f6×100（已独立验证，误差<0.005）
  - σ 用 EWMA(λ=0.94) 自适应，因固定窗口不稳定
    （近250日复算 vs 5.3年复算：科创50 2.61% vs 2.03%，恒生科技 1.77% vs 2.46%）
  - 板块涨幅 σ 与成分股数无关（r=-0.07），不需按规模调整阈值
  - 指数阈值对称（实测上涨侧触发反而更多，偏度方案被否）
"""
import os, time, datetime, sys

# 允许 MW_DIR 覆盖：测试可在沙箱里跑而完全不碰线上状态文件
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/
from paths import p_cache
from marketlib import (UT, HIS, HIS_HTTP, cj as _cj,
                       log as _log, in_guard as _in_guard,
                       dcache_get, dcache_set)
import marketlib as M   # 原子读写助手 _atomic_write_json / _load_json

# 指数静态 σ（近250日实测，作为 EWMA 的下限保护）
# ⚠️ STATIC_SIGMA / FLOOR 已下移到 marketlib（见 marketlib.idx_thresholds）。
# 原因：collect 的 IDX3S 与这里的 check_index 原先各持一份阈值，两条通道
# 对同一指数用不同门槛（实测上证 2.25% vs 2.70%），[推送门槛, 快照门槛)
# 区间内"推送到群但快照不记"。下移到 marketlib 后两边共用同一份真值。
# 这里保留别名，避免改动过多调用点。
STATIC_SIGMA = M.STATIC_SIGMA
FLOOR = M.SIGMA_FLOOR

cj = _cj


# 腾讯日K线（备用主机 push2test 的日K线实测损坏：上证含 22 次 >15% 单日波动，
# 最大 +164%，用它算波动率会把阈值抬高 2 倍以上，导致真暴跌漏报）
TX_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            "?param={code},day,,,{n},qfq")

# 港股指数在腾讯的代码。走这条可以绕开已封锁的 push2his，
# 让港股也用上真实 EWMA σ（实测 131 日数据可用）。
TX_HK_INDEX = {
    "100.HSI":    "hkHSI",
    "124.HSTECH": "hkHSTECH",
}

def _kline_closes(secid, n):
    """取日K收盘价序列。含损坏校验：出现不可能的单日波动即判定源不可信。"""
    import subprocess as _sp
    mkt, code = secid.split(".")
    tx = ("sh" if mkt == "1" else "sz") + code if mkt in ("0","1") else None
    if not tx:
        # 港股：腾讯有完整的指数日K（hkHSI / hkHSTECH），**不要走 push2his** ——
        # 该主机已被按主机名永久封锁，每次返回 None，导致港股 σ 永远算不出、
        # 长期回退到 STATIC_SIGMA（250 日历史均值，不反映当下波动）。
        # 实测差距：恒生科技静态 3σ=5.31%，动态 3σ=3.96% —— 阈值高 34%，属漏报风险。
        tx = TX_HK_INDEX.get(secid)
        if not tx:
            # 其余（极少数）仍走 EM，不可达时由调用方回退静态值。
            # ⚠️ 2026-10-22：原先写死 `HIS` —— 那是 **HTTPS** 的 push2his，
            # 实测 0/8 全端点 rc=52（硬封）。同一主机换 `http://` 明文后
            # kline 8/8 可用（见 marketlib 文件头的主机矩阵），故改走
            # `HIS_HTTP`，并保留 `HIS` 作第二候选 —— 站点若哪天解封
            # HTTPS，回切只需调换这两行的顺序。
            d = None
            for _hh in (HIS_HTTP, HIS):
                d = cj(f"{_hh}?secid={secid}&fields1=f1,f2,f3&fields2=f51,f53"
                       f"&klt=101&fqt=1&end=20500101&lmt={n}&ut={UT}")
                if M._has_data(d, "klines"):
                    break
            try:
                # 用 M._has_data 返回的内层 dict，而不是再写 d["data"]：
                # 后者在类型检查器眼里仍是"可能为 None 的下标"
                # （reportOptionalSubscript），重取一次等于丢掉刚建立的保证。
                _inner = M._has_data(d, "klines")
                if not _inner:
                    return None
                return [float(x.split(",")[1]) for x in _inner["klines"]
                        if isinstance(x, str)]
            except (TypeError, KeyError, AttributeError, ValueError, IndexError):
                return None
    try:
        # ⚠️ Python 层超时**必须显式给**（2026-10-21 补）：`curl -m 15`
        # 盖不住 DNS 解析卡死（resolver 挂起时 curl 停在 connect 之前，
        # -m 不计时）。本函数在 `should_alert()` 的紧急推送判定里被调用，
        # 卡住就等于**整轮紧急推送判定挂死** —— 而它跑在 analyze 的主路径上，
        # 后果是整轮不退出、后续槽位全被 launchd 丢弃，且日志无痕。
        # marketlib 的 cj / proxy_get 都写了 `timeout=... + 5`，这里对齐。
        raw = _sp.run(["curl","-s","-m","15",TX_KLINE.format(code=tx, n=n)],
                      capture_output=True, timeout=20).stdout.decode(
                          "utf-8","ignore")
        import json as _json
        j = _json.loads(raw)
        node = j["data"][tx]
        kl = node.get("qfqday") or node.get("day") or []
        px = [float(r[2]) for r in kl]          # [日期,开,收,高,低,量]
    except _sp.TimeoutExpired:
        # 超时 = 本次拿不到，不是坏数据。与下面几条一样返回 None，
        # 让调用方按"源不可信"处理（宁可缺数据，不要错数据）。
        _log(f"K线取数超时({secid}，20s) —— 本次跳过", "ALERT")
        return None
    except Exception:
        return None
    if len(px) < 20:
        return None
    # 损坏校验：A股指数单日不可能 >15%
    for i in range(1, len(px)):
        if px[i-1] > 0 and abs(px[i]/px[i-1]-1) > 0.15:
            _log(f"K线源异常({secid} 第{i}根波动过大)，弃用", "ALERT")
            return None
    return px


def ewma_sigma(secid, lam=0.94, n=120):
    """EWMA 波动率；不足则回退静态值。

    按 (secid, 日期) 缓存：日K线每天只更新一次，同一交易日内重复计算
    纯属浪费（每轮 8 次 K 线请求 × 66 轮 = 528 次/天）。
    """
    dk = f"sigma:{secid}"
    hit = dcache_get(dk)
    if hit is not None:
        if hit != "__none__":
            return hit
        # 上次取数失败：短期记住（避免每轮白拉），但 30 分钟后重试一次，
        # 以便数据源恢复后当天就能用上真实波动率
        if not _none_expired(secid):
            return None
    v = _ewma_sigma_calc(secid, lam, n)
    dcache_set(dk, v if v is not None else "__none__")
    _stamp_none(secid, v is None)
    return v

NONE_TTL = 1800        # 30 分钟
NONE_TS_FILE = p_cache("none_ts.json")

def _stamp_none(secid, failed):
    """记录取数失败时刻（落盘，跨进程生效）。

    ⚠️ 键必须带 `alert:` 前缀 —— 本文件与 `marketlib._levels_stamp_none`
    **共用**,那边用的是 `levels:<secid>`。原实现写**裸 secid**,与对方的
    命名不对称:"各写各的键、不会互相覆盖"这个结论当时**只靠巧合成立**
    (两边的键形状碰巧不同),任一侧改名就会互相覆盖。
    `[实测]` 2026-10-08 文件里那两条裸鍵(100.HSI / 124.HSTECH,停在
    2026-09-23)就是旧格式的遗留 —— 单看文件分不清是哪一侧写的。

    ⚠️ 同时剔除已过期的条目:过期 = "允许重试",与键不存在等价,删掉不改变
    行为;不删则这个"只活 30 分钟"的文件会永久留着陈旧时间戳。
    TTL 必须与 `marketlib.LEVELS_NONE_TTL` **相等**(共用文件),
    自检的 [失败记忆缓存] 段断言了这条不变量。
    """
    if not failed: return
    try:
        # ⚠️ 必须走 `_locked_update`（2026-10-11 审查）：本文件与
        # `marketlib._levels_stamp_none` **共用** `cache/none_ts.json`，
        # 两处原先都是「读全量 -> 内存改 -> 整份写回」且**无锁** ——
        # 典型的 lost-update：后写的把先写的增量整段覆盖掉。
        # 后果不对称且隐蔽：被覆盖的那一侧会以为"这个 secid 刚失败过"，
        # 于是在 TTL 内**不再重试**，而它其实没失败 —— 表现为某个指数/板块
        # 静默缺一段时间的数据，日志里零痕迹。
        # 可达性：analyze.run 同一轮里先调 `M.levels()`（1313 行）再调
        # `ALERT.should_alert()`（2806 行），两者各写一次；而 selfcheck
        # 真调 build_brief（逐标的调 levels）与 should_alert 时也可能与
        # 生产 analyze 进程并发。
        # `_locked_update` 在**排他 flock 内重新读盘**再改再原子写 ——
        # 与 dead_hosts / host_stat 用的是同一把锁模式（见其 docstring）。
        def _mu(d):
            _now = time.time()
            for _k in [k for k, v in list(d.items())
                       if not isinstance(v, (int, float))
                       or (_now - v) > NONE_TTL]:
                d.pop(_k, None)
            d[f"alert:{secid}"] = _now
        M._locked_update(NONE_TS_FILE, _mu)
    except Exception:
        pass

def _none_expired(secid):
    """失败记忆是否过期（过期则允许重试）。

    ⚠️ 键必须与 `_stamp_none` **逐字对应**（`alert:<secid>`）。写用带前缀的
    键、读用裸 secid，会让读永远取到 0 → 恒判"已过期" → 每轮都重试，
    而"失败记忆"这个功能**静默失效**（既不报错也没有任何信号）。
    """
    try:
        d = M._load_json(NONE_TS_FILE, {})
        ts = d.get(f"alert:{secid}", 0)
        return (time.time() - ts) > NONE_TTL
    except Exception:
        return True

def _ewma_sigma_calc(secid, lam=0.94, n=120):
    p = _kline_closes(secid, n+1)
    if not p:
        return None
    # ⚠️ 必须挡住非正/非数值收盘价，否则这里会**抛异常**而不是返回 None。
    # _kline_closes 的损坏校验从 i=1 开始（`for i in range(1, len(px))`），
    # 所以**首根**为 0 时校验看不到它：p[1]/p[0] -> ZeroDivisionError。
    # 异常沿 check_index 冒到 analyze.run 的 `except Exception as exc`，
    # 只留一行 "ALERT EXC" —— **整轮紧急推送判定全部失效**（不止这一个
    # 指数，同轮的板块资金与大盘资金告警一起被跳过）。实测：
    #   px[0]=0    -> ZeroDivisionError
    #   px 含 None -> TypeError
    # 且这一轮不会留下 __none__ 记忆，所以下一轮重算、再抛，循环往复。
    # 非正就整条弃用（返回 None，由调用方回退静态 σ）。
    if not M.isnum(p[0]) or p[0] <= 0 or any(not M.isnum(x) or x <= 0 for x in p):
        _log(f"K线含非正收盘价({secid})，弃用", "ALERT")
        return None
    r = [(p[i]/p[i-1]-1)*100 for i in range(1, len(p))]
    if len(r) < 20: return None
    var = r[0]**2
    for x in r[1:]:
        var = lam*var + (1-lam)*x*x
    return var ** 0.5

def index_quotes():
    """指数行情。港股走腾讯 —— 备用主机(EM_TEST)的港股数据陈旧
    （实测恒指报 4.61% 而真实 0.18%），用于异动判定会造成误报。"""
    ids = ",".join(STATIC_SIGMA.keys())
    out = {}
    # ⚠️ 2026-10-22：候选改走 `_ordered_em_hosts()`（与 quotes() 同一套
    # 健康度排序），不再硬编码 (EM, EM_TEST)。原写法里 `EM` 是
    # `PERMANENTLY_DEAD` 里的 push2delay —— 每次调用都要走一遍 cj() 的
    # 封锁早退（零发包但白跑一层），而且永远只用得上第二台。
    # 这批 secid 全是指数/港股（STATIC_SIGMA 的键），三源实测逐位一致
    # （见 marketlib._codes_for_host 的说明），所以扩到四台是安全的；
    # 港股随后**无条件**被腾讯覆盖，即便某台给错也进不了判定。
    for host in M._ordered_real_em_hosts():
        d = cj(f"{host}/ulist.np/get?secids={ids}&fields=f2,f3,f12,f14&ut={UT}&fltt=2&invt=2")
        try:
            # diff 有 list / dict 两种形状（见 marketlib.clist 同名注释）。
            # 迭代 dict 得到的是**键字符串**，x["f12"] 会 TypeError ——
            # 虽然被下面的 except 兜住，但后果是**指数行情整体为空**，
            # 静默退化成"没有指数异动"。
            # 用 M._has_data 返回的内层 dict，避免再写 d["data"]
            # （reportOptionalSubscript：重取一次就退回未收窄状态）
            _inner = M._has_data(d, "diff")
            if not _inner:
                continue
            _diff = _inner["diff"]
            if not isinstance(_diff, list):
                _diff = list(_diff.values())
            for x in _diff:
                if isinstance(x, dict) and x.get("f12"):
                    out[x["f12"]] = x
        except (TypeError, KeyError, AttributeError):
            pass
        if out: break
    # 港股（100.HSI / 124.HSTECH）：一律用腾讯覆盖，防备用主机的陈旧值
    hk = [k for k in STATIC_SIGMA if k.startswith("100.") or k.startswith("124.")]
    if hk:
        # ⚠️ fail-closed 必须**无条件**执行，不能只装在 except 分支上。
        # 原实现把"删掉港股键"写在 `except Exception` 里，于是只挡住了
        # **抛异常**那一种失败。而腾讯还有一条**不抛异常**的失败路径：
        # from_tencent 对解析不出的行 `continue`，整批解析失败时它安静地
        # 返回 {}（限流、返回体被截断、字段位变动都会这样）—— backup_quotes
        # 于是返回 {}，上面这个 for 循环一次都不进，**港股的 EM 陈旧值原样
        # 留在 out 里**，下一轮 check_index 拿它去比阈值。
        # 实测（2026-09-27，沙箱，强制回退静态 σ）：
        #   A) 腾讯抛异常   -> 港股键被删，0 条告警
        #   B) 腾讯返回 {}  -> 第二轮推出「恒生指数 +4.61% (阈 3.54%)」
        # 而 4.61% 正是本函数注释点名要防的那个陈旧值（真实 0.18%）。
        # 这不是理论风险：EM_TEST 的港股口径本就不可信（见 marketlib.quotes
        # 里"不要改成 _try_hosts"那段实测），港股在这里**只有腾讯一个可信源**，
        # 所以"腾讯没给数"的正确语义是"港股未知"，而不是"沿用 EM 的数"。
        #
        # 改法：先把港股键全部摘掉，再按腾讯实际返回的逐个放回。
        # 这样"抛异常"与"静默返回空"两条路径收敛成同一种行为。
        for k in hk:
            out.pop(k.split(".")[-1], None)
        try:
            import feeds
            keymap = {"100.HSI":"HSI", "124.HSTECH":"HSTECH"}
            bk = feeds.backup_quotes([keymap[k] for k in hk if k in keymap])
            # ⚠️ bk 可能**不是 dict**。backup_quotes 的契约是返回 dict，但它是
            # 本模块之外的可替换组件（自检会替换它、将来也可能换实现），
            # 而下面 `bk.get(...)` 在 None/list 上直接 AttributeError。
            # 实测（2026-09-27）：桩函数返回 None 时抛
            # "'NoneType' object has no attribute 'get'"，异常虽被外层
            # except 兜住（港股键已在上面摘除，行为仍正确），但日志里留下
            # 一条看着像故障的记录。非 dict 一律当"腾讯没给数"处理。
            if not isinstance(bk, dict):
                bk = {}
            for k in hk:
                r = bk.get(keymap.get(k))
                if r and M.isnum(r.get("f3")):
                    # ⚠️ 恢复时**必须校验来源与时间戳**（2026-10-08 审查）：
                    # 本函数注释说"港股只有腾讯一个可信源"，但恢复循环原样
                    # 采纳任何来源、且**丢弃 _ts** —— 若链路落到新浪/百度
                    # （MAP 里 HSI/HSTECH 也有这两源），或腾讯返回陈旧行，
                    # 就会用上一交易日的价算出"恒生科技 +x%"做异动判定
                    # （对照 marketlib.quotes() 末尾的 _is_ts_stale 闸门）。
                    # 来源非腾讯、或时间戳明显滞后 -> 视为"港股未知"，
                    # 保持上面已摘除的状态（fail-closed），不恢复。
                    _src = r.get("_src")
                    if _src is not None and _src != "tencent":
                        _log(f"alert 港股 {keymap.get(k)} 恢复源非腾讯"
                             f"（{_src}），丢弃", "ALERT")
                        continue
                    _ts = r.get("_ts")
                    if _ts and M._is_ts_stale(_ts, datetime.datetime.now(),
                                              market="hk"):
                        _log(f"alert 港股 {keymap.get(k)} 时间戳陈旧，丢弃", "ALERT")
                        continue
                    out[k.split(".")[-1]] = {"f12":k.split(".")[-1], "f14":r.get("f14"),
                                            "f3":r["f3"], "f2":r.get("f2"),
                                            "_src": _src, "_ts": _ts}
        except Exception as e:
            # 腾讯取数异常：港股键已在上面摘除，这里只需留痕
            _log(f"alert 港股取数失败: {e}", "ALERT")
    return out

def check_index(now, state, quotes_fn=None):
    """指数级异动。返回触发列表。

    ⚠️ `quotes_fn` 是**可选**的已取行情注入点，用于消除同一轮内的重复取数。

    背景：`analyze.run()` 开头已经用 `_quotes(index_secids())` 取过**同一批**
    指数（`STATIC_SIGMA` 的 6 个键与 `index_secids()` 完全相同），而本函数
    又通过 `index_quotes()` 取了第二遍 —— 同一个进程、同一个时刻、同一批
    secid。实测 2026-09-30：`analyze` 一轮 7 个外发请求里，这一个是纯重复
    （占 14%），而 `check_index` 只用到 `f3`（涨跌幅）一个字段，`run()` 的
    行情里已经带了。

    默认 None 时行为与原先**完全一致**（自己取数），所以 `alert.py` 单独
    运行（`__main__` 冒烟）不受影响；只有 `run()` 显式传入时才走复用。
    """
    q = quotes_fn() if quotes_fn is not None else index_quotes()
    # ⚠️ 注入点契约守卫：`quotes_fn` 默认返回 dict，但它是**可替换组件**
    # （自检会换、将来也可能换实现）。非 dict 时下面的 `q.get` 直接
    # AttributeError —— 而本函数在 `run()` 里被裸调用，异常会冒到顶层。
    # 与 `index_quotes()` 里对 `bk` 的 `isinstance(bk, dict)` 守卫同型：
    # 非 dict 一律当"没有行情"，返回空 hits 而不是抛。
    if not isinstance(q, dict):
        _log(f"check_index 注入的行情不是 dict（{type(q).__name__}），"
             f"本轮不判指数异动", "ALERT")
        return []
    hits = []
    # ⚠️ 港股整日休市时**不得**用它的 f3 判 3σ（2026-10-20 自查）：
    # 休市日 f3 是**上一交易日**的涨跌幅，`abs(chg) >= thr` 会把它当成
    # "今日异动"推一条紧急消息 —— 读者会以为港股今天暴跌/暴涨。
    # 与 collect.evaluate 的 IDX3S 循环、analyze.detect 的 hstech 支
    # 是同一个病的三处，2026-10-19 只补了其中一处。
    # 注：`index_quotes()` 末尾已有 `_ts` 陈旧闸门，但它在 `_ts` 缺失时
    # **放行**（`if _ts and ...`）—— 而 `_ts` 正是备用源最可能不给的字段
    # （见 [[market-watch-backup-source-freshness-gap]]）。日历闸门不依赖
    # 任何上游字段，两者互补而非重复。
    # ⚠️ 2026-11-05（P1-13）改用 `hk_session_open`：不只"今天开不开市"，
    # 还要"此刻是否仍在交易时段"——半日市 12:00 后 f3 定格在当日收盘，
    # 再拿它判 3σ 会在下午推一条「当前 X%」的紧急异动，而事实是 12:00
    # 的收盘值。与 detect/collect 同一真源。
    _hk_live = M.hk_session_open(now)
    # ⚠⚠ 2026-11-07（复核 F3）：pending 的年龄上限。
    # 超过这个分钟数的「第一轮」不再算数 —— 两轮确认的本意是
    # 「异动持续到下一次新观测」，而相隔 90 分钟的两个点
    # 不构成“持续”。与下一个观测周期（collect 300s）相比，
    # 15 分钟相当于“连续 3 次尚未回落”，对一个**尖峰型**
    # 3σ 事件已经很宽松。
    PEND_MAX_AGE_MIN = 15

    def _pend_fresh(obs, ts_now, now_):
        """pending 记录的观测时刻是否仍在年龄上限内。

        解析不出来就判“不新鲜”（重新计准）——对这道闸门而言，
        “不能证明是持续的”就不该拿它去确认一条推送。
        """
        _a = M._ts_parse(obs)
        _b = M._ts_parse(ts_now) or now_
        if _a is None or _b is None:
            return False
        return abs((_b - _a).total_seconds()) <= PEND_MAX_AGE_MIN * 60
    # ⚠⚠ 2026-11-07（复核 F1）：A 股行同样需要开市闸门。
    # 这里是紧急推送通道 —— `[A/B 实测]` 11-06 09:26 回放：本函数第二轮
    # 就吐出「上证指数 -3.20%」并直接抢走紧急名额，而那个
    # -3.20% 是**上一交易日**的冻结值。与 `hk_session_open` 同层同形。
    _a_live = M.a_session_open(now)
    # 盘面上下文（2026-11-05，用户反馈「播报的信息太少」）：紧急播报原先
    # 只有一行涨跌幅，读者看不出"是港股独涨还是全线普涨"。这里把本轮
    # **已取到**的行情字段汇成上下文 —— 点位 / 涨跌家数 / 快照时刻 /
    # 其余指数同轮涨跌幅 —— 由 build_urgent 渲染，不新增任何请求。
    _board = []
    for sid, (nm, s_sig) in STATIC_SIGMA.items():
        _is_hk_idx = str(sid).startswith(("100.", "124."))
        if _is_hk_idx and not _hk_live:
            continue
        # A 股日期闸门（F1）：未连续竞价时 f3 可能还是昨日值。
        if not _is_hk_idx and not _a_live:
            continue
        x = q.get(sid.split(".")[-1]) or q.get(sid)
        if not x or not M.isnum(x.get("f3")): continue
        # ⚠️ 港股行必须来自**腾讯**（2026-11-01 修，P3）。
        # `check_index` 改为接受注入的 `quotes_fn`（消除同轮重复取数）后，
        # 它绕过了 `index_quotes()` 里"港股一律用腾讯覆盖、EM 只在最后手段"
        # 的那道守卫 —— 而**注入的 q 正是 `analyze` 用 `quotes()` 取的那份**，
        # 腾讯挂掉时里面的港股行会带 `_src="em"`，其 f2/f3 口径与 A 股不同、
        # 是**有限正数但错误**的值（实测腾讯控股差 +50.0%、恒生指数 +3.7%），
        # `_valid_quotes` 拦不住。拿它去比 3σ 会推出错误的紧急推送。
        # 判据：**明确**标了非腾讯来源才跳过；`_src` 缺失（自检桩、
        # 旧格式）放行 —— 与 `index_quotes` 的 fail-closed 同向，
        # 且不改变桩路径的行为。
        if _is_hk_idx:
            _s = x.get("_src")
            if _s and _s != "tencent":
                _log(f"{nm} 行情来自 {_s}（非腾讯），港股只有腾讯可信 —— "
                     f"本轮跳过其指数异动判定", "ALERT")
                continue
        es = ewma_sigma(sid)
        sig = es if (es and 0.3 < es < 6) else s_sig
        thr = max(3.0*sig, 3.0*s_sig*FLOOR)
        chg = x["f3"]
        _board.append((nm, chg))
        # 事件附带的展示字段（见上方 _board 注释）。全部走 isnum 守卫：
        # 港股行走腾讯源，没有 f104/f105（涨跌家数），不能想当然。
        _ctx = {"last": x.get("f2") if M.isnum(x.get("f2")) else None,
                "n_up": x.get("f104") if M.isnum(x.get("f104")) else None,
                "n_down": x.get("f105") if M.isnum(x.get("f105")) else None}
        if abs(chg) >= thr:
            # 持续性确认：首次超阈值只记 pending，下一轮仍超才推。
            # 否则瞬时尖峰（如一笔大单扫过）就会触发推送。
            #
            # ⚠️⚠️ 「下一轮」必须是**新的观测**，不能是同一帧数据（2026-11-01
            # 修，P3）。相邻两轮 analyze 若复用 collect 写下的**同一份快照**
            # （`_quotes_from_snapshot` 的 360s 窗口内很常见），pending 与确认
            # 用的是**同一个** chg 值 —— 「两轮确认」退化成"单帧数据确认两次"，
            # 而这道闸门存在的全部意义就是防单帧尖峰。
            # `[实测]` 生产 runs.jsonl：895 次复用中有 7 次属于这种（如
            # 10-27 13:20 那一帧被 13:20:11 与 13:25:13 两轮读到）。
            # ⚠️ 判据：把**数据自身的时刻**编进 pending 键 —— 但**只在
            # `_ts` 存在时**才这么做。`_ts` 缺失（自检桩、旧格式、某些
            # 备用源不给该字段）时无从判断"两轮是不是同一帧"，退回原行为：
            # 宁可偶尔把同一帧确认两次，也不要把合法的第二轮确认误杀
            # （`[实测]` 沙箱自检 `[港股闸门]` 的 overkill 断言当场变红）。
            # 快照复用写入的行**都带 `_ts`**（`_quotes_from_snapshot` 注入），
            # 所以真正要防的那个形态仍被覆盖。
            pkey_pre = f"pend_{sid}_{now.date().isoformat()}"
            _tsv = x.get("_ts")
            _obs = str(_tsv) if _tsv else None
            # ⚠️⚠️ 2026-11-06（全量复核 P1-10）：pending 键必须**带方向**。
            #
            # 这道闸门的语义是「**同一个**异动持续到下一轮新观测才推」，
            # 防的是单帧尖峰。而原键只含 sid+日期(+观测时刻)，不含方向，
            # 于是：
            #   10:00 上证 +4.0%  → 记 pending
            #   10:05 上证 -4.0%  → 观测时刻不同、就判「确认通过」→ 推送
            # 可这两次根本不是同一个异动 —— 一个是涨破、一个是跌破，
            # 中间没有任何持续性可言。等于反向尖峰只要出现一次就直接推，
            # 完全没有两轮确认。
            # 实测（沙箱桩两轮不同 chg 符号）确认了这个路径。
            # 修法：方向编进键（"_+"/"_-"），确认只在**同向**的 pending
            # 之间成立；反向尖峰重新进入 pending 等待自己的第二轮。
            _sgn = "+" if chg > 0 else "-"
            pkey_dir = f"{pkey_pre}_{_sgn}"
            pkey = f"{pkey_dir}@{_obs}" if _obs else pkey_dir
            key = f"idx_{sid}_{now.date().isoformat()}"
            # ⚠️ 必须 fired | inflight 同口径（2026-10-08 审查）：
            # 推送成功但 _commit/save_st 落盘失败时，盘上只有 inflight
            # 记着该键 —— 只看 fired 会让同一条异动按 300s 间隔最坏
            # 重复推 96 次。detect() 早就用 `fired | inflight`，这里是
            # 漏改的另一半（与 hourly_slot._live 同因）。
            if key in state.get("fired", []) or key in state.get("inflight", []): continue
            if _obs:
                _pend_today = [k for k in state.get("pending_idx", [])
                               if str(k).startswith(pkey_dir)]
                if not _pend_today:
                    # 同一方向只保留最新一条（旧的观测时刻无意义）
                    state["pending_idx"] = [
                        k for k in state.get("pending_idx", [])
                        if not str(k).startswith(pkey_dir)]
                    state.setdefault("pending_idx", []).append(pkey)
                    _log(f"{nm} 首次超阈值 {chg:+.2f}%（阈 {thr:.2f}%），"
                         f"待下轮确认（观测时刻 {_obs}）", "ALERT")
                    continue
                if all(str(k).split("@", 1)[-1] == _obs for k in _pend_today):
                    # 同一帧数据被读了两轮 —— 不是新观测，不确认（也不重复记）
                    _log(f"{nm} 本轮读到的仍是同一帧行情（观测时刻 {_obs}，"
                         f"快照复用），不算第二轮确认", "ALERT")
                    continue
                # ⚠⚠ 2026-11-07（复核 F3）：pending 必须有**年龄上限**。
                # 上面只比「有没有同向 pend」与「观测时刻是否相同」，
                # **不看 pending 有多旧**，确认后也不摘除。于是单日的
                # pending 会一直挂到收盘，后面任何一个顺势尖峰都能
                # 拿它当“第一轮”直接确认 —— 两轮确认变成单帧确认。
                # `[A/B 实测]` 上证阈 2.70%：
                #   10:00 +4.00 → pend；10:05 -4.00 → 另记；10:10 -4.20 → 确认（正确）
                #   11:30 +4.10（**仅一帧**）→ 直接推「上证指数涨」❌
                #   用的是 90 分钟前、中间已经反转过的那条 pending。
                # 修法：超过 PEND_MAX_AGE_MIN 分钟的 pending 视为作废，当做首次记录；
                # 确认后也摘掉它 —— 同一次尖峰只能被消耗一次。
                _fresh = [k for k in _pend_today
                          if _pend_fresh(str(k).split("@", 1)[-1], _tsv, now)]
                if not _fresh:
                    state["pending_idx"] = [
                        k for k in state.get("pending_idx", [])
                        if not str(k).startswith(pkey_dir)]
                    state.setdefault("pending_idx", []).append(pkey)
                    _log(f"{nm} 超阈值 {chg:+.2f}%，但此前的 pending 已超"
                         f" {PEND_MAX_AGE_MIN} 分钟（或无法定年龄）"
                         f"—— 重新计算确认，待下轮新观测", "ALERT")
                    continue
                # 确认成立 —— 消耗掉这些 pending，避免后续顺势尖峰
                # 再拿它们当“第一轮”。
                state["pending_idx"] = [
                    k for k in state.get("pending_idx", [])
                    if not str(k).startswith(pkey_dir)]
                hits.append({"type":"index","name":nm,"chg":chg,"thr":thr,
                             "sigma":sig,"key":key,
                             "dir":"涨" if chg>0 else "跌",
                             "obs": str(_tsv)[:16] if _tsv else None,
                             **_ctx})
            else:
                # 旧行为（无 `_ts` 可判）：首次记 pending，第二次即确认
                if pkey not in state.get("pending_idx", []):
                    state.setdefault("pending_idx", []).append(pkey)
                    _log(f"{nm} 首次超阈值 {chg:+.2f}%（阈 {thr:.2f}%），"
                         f"待下轮确认", "ALERT")
                    continue
                hits.append({"type":"index","name":nm,"chg":chg,"thr":thr,
                             "sigma":sig,"key":key,
                             "dir":"涨" if chg>0 else "跌",
                             "obs": None,
                             **_ctx})
        else:
            # 回落则清除 pending，避免把不连续的两次尖峰拼成"持续"
            state["pending_idx"] = [
                k for k in state.get("pending_idx", [])
                if not str(k).startswith(f"pend_{sid}_{now.date().isoformat()}")]
    # 盘面上下文挂到每个命中事件上（排除它自己，避免同一行重复两遍）。
    # 在循环外统一挂：`_board` 要在所有指数都看过之后才是完整的。
    for _h in hits:
        _h["board"] = [(n2, c2) for n2, c2 in _board
                       if n2 != _h.get("name")]
    return hits

# 板块资金异动扫描的板块范围。顺序即优先级（同一轮里先命中先推）。
#
# ⚠️⚠️ 2026-10-22 用户问「紧急事件触发条件是不是不够完善？对应的板块也不全」
# —— 复核属实，原实现有**三个**漏报方向：
#
#  ① **概念板块整类没扫**。原实现只查 `m:90+t:2`（行业，496 个），而
#     sectors.json 的 12 个关注板块里**有 5 个是概念**（存储芯片 / CPO概念 /
#     AI应用 / 创新药 / 白酒 / 消费风格）。实测 2026-10-22：CPO概念主力
#     净流出 110 亿、占成交额 -10.4%，**远超 B 档的 50 亿**，因为不在行业
#     列表里，一条紧急推送都没发。
#
#  ② **行业板块的 15% 占比门槛几乎不可能达到**。占比 = 主力净额/成交额，
#     一个板块总成交里主力占三成以上本身就罕见，而成交额越大的板块越难
#     越过 15%。实测 2026-10-22 全场 496 个行业板块：**B 档命中 0 个**，
#     而「≥50 亿 且 ≥8%」有 3 个（通信 -69亿/-8.3%、通信设备 -65亿/-8.5%、
#     通信网络设备及器件 -53亿/-10.2%）。历史 5 条紧急推送里板块资金只有
#     1 条（2026-09-28 通信设备 -118.5 亿/-15.1%），比例正好印证这个门槛
#     把绝大多数真异动挡在了门外。
#
#  ③ **概念板块数量（516）比行业还多**，且同日「≥50亿 且 8~15%」有 9 个
#     （科技风格 -153亿/-9.3%、通信技术 -151亿/-8.1%、CPO概念 -110亿/-10.4%）。
#     只扫行业等于把半边市场丢在视野外。
#
# 故改为：**两类板块都扫**，阈值按「是否在关注清单里」分档 ——
#   · 关注清单（sectors.json 的 watch，12 个，跨行业/概念）：50亿 + 8%
#     （`watch_fund_pct`，与 analyze.detect 的 watch_* 同一个数）
#   · 其它全部板块：50亿 + 15%（`fund_pct`，保持原广度）
# 为什么不统统降到 8%：概念板块里大量是**同主题的重复板块**（通信 / 通信设备 /
# 通信网络设备及器件 / 通信技术 / 光通信模块 / F5G概念 是同一件事的六个切面），
# 8% 全量会在一轮里产出十几条近义事件、把一条紧急消息堆成一堵墙。关注清单是
# 用户自己圈定的口径，用它来分档既补齐了盲区，又不制造同义噪音。
#
# ⚠️ 关注板块的**去重键与事件类共用**（`watch_<secid>`，见下方 key 的构造）：
# 同一板块若事件类已推过，紧急通道不再推；反之紧急先推，detect() 的
# `busy = fired | inflight` 会把事件类那条压掉。**一个事实只发一条消息**。
# 代价是紧急消息采用模板文（不过 AI），但那正是"紧急"的含义 —— 快、准、可核对。
_SECTOR_SCANS = ("m:90+t:2", "m:90+t:3")   # 行业、概念


def _watch_codes():
    """sectors.json watch 清单的板块短码集合（"BK1036" 形态）。

    读取失败返回**空集**（= 退回"全部按 15% 判"的原行为），不抛 ——
    紧急判定挂掉比少几条关注板块告警严重得多。
    """
    out = set()
    try:
        for _seg, _it in M._cfg_items("watch"):
            _c = M._cfg_id(_it, "code")
            if _c:
                out.add(M._sid(_c).split(".")[-1])
    except Exception as e:
        _log(f"读取 watch 清单失败({e})，紧急通道全部按 fund_pct 判", "ALERT")
    return out


def _sector_rows(fs):
    """取一类板块的**全量**列表（含两条 EM 主机故障转移 + 形状校验）。

    失败返回 []（不是 None）—— 调用方逐类累加，一类挂掉不该让另一类也丢。

    ⚠️⚠️ 必须**翻页取全**，不能只取 pz=500 的第一页。这是原实现的一个
    真实盲区（2026-10-22 实测）：概念板块有 **516** 个，pz=500 只回 500 行，
    按 `fid=f62` **降序**排序时被截掉的 16 个正是**流出最猛的一批** ——
      融资融券 -396亿 / MSCI中国 -273亿 / 富时罗素 -254亿 / 深股通 -236亿
      科技风格 -153亿(占比-9.3%) / 通信技术 -151亿(占比-8.1%)
    而这几条恰恰是"紧急"最该报的（外资/融资盘整体撤离）。只取一页时，
    **跑得最凶的那批永远看不见**，且没有任何报错 —— 纯静默漏报。
    行业 496 个恰好塞得下一页，所以这个问题只在概念侧显形。

    300 行注释说 pz=100 会漏 368 个流出板块 —— 同一个病，当时只把
    pz 调大到 500 就收手了，没有意识到板块总数会随行情膨胀越过分页边界。

    ⚠️ 形状校验必须写全，同 check_market_fund（下方）的写法。原先只判
    `not d or not d.get("data")`，然后裸取 d["data"]["diff"]：
      · data 无 diff 键        -> KeyError
      · data 是 list / str     -> TypeError（用字符串下标取列表）
      · diff 是 None           -> AttributeError（None.values()）
      · 整包是 list            -> AttributeError（list.get）
    实测 9 种形状里 5 种抛异常。异常沿 analyze.run 的 except 冒出去，
    只留一行 "ALERT EXC" —— **整轮紧急推送判定全部失效**（同轮的指数
    异动与大盘资金告警一起被跳过），而这一轮不会留下任何失败记忆，
    下一轮重算再抛，循环往复。
    """
    # 走 marketlib.clist()：它已经处理了翻页、diff 的 list/dict 两种形状、
    # 两主机故障转移与 budget_s 总耗时上限。这里再手写一份循环等于把
    # 那三条语义各抄一遍（记忆 [同一真值抄两份] 的坑）。
    # pz=500 + max_pn=8：616 个板块在两页内取完（第三页起是保险）。
    # ⚠️ `max_pn` 从 3 提到 8（2026-10-22）：3 只够 pz=500 **不被截断**时用。
    # 服务端可能把 pz 截到自己的上限（实测明文 HTTP 主机对 pz=500 只返
    # 100 行、翻页偏移也按 100 算），那时 3 页只拿到 300 个板块 ——
    # 板块资金异动会**静默漏掉 40% 的板块**（漏报，不是误报，更难发现）。
    # 8 页覆盖 100×8=800 > 616。**代价为零**：clist 按服务端 `total`
    # 判终止，正常路径第 2 页就 break。
    # budget_s 防止两台主机同时"能连但慢"时一个函数吞掉整轮
    # （2026-10-21 实测一条链可烧 3 分钟）。
    return M.clist(fs, "f12,f14,f3,f6,f62,f184", pz=500, max_pn=8,
                   fid="f62", budget_s=45)


def check_sector_fund(now, state):
    """板块资金异动：行业 + 概念全扫；关注板块 50亿+8%，其它 50亿+15%

    范围与分档见 `_SECTOR_SCANS` 上方注释（2026-10-22 用户反馈后重做）。

    ⚠️ 占比阈值必须读**配置**（`watch_fund_pct` / `fund_pct`），不能在调用处
    写字面量当"默认值"。threshold() 是 `config().get(key, TH.get(key, default))`
    —— 只要 sectors.json 里**配了**这个键，调用处的字面量**永远不参与**。
    第一版在这里想给行业降档、把 fund_pct 的字面默认写成 8，而配置里
    fund_pct=15，实测改动完全没生效（命中 0 条，与改前一样）。
    `[实测]` 2026-10-22 11:45 用真实数据跑：修正后才命中。
    """
    hits = []
    # ⚠⚠ 2026-11-07（复核）：f184 是**当日累计**净流入。盘前
    # （09:25~09:29）拿到的是**上一交易日全天的冻结值**，拿它跟
    # 「今日资金异常」的阈值比就是用昨天的事说今天。
    # 同一个缺陷在 check_index 里已经修过（F1），但**只修了一条
    # 通道** —— should_alert 一共调三条，另两条一行闸门都没有。
    # `[实测]` 09:26 / 09:29（a_session_open=False）本函数仍产出 2 条事件，
    # 与 10:30 无异；而 trading_guard 在 09:26/09:29 返回 True（允许告警），
    # 挡不住。
    if not M.a_session_open(now):
        return hits
    _watch = _watch_codes()
    # ⚠️ 2026-10-28 加：单行异常**计数**，整轮结束后若有则记一条汇总。
    # 原先裸 `except Exception: continue` 完全静默 —— 单行容错是对的，
    # 但如果是**系统性**故障（如某字段类型整体变了、`_sid`/`threshold`
    # 对所有行都抛），告警通道会整体静默死掉而日志里一个字都没有。
    # 只记汇总不逐行记：逐行会在字段整体变更时刷满 watch.log（2MB 单代）。
    _bad_rows = 0
    for _fs in _SECTOR_SCANS:
        for x in _sector_rows(_fs):
            if not isinstance(x, dict):
                continue
            try:
                f62 = x.get("f62"); f184 = x.get("f184")
                if not M.isnum(f62) or not M.isnum(f184): continue
                # f62/f184/阈值都必须先取值再判：`_pct` 依赖 `_f12`（是不是
                # 关注板块），而 `_f12` 在下面才取 —— 顺序在这里是**语义**
                # 而非风格，调换会让分档失效。
                # 金额门槛（亿元）走配置；占比门槛按"是否关注板块"二选一。
                # ⚠️ `fund_pct` / `watch_fund_pct` 都在 sectors.json 里配了值，
                # 所以下面两个字面量只是"配置整个缺失"时的安全网。
                _f12 = x.get("f12")
                if not _f12: continue
                _is_watch = _f12 in _watch
                _pct = M.threshold("watch_fund_pct", 8.0) if _is_watch \
                    else M.threshold("fund_pct", 15)
                if (abs(f62) < M.threshold("fund_abs", 50)*1e8
                        or abs(f184) < _pct): continue
                # f12/f14 用 .get()：主机少返一个字段时裸下标 KeyError
                # （虽在 try 内会被吞，但那一条告警就静默丢了 —— 换成 .get 后
                # 缺 f14 只是名字难看，缺 f12 才跳过，告警本身不丢）
                # ⚠️ 关注板块必须与 analyze.detect 用**同一个键** `watch_<secid>`：
                # 两个通道都是"一个板块一天一条"，键不同就会各推一条 ——
                # 同一条资金异动在群里出现两次（与 urgent 双推同型的坑）。
                # 共用键后：任一方推过，另一方的 fired|inflight 就把它压掉。
                key = (f"watch_{M._sid(_f12)}" if _is_watch
                       else f"sec_{_f12}_{now.date().isoformat()}")
                # ⚠️ 必须 fired | inflight 同口径（2026-10-08 审查）：
                # 推送成功但 _commit/save_st 落盘失败时，盘上只有 inflight
                # 记着该键 —— 只看 fired 会让同一条异动按 300s 间隔最坏
                # 重复推 96 次。detect() 早就用 `fired | inflight`，这里是
                # 漏改的另一半（与 hourly_slot._live 同因）。
                if key in state.get("fired", []) or key in state.get("inflight", []): continue
                # f3 也要守卫：同一行的 f62/f184 已有守卫，漏了 f3 会导致
                # 下游 build_urgent 的 f"{chg:+.2f}" 抛 ValueError，整条紧急推送失败
                chg = x.get("f3")
                if not M.isnum(chg): chg = 0.0
                hits.append({"type":"sector","name":x.get("f14") or _f12,"chg":chg,
                             "inflow":f62/1e8,"pct":f184,"key":key,
                             "dir":"流入" if f62>0 else "流出"})
            except Exception:
                _bad_rows += 1     # 单条坏数据不应废掉整批告警（汇总另记）
    if _bad_rows:
        _log(f"板块资金扫描有 {_bad_rows} 行解析失败（已逐行跳过）—— "
             f"少量属正常坏数据；若接近总行数则是字段口径整体变了，"
             f"告警通道会整体静默", "ALERT")
    return hits

def check_market_fund(now, state, quotes_fn=None):
    """大盘级：净流入≥500亿 或 ≤-1000亿，或净流入占成交额比 |≥4.5%|

    三条阈值原先全部硬编码，连 TH 都没进，改 sectors.json 无从下手；
    第三条（占比）更是没写进 docstring —— 现在统一走 threshold()，
    默认值保持不变，行为与之前一致，但可配置、可追溯。

    ⚠️ `quotes_fn` 与 `check_index` 同款注入点。本函数只需要 `1.000001`
    的 `f62`(主力净额) 与 `f6`(成交额)，而 `quotes()` 的**默认字段里就有
    这两个**（见 marketlib.quotes 的 fields 参数），且 `analyze.run()` 开头
    已经把 `1.000001` 连同这两个字段取回来了。`[实测]` 2026-09-30：不注入时
    本函数会为同一个标的、同样两个字段再打一次 `ulist.np` —— 一轮 7 个外发
    请求里的又一处纯重复。
    """
    # ⚠⚠ 2026-11-07（复核）：与 check_sector_fund 同因同形。
    # f62（主力净额）/f6（成交额）都是**当日累计**；盘前拿到的是昨天全天值，
    # 拿它跟「今日净流入≥500 亿」这类阈值比，得出的是昨天的结论。
    # 放在最前面：连 quotes_fn 都不调，不浪费一次取数。
    if not M.a_session_open(now):
        return []
    d = None
    if quotes_fn is not None:
        # 复用已取到的行情：按 `1.000001` 优先、短码 `000001` 兜底
        # （与 detect() 里 _pq 的取键约定一致 —— quotes() 按完整 secid 建键，
        #  只在末尾补短码别名）。
        _q = quotes_fn() or {}
        _x = _q.get("1.000001") or _q.get("000001")
        if isinstance(_x, dict) and M.isnum(_x.get("f62")):
            d = {"data": {"diff": [_x]}}
    # 同上：走健康度排序，不再硬编码「push2delay 主 + push2test 备」。
    for host in (M._ordered_real_em_hosts() if d is None else ()):
        d = cj(f"{host}/ulist.np/get?secids=1.000001&fields=f12,f14,f62,f6&ut={UT}&fltt=2&invt=2")
        # isinstance 是必需的：`d and d.get(...)` 在 d 是 list/str/int 时
        # 直接 AttributeError，连试下一台主机的机会都没有。
        # ⚠️ 这里曾写成
        #     `isinstance(d, dict) and d["data"].get("diff") if isinstance(d.get("data"), dict) else False`
        # —— Python 把它解析成 `(A and B) if C else False`，于是**条件 C 最先
        # 求值**，`d.get(...)` 在 d 是 list/str/int 时先炸；而 B 里的 `d["data"]`
        # 是裸下标，data 缺失时又 KeyError。写成平铺的 and 链才真正安全。
        if (isinstance(d, dict) and isinstance(d.get("data"), dict)
                and d["data"].get("diff")):
            break
    hits = []
    # ⚠️ 形状校验必须与 check_sector_fund（上方）一致且写全。原先只判真值，
    # 实测 13 种形状里 6 种抛异常（2026-09-26）：
    #   整包 list/str/int -> AttributeError（list.get）
    #   data 是 list/str  -> AttributeError
    #   条目非 dict       -> AttributeError（'str' has no attribute 'get'）
    # 后果同 check_sector_fund：异常冒到 analyze.run 的 except 只留一行
    # "ALERT EXC"，**整轮紧急推送判定全部失效**，且不留失败记忆、下轮再抛。
    if not isinstance(d, dict) or not isinstance(d.get("data"), dict):
        return hits
    # 同 index_quotes：diff 可能是 dict，[0] 会 KeyError（键是 "0" 而非 0）
    _diff = d["data"].get("diff")
    if isinstance(_diff, dict):
        _diff = list(_diff.values())
    if not isinstance(_diff, list) or not _diff:
        return hits
    x = _diff[0]
    if not isinstance(x, dict):
        return hits
    # 必须用 isnum 而非 `or 0`：后者只兜住 None/缺失，
    # 接口偶发返回 "-" 时是字符串，f62/f6 会抛 TypeError
    # （被 should_alert 的 except 吞成一行 ALERT EXC，大盘资金告警整体失效）
    f62 = x.get("f62"); f6 = x.get("f6")
    if not M.isnum(f62) or not M.isnum(f6): return hits
    if not f6: return hits
    ratio = f62/f6*100
    IN  = M.threshold("market_fund_in", 500)        # 亿
    OUT = M.threshold("market_fund_out", -1000)     # 亿
    PCT = M.threshold("market_fund_pct", 4.5)       # 占成交额 %
    trig = (f62 >= IN*1e8 or f62 <= OUT*1e8 or abs(ratio) >= PCT)
    if not trig: return hits
    key = f"mkt_{now.date().isoformat()}"
    # 同 check_index/check_sector_fund：fired | inflight 双读（见上注释）
    if key in state.get("fired", []) or key in state.get("inflight", []):
        return hits
    hits.append({"type":"market","name":"全市场","inflow":f62/1e8,
                 "ratio":ratio,"key":key,
                 "dir":"流入" if f62>0 else "流出"})
    return hits

def trading_guard(now):
    """开盘/尾盘保护（阈值统一在 marketlib.TH）"""
    if _in_guard(now): return False, "保护时段不告警"
    return True, ""

def should_alert(now, state, news_fn=None, quotes_fn=None):
    """主入口：返回 (是否推送, 事件列表, 新闻佐证)

    `quotes_fn` 透传给 `check_index`，见那里的说明（消除同轮重复取数）。
    """
    ok, why = trading_guard(now)
    if not ok: return False, [], why
    ev = (check_index(now, state, quotes_fn)
          + check_sector_fund(now, state)
          + check_market_fund(now, state, quotes_fn))
    if not ev: return False, [], "无异动"
    # 新闻佐证（用户硬要求）
    # 原先写成 `corroborated, news_txt = [], ""`，而 news_txt 全文未再出现
    # （佐证文本由 build_urgent 从事件的 news 字段现拼）。删掉。
    corroborated = []
    if news_fn:
        try:
            ns = news_fn()
        except Exception as e:
            _log(f"新闻源异常({e})，按无佐证放行", "ALERT")
            ns = None
        if ns is None:
            corroborated = ev          # 源不可用 ≠ 无佐证，放行（fail-open）
        elif not ns:
            # 源可用但返回空（挂掉/限流）。真异动不该因快讯缺失而漏报。
            _log("新闻源返回空，按无佐证放行", "ALERT")
            corroborated = ev
        else:
            try:
                import news as NEWS
                for e in ev:
                    h = NEWS.related_news(e["name"], ns, within_min=120, max_n=2)
                    m = NEWS.macro_news(ns, within_min=120, max_n=2)
                    if h or (m and e["type"] in ("index","market")):
                        e["news"] = h or m
                        corroborated.append(e)
            # ⚠️ 用 exc 而非 e：上面 `for e in ev:` 的 e 是事件对象，
            # 写成 `as e` 会遮蔽它（当前无害，但是隐患）。
            except Exception as exc:
                _log(f"新闻佐证匹配异常({exc})，放行", "ALERT")
                corroborated = ev
            # ⚠️ 2026-10-28 加：**一条都没佐证上**必须留痕。
            # 这是"告警被静默吞掉"的最后一种形态 —— 新闻源可用（返回了
            # 非空列表）、但没有任何快讯命中任何一个事件的关键词，于是
            # `corroborated` 为空、`should_alert` 返回 False，**一条日志
            # 都没有**。源故障/源返回空那两条都有 fail-open 日志，唯独
            # 这条"源健康但与事件无关"既不放行也不留痕，排查时表现为
            # "异动明明触发了却没推"，而日志里查不到任何线索。
            if ev and not corroborated:
                _log(f"新闻源可用（{len(ns)} 条）但 {len(ev)} 个事件均无"
                     f"相关快讯佐证 —— 按设计不推（非故障）", "ALERT")
    else:
        corroborated = ev
    return (len(corroborated) > 0), corroborated, ""

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/
    now = datetime.datetime.now()
    st = {"fired": []}
    import news as NEWS
    ok, ev, why = should_alert(now, st, lambda: NEWS.fetch_news(80))
    print(f"时间 {now:%H:%M}  触发={ok}  {why}")
    for e in ev:
        if e["type"]=="index":
            print(f"  [指数] {e['name']} {e['chg']:+.2f}% 阈值{e['thr']:.2f}% σ={e['sigma']:.2f}")
        elif e["type"]=="sector":
            print(f"  [板块] {e['name']} {e['dir']}{e['inflow']:+.1f}亿 占比{e['pct']:+.1f}%")
        else:
            print(f"  [大盘] {e['name']} {e['dir']}{e['inflow']:+.1f}亿 占比{e['ratio']:+.2f}%")
        for n in e.get("news", [])[:2]:
            print(f"        新闻 {n['time']:%H:%M} {n['title'][:44]}")
