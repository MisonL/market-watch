#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多数据源行情（东财为主，腾讯/新浪为备）。
东财 push2* 接口会因请求量过大封 IP，此时自动切换备用源。

字段映射（实测确认）：
  腾讯 A股 sh000001: [1]名称 [3]现价 [4]昨收 [5]今开 [31]涨跌 [32]涨跌% [33]最高 [34]最低
  腾讯 港股 hkHSI:   [1]名称 [3]现价 [4]昨收 [5]今开 [6]成交额
  新浪 s_sh000001:   名称,现价,涨跌额,涨跌%,成交量(手),成交额(万)
  新浪 rt_hkHSI:     code,名称,今开,昨收,最高,最低,现价,涨跌额,涨跌%,...
"""
import subprocess, json, os, sys

# 允许 MW_DIR 覆盖：测试可在沙箱里跑而完全不碰线上状态文件
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/
import marketlib as M

TENCENT = "https://qt.gtimg.cn/q="
SINA = "https://hq.sinajs.cn/list="
SINA_REF = "https://finance.sina.com.cn"

# secid -> (腾讯代码, 新浪代码, 名称)
MAP = {
    # ⚠️ 新浪列从 s_ 简版（6 字段、无时间戳）切换到全量格式（2026-10-15
    # 复核 P2-4）：简版没有行情时间戳，quotes() 的新鲜度闸门对它从不生效
    # —— 而 sina 是 A股备用链第二顺位，限流期返回昨收会被当成现价推送。
    # 全量格式 34 字段：p[2]=昨收 p[3]=现价 p[30]=日期 p[31]=时间
    # （实测 "2026-10-15"/"15:35:27"，与 _ts_parse 的 "%Y-%m-%d %H:%M:%S"
    # 直接匹配；简版 px/chg 与全量 p[3]/(p[3]/p[2]-1) 已实测对齐）。
    "000001": ("sh000001", "sh000001", "上证指数"),
    "399001": ("sz399001", "sz399001", "深证成指"),
    "399006": ("sz399006", "sz399006", "创业板指"),
    "000688": ("sh000688", "sh000688", "科创50"),
    "000300": ("sh000300", "sh000300", "沪深300"),
    "000905": ("sh000905", "sh000905", "中证500"),
    # ⚠️ 港股指数用 **r_ 前缀（实时）**：hkHSI/hkHSTECH 是延迟 15 分钟的行情
    # （与 marketlib._TX_HK 同因，2026-10-08 审查），备用源若用它，
    # 13:00–13:15 会被 quotes() 的新鲜度闸门整体丢弃。
    "HSI":    ("r_hkHSI",  "rt_hkHSI",    "恒生指数"),
    "HSTECH": ("r_hkHSTECH","rt_hkHSTECH","恒生科技指数"),
    "00700":  ("r_hk00700","rt_hk00700",  "腾讯控股"),
    "00981":  ("r_hk00981","rt_hk00981",  "中芯国际"),
    "09988":  ("r_hk09988","rt_hk09988",  "阿里巴巴-W"),
}
# 反向：东财 f12 可能是 00700 或 HSTECH 等

def _curl(url, ref=None, timeout=12, enc="auto"):
    """取文本。enc='auto' 自动判编码（gbk 站点 vs utf-8 站点）。

    注意：这里**必须**过一遍 M.RL（限流 + 日配额）。备用源是"主源被封锁时"
    才走的路，恰恰是请求量最容易失控的场景；原先这条路径完全不限流，
    而它是 backup_quotes 逐键降级的底层调用 —— MAP 有 11 个键，
    腾讯失败后最坏 3×11=33 次串行请求，既不受 min_interval 节流、
    也不计入 2000/天的配额，等于在防封 IP 的链条上开了个口子。
    """
    # ⚠️ host 必须在 try **之外**初始化。原先它只在 try 内赋值，
    # 而下面第 65/85 行在 try 之外使用它 —— 若第 50 行本身抛异常
    # （url 非字符串、split 越界），host 处于**未绑定**状态，
    # `M.RL.fail(host)` 抛 NameError 被 except 吞掉，主机级熔断永远不会触发。
    # 实测（静态检查 reportPossiblyUnboundVariable 命中 L65/L85）。
    host = ""
    try:
        host = url.split("/")[2] if "//" in url else ""
        if not M.RL.wait(host):
            # ⚠️ `wait()` 返回 False 有**两种完全不同的原因**，原先打同一句话：
            #   ① 主机熔断（该源近期连续失败，冷却中）
            #   ② 日配额耗尽（本地 2000/天 用尽）
            # 前者是"这个源坏了"，后者是"我们请求太多" —— 排查方向相反。
            # 而且这是**持续状态**：MAP 有 11 个键，每个键每轮都撞一次，
            # `[实测]` 日均约 85 行（单日最高 160 行），全是同一批 URL
            # 反复刷屏，而 watch.log 是 2MB 单代轮转，真实诊断日志被挤掉。
            # 改为按**主机**按日去重 + 区分原因。
            # ⚠️ host_open() 返回 True 表示**主机可用** —— 判据要取反。
            # 写成 `if host_open` 会把两种原因**正好说反**（实测踩过：
            # 熔断状态下报出"日配额耗尽"，把排查引向完全相反的方向）。
            _why = ("日配额耗尽" if M.RL.host_open(host) else "主机熔断中")
            M._daily_log_once(
                host, f"bakskip-{_why}",
                f"备用源跳过（{_why}）: {host} —— 本日同主机不再重复"
                f"（首次触发 URL: {url[:60]}）")
            return ""
    except Exception:
        pass            # 限流器自身异常不该让取数整条断掉
    args = ["curl","-s","--max-time",str(timeout),"-A",M.UA]
    if ref: args += ["-H", f"Referer: {ref}"]
    args.append(url)
    # curl 的 --max-time 覆盖不了 DNS 卡死等场景，再加一层进程级超时兜底：
    # 子进程挂住会让整轮 cron 永不退出，下一轮 5 分钟后又叠上来
    _failed_reported = False
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        M.log(f"curl 超时({timeout+5}s): {url[:60]}", "FEED")
        try: M.RL.fail(host)
        except Exception: pass
        # ⚠ 2026-11-10（Codex P3）：这里已经报过一次失败，
        # 下面的“空 body”分支**不能再报一次** ——
        # 否则一次超时计 2 次，三次超时就凑满 HOST_FAIL_THRESHOLD=6，
        # 主机熔断被提前触发。
        _failed_reported = True
        raw = b""
    else:
        raw = r.stdout
    # ---- 网络异常兜底：直连拿到空 body 时，走 代理 → firecrawl ----
    # 与 marketlib.cj() 用**同一套**降级链，保证两条取数路径行为一致。
    # `[实测]` 2026-09-29：东财直连全空，而经代理 0.19s 就拿到数据。
    # ⚠️ 判据是"空 body"而不是"抛异常" —— 备用源被拦时返回的正是**空**，
    # 那是这条路径最常见的失败形态，只兜异常等于没兜。
    if not raw.strip():
        # ⚠️ 直连空 body = **主机级失败，先回报 RL.fail**（2026-10-08 审查）：
        # 原实现把 ok/fail 的回报放在两级兜底**之后**，于是"直连空、代理救回"
        # 这条最常见路径**从不回报失败** —— 主机熔断永不触发，每轮白付一次
        # 注定失败的直连（生产 qt.gtimg.cn 实测如此）。与 cj() 的约定相反
        # （cj 先 RL.fail(host) 再 _fallback_get）。
        if not _failed_reported:
            try: M.RL.fail(host)
            except Exception: pass
        # 代理与 firecrawl 是**彼此独立**的两级（原先 `and M.PROXY` 把 firecrawl
        # 一并挂在代理开关下 —— MW_PROXY="" 时会连 firecrawl 都不试，与
        # marketlib._fallback_get 的行为不一致）。改为让 proxy_get 自己因
        # PROXY 为空返回 None，再落到 firecrawl。
        _pt = M.proxy_get(url, ref=ref, timeout=timeout, enc=enc)
        if _pt:
            M._daily_log_once(host or url, "curl-proxy",
                              f"直连空响应，经代理兜底成功: {url[:60]}"
                              f"（本日首次，同源后续不再重复）")
            return _pt
        _ft = M.firecrawl_get(url, timeout=M.FIRECRAWL_TIMEOUT)
        if _ft:
            M._daily_log_once(host or url, "curl-fc",
                              f"直连与代理均失败，经 firecrawl 兜底成功: {url[:60]}"
                              f"（本日首次，同源后续不再重复）")
            return _ft
    else:
        try: M.RL.ok()
        except Exception: pass
    if enc != "auto":
        return raw.decode(enc, "ignore")
    # 自动检测：先试 utf-8，失败再 gbk
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "ignore")

def from_tencent(codes):
    """codes: 腾讯代码列表。返回 {tencent_code: dict}"""
    out = {}
    if not codes: return out
    txt = _curl(TENCENT + ",".join(codes))
    # ⚠️ 空响应必须留痕（2026-10-15 复核 P2-1/P2-3）：_curl 在**主机熔断 /
    # 日配额耗尽 / 三级兜底全败**时返回 "" 且**根本没发请求**，但排在最前的
    # 腾讯原先是四种失败形态（空 body、网关 HTML、字段<35、float 失败）
    # **一律静默返回 {}** —— health() 只报一句「❌ tencent」，无从区分
    # "源坏了"与"我们没配额去探"。同文件 from_baidu/from_ths 早已记响应前
    # 80 字，这条标准没落到腾讯/新浪上。_daily_log_once 按日去重防刷屏。
    if not txt.strip():
        M._daily_log_once("tencent", "empty",
                          "腾讯空响应（熔断/配额/兜底全败，见 bakskip 行）"
                          f" code={','.join(codes)[:60]}（本日首次）")
        return out
    for line in txt.strip().split("\n"):
        if "=" not in line or '"' not in line: continue
        code = line.split("=")[0].replace("v_", "").strip()
        p = line.split('"')[1].split("~")
        if len(p) < 35: continue
        try:
            out[code] = {
                "name": p[1], "px": float(p[3]),
                "chg": float(p[32]) if p[32] else 0.0,
                "preclose": float(p[4]) if p[4] else 0.0,
                "high": float(p[33]) if p[33] else 0.0,
                "low": float(p[34]) if p[34] else 0.0,
                # 行情时间戳（p[30]，格式 YYYYMMDDHHMMSS）。
                # 备用源原先**完全不输出时间戳**，下游 _valid_quotes 只校验
                # "有限正数"，无法区分"今天的收盘价"与"上周五的收盘价"。
                # 腾讯在休市/限流时会返回上一交易日的价 —— 那会被当成现价
                # 写进推送（"上证 3700.00 +33.00%"这种用昨天价格配今天涨跌幅）。
                "_ts": p[30] if len(p) > 30 else "",
            }
        except (ValueError, IndexError):
            continue
    if not out:
        M._daily_log_once("tencent", "parse-fail",
                          f"腾讯有响应但零行可用（改版/风控页？），响应前 80 字: "
                          f"{txt[:80]!r}（本日首次）")
    return out

def from_sina(codes):
    """codes: 新浪代码列表（s_sh000001 / rt_hkHSI 等）"""
    out = {}
    if not codes: return out
    txt = _curl(SINA + ",".join(codes), SINA_REF)
    # 空响应留痕（与 from_tencent 同标准，2026-10-15 复核 P2-1/P2-3）
    if not txt.strip():
        M._daily_log_once("sina", "empty",
                          f"新浪空响应（熔断/配额/兜底全败）"
                          f" code={','.join(codes)[:60]}（本日首次）")
        return out
    for line in txt.strip().split("\n"):
        if '="' not in line: continue
        code = line.split("=")[0].replace("var hq_str_", "").strip()
        p = line.split('"')[1].split(",")
        try:
            if code.startswith("rt_hk"):       # 港股
                # _ts：p[17]=日期("2026/10/15") p[18]=时间("16:09:30")，
                # 拼接后与 _ts_parse 的 "%Y/%m/%d %H:%M:%S" 直接匹配
                # （实测 2026-10-15）。这是港股备用链唯一的时效依据。
                out[code] = {"name": p[1], "px": float(p[6]),
                             "chg": float(p[8]),
                             "high": float(p[4]), "low": float(p[5]),
                             "_ts": f"{p[17]} {p[18]}" if len(p) > 18
                                    and p[17] and p[18] else ""}
            elif len(p) >= 30:                 # A股全量格式（34 字段）
                # p[2]=昨收 p[3]=现价 p[4]=高 p[5]=低 p[30]=日期 p[31]=时间。
                # chg 由昨收现算（简版的 p[3] 在全量格式里是现价，不能照搬）。
                _px, _pre = float(p[3]), float(p[2])
                out[code] = {"name": p[0], "px": _px,
                             "chg": round((_px/_pre - 1)*100, 2) if _pre else 0.0,
                             "high": float(p[4]), "low": float(p[5]),
                             "preclose": _pre,
                             "_ts": f"{p[30]} {p[31]}" if len(p) > 31
                                    and p[30] and p[31] else ""}
            else:                              # 简版兜底（6 字段，无时间戳）
                # ⚠️ 2026-11-06（P1-1）：必须显式标记「本行无时间戳」。
                # 原实现既不写 `_ts` 也不标记，而 `quotes()` 的新鲜度闸门
                # 判据是 `if v.get("_ts") and _is_ts_stale(...)` ——
                # **没有 `_ts` 就整条跳过检查**，于是上一交易日收盘价
                # 原样当现价进推送。带上 `_ts_unknown` 让闸门知道
                # 「这行无法验证时效」，盘中按 fail-closed 丢弃。
                out[code] = {"name": p[0], "px": float(p[1]),
                             "chg": float(p[3]), "amount": float(p[5]),
                             "_ts": "", "_ts_unknown": True}
        except (ValueError, IndexError):
            continue
    if not out:
        M._daily_log_once("sina", "parse-fail",
                          f"新浪有响应但零行可用（改版/风控页？），响应前 80 字: "
                          f"{txt[:80]!r}（本日首次）")
    return out

# 百度股市通：code 用裸 6 位；港股/美股用其 market_type
BAIDU_CODES = {
    "000001": ("000001","ab"), "399001": ("399001","ab"), "399006": ("399006","ab"),
    "000688": ("000688","ab"), "000300": ("000300","ab"), "000905": ("000905","ab"),
}
# 同花顺：指数代码规则 1A0001=上证 1A0002=深成 1A0003=创业板 1B0688=科创50
THS_CODES = {
    "000001": "1A0001", "399001": "1A0002", "399006": "1A0003",
    "000688": "1B0688", "000300": "1B0300", "000905": "1B0905",
}

# 被目标站点**风控拦截**的源（进程内状态，供 selfcheck 的 [数据源] 段分档）。
# `health()` 每次进入前清空，`from_baidu` 命中风控响应时加入。
# ⚠️ 为什么需要这层区分（2026-10-16 生产实测）：百度对第三方客户端有
# **间歇性风控**（HTTP 200 + 合法 JSON `{"Result":{"code":403,
# "isCaptchaEnabled":true,"msg":"hit risk"}}`，实测窗口可达数十分钟、随后
# 自愈）。它既不是本系统的缺陷、也不是源下线 —— 但 `health()` 只返回 bool，
# 与"解析坏/接口改版"**同形**，于是自检每次撞上风控窗口就打 ❌ →
# ok_all=False → rc=1 → 推 webhook 打扰（2026-10-16 17:42 那条即此因）。
# 分档后：风控 → ⚠️ + 本轮未验证（不置 rc）；其余失败 → ❌（照旧推）。
RISK_BLOCKED = set()

def from_baidu(keys):
    """百度股市通。返回 {key: {name,px,chg,high,low,preclose}}"""
    out = {}
    for k in keys:
        if k not in BAIDU_CODES: continue
        code, mt = BAIDU_CODES[k]
        url = (f"https://finance.pae.baidu.com/vapi/v1/getquotation?srcid=5353&all=1"
               f"&code={code}&market_type={mt}&group=quotation_index_minute&query={code}")
        txt = _curl(url, "https://gushitong.baidu.com/", enc="utf-8")
        try:
            d = json.loads(txt)
            R = d["Result"]
            # ⚠️ 百度被风控时返回的是 **HTTP 200 + 合法 JSON**：
            #   {"Result":{"code":403,"isCaptchaEnabled":true,"msg":"hit risk"}}
            # 它一路走进下面的 `cur.get("price")` 为假的分支，**什么都不产出、
            # 也不记一行日志** —— 与"接口改版"、"数据格式变了"在日志里
            # 完全无法区分。实测 2026-09-30：连续 3 次探测里 1 次成功、
            # 2 次 hit risk（**间歇性**），而 `health()` 只报一句 `❌ baidu`，
            # 看不出是风控还是解析坏了 —— 排查方向完全不同。
            # 这是本项目最忌讳的"静默失败"形态（同 [[market-watch-json-shape-contract]]
            # 里 502 页被当成取数成功那一次）。
            if isinstance(R, dict) and (R.get("isCaptchaEnabled")
                                        or R.get("code") == 403):
                # 同时置入风控分档集合：[数据源] 段据此把「间歇风控」与
                # 「结构性损坏」分开（见 RISK_BLOCKED 上方长注释）。
                # 不区分的话风控窗口内的自检必然 rc=1 → 必然打扰用户。
                RISK_BLOCKED.add("baidu")
                # _daily_log_once（2026-10-15 复核 P2-2）：backup_quotes 对
                # 11 个键逐个调本函数，风控窗口内裸 log 一轮最多 11 行相同
                # 内容（实测），watch.log 2MB 单代轮转里真诊断被挤掉。
                M._daily_log_once("baidu", "risk",
                                  f"百度风控拦截（{R.get('msg') or R.get('code')}）"
                                  f"—— 该源本轮不可用，降级到下一顺位（本日首次）")
                continue
            cur = R.get("cur") or {}
            pk = {x.get("ename"): x.get("value") for x in R.get("pankouinfos",{}).get("list",[])}
            # ⚠️ ratio 缺失时**不能**默认成 "0%"。那会把"没有数据"渲染成
            # "平盘" —— 读者看到「科创50 +0.00%」这个**正常数字**，无法区分
            # "真的没波动"与"这个源根本没给涨跌幅"。
            # 实测（2026-09-27，构造 ratio=null / ""）：
            #   ratio=None -> chg 0.0（应为"无数据"）
            #   ratio=''   -> chg 0.0（同上）
            # 同文件的 from_ths（pre=0）与 from_tencent（p[32]）都有显式守卫，
            # 此处不一致。缺 ratio 就整条弃用，交给下一个备用源。
            _ratio = cur.get("ratio")
            # ⚠️ 2026-10-16 复核实测：百度对**当日尚未形成的涨跌幅**返回字符串
            # `"--"`（不是 None/空串）—— 它是**非空字符串（truthy）**，于是
            # `if cur.get("price") and _ratio:` 放行，`_num("--")` 返回 0.0，
            # 整条被渲染成「科创50 +0.00%」= **平盘这个正常数字**。
            # 实测：ratio="--" → `{'px': 3823.44, 'chg': 0.0}`（应为"无数据"、
            # 整条弃用，与上面「缺 ratio 就整条弃用」的原意一致）。
            # 先在判定**之前**把 `--`/`-`/空串统一归一为 None。
            if isinstance(_ratio, str):
                _rs = _ratio.strip()
                if not _rs or set(_rs) <= {"-"}:
                    _ratio = None
            if cur.get("price") and _ratio:
                # ⚠️ 百度对**当日尚未形成的字段**返回的是字符串 `"--"`，
                # 不是 None/空串 —— 于是 `pk.get("high") or 0` 这类"缺省兜底"
                # **完全不起作用**（`"--"` 是非空字符串，为真），紧接着
                # `float("--")` 抛 ValueError，被外层 `except Exception: continue`
                # 吞掉，**整条记录**（含已经取到的 price/ratio）一起报废。
                #
                # `[实测]` 2026-09-29 09:12（开盘前）与 09:30 后各测多次：
                #   cur.price='3823.44'  cur.ratio='-0.00%'（都正常）
                #   pankouinfos: high='--' low='--' open='--' amount='--'
                #   -> ValueError: could not convert string to float: '--'
                #   -> from_baidu 恒返回 {}，`feeds.health()["baidu"]` 恒 False。
                # 它在降级链里的位置（2026-10-16 起）是 tencent -> sina -> ths
                # -> **baidu（末位，用户指令降级）**；此前是第三顺位、
                # 即**第三顺位永久死链**：
                # 前两源都挂时本应轮到它，实际直接跳到 ths，白丢一个备用源。
                # 更坏的是 `health()` 每天报 `❌ baidu`，而 `[数据源]` 段不置
                # ok_all，于是"一个备用源恒不可用"这件事**永远不会浮到结论上**。
                #
                # 修法：逐个字段用 M.isnum 判**解析后的数值**，非数值一律取 0，
                # 让"价格有效"的行照常产出。注意 high/low/preclose 在
                # `backup_quotes` 里**本就被丢弃**（只取 px/chg），所以取 0
                # 不会污染任何下游；而"缺 ratio 就整条弃用"的原意**保留**
                # —— 涨跌幅缺失时仍不产出，避免把"无数据"渲染成"平盘"。
                def _num(v):
                    """百度字段 -> float；`"--"`/None/空串/非数值一律 0.0。"""
                    try:
                        f = float(str(v).replace("%", "").replace("+", "").strip())
                    except (TypeError, ValueError):
                        return 0.0
                    return f if M.isnum(f) else 0.0
                out[k] = {"name": R.get("basicinfos",{}).get("name", k),
                          "px": _num(cur["price"]),
                          "chg": _num(_ratio),
                          "high": _num(pk.get("high")),
                          "low": _num(pk.get("low")),
                          "preclose": _num(pk.get("preClose")),
                          # ⚠️ 2026-11-06（P1-1）：百度行不提供行情时间戳。
                          # 显式标记「时效未知」，让 quotes() 的新鲜度闸门
                          # 在盘中按 fail-closed 丢弃，而不是静默放行。
                          "_ts": "", "_ts_unknown": True}
            else:
                # 走到了这里说明"响应是合法 JSON、但既没有 price 也没有 ratio"。
                # 记一行：这是**源可用但数据不全**，与上面两种失败形态都不同，
                # 静默丢弃会让"备用源悄悄退化成空壳"无人知晓。
                M._daily_log_once("baidu", "no-price",
                                  f"百度返回结构不含价格/涨跌幅（key={k}）"
                                  f"—— 该源本轮不产出（本日首次）")
        except Exception as e:
            # ⚠️ 原先是裸 `except Exception: continue` —— JSON 解析失败、
            # 形状改版、字段类型变化**全部静默**。备用源的价值恰恰在于
            # "主源挂掉时才启用"，而它自己的故障却没有任何痕迹。
            # ⚠️ 空串不算"解析失败"（2026-10-15 复核 P2-3）：_curl 在主机
            # 熔断/配额耗尽时**不发请求**直接返回 ""，json.loads("") 抛
            # JSONDecodeError 落到这里 —— 把"我们没配额去探"写成"百度格式
            # 变了"，排查方向相反。空串单独记，且与熔断行的 bakskip 去重键
            # 区分开（一个是本函数视角、一个是 _curl 视角）。
            if not (txt or "").strip():
                M._daily_log_once("baidu", "empty",
                                  f"百度空响应（熔断/配额/兜底全败，见 "
                                  f"bakskip 行）key={k}（本日首次）")
            else:
                M._daily_log_once("baidu", "parse-fail",
                                  f"百度解析失败({type(e).__name__}: {e})，"
                                  f"响应前 80 字: {txt[:80]!r}（本日首次）")
            continue
    return out

def from_ths(keys):
    """同花顺分时接口（JSONP）"""
    out = {}
    for k in keys:
        if k not in THS_CODES: continue
        code = THS_CODES[k]
        url = f"https://d.10jqka.com.cn/v2/time/hs_{code}/last.js"
        txt = _curl(url, "https://stockpage.10jqka.com.cn/", enc="utf-8")
        # 空响应留痕（与 tencent/sina 同标准）；注意要在 try 外判，
        # 否则 txt.index("(") 对空串抛 ValueError 会先被记成"解析失败"
        if not (txt or "").strip():
            M._daily_log_once("ths", "empty",
                              f"同花顺空响应（熔断/配额/兜底全败）key={k}"
                              f"（本日首次）")
            continue
        try:
            j = txt[txt.index("(")+1:txt.rindex(")")]
            o = json.loads(j)[f"hs_{code}"]
            pre = float(o.get("pre") or 0)
            rows = [r.split(",") for r in o.get("data","").split(";") if r]
            last = float(rows[-1][1])
            # ⚠️ `last` 必须与 `pre` 一样有守卫。停牌/未开盘时同花顺的
            # 尾行是 `1500,0,0`（价格为 0），而 `pre` 有守卫、`last` 没有 ——
            # 于是算出 `(0/pre - 1)*100 = -100.0%`，并写出 `px: 0.0`。
            # 实测（2026-09-27，构造 pre=3936.52 + 尾行 0）：
            #   {'px': 0.0, 'chg': -100.0, 'preclose': 3936.52}
            # 而 backup_quotes 没有任何量级校验，-100% 会作为真实涨跌幅透传
            # （渲染成「上证 -100.00%」这种看着像崩盘的假数据）。
            # 作者显然意识到"占位 0"这类形态（`pre` 就是为此加的守卫），
            # 只守了一半。
            if pre and M.isnum(last) and last > 0:
                # _ts：o["date"]="20261015"（YYYYMMDD）+ 尾行首字段=HHMM。
                # 补 "00" 凑成 14 位秒级（秒未知取 0，对 20 分钟陈旧阈值
                # 无影响），与 _ts_parse 的 "%Y%m%d%H%M%S" 匹配
                # （实测 2026-10-15）。收盘后 15:00 的尾行时间戳在 15:05
                # （in_session 上界）前不会被判陈旧，之后闸门整体跳过。
                _lastrow = rows[-1]
                out[k] = {"name": o.get("name", k), "px": last,
                          "chg": round((last/pre - 1)*100, 2),
                          "preclose": pre,
                          "_ts": (f"{o.get('date','')}{_lastrow[0]}00"
                                  if o.get("date") and _lastrow else "")}
        except Exception:
            # ⚠️ 与 from_baidu 的三分法同标准（2026-10-08 审查）：任一解析步骤
            # 失败原先都静默 -> health() 只打 ❌ ths，无法区分"源坏了"与
            # "我解析错了"（备用源静默失败记忆的通用化结论未落到本函数）。
            M._daily_log_once("ths", "parse-fail",
                              f"同花顺解析失败（{k}），响应前 80 字: "
                              f"{(txt or '')[:80]!r}（本日首次）")
            continue
    return out

def _usable_row(r):
    """一行行情是否**真的可用**（值级判据）。

    供 `health()` 与 `backup_quotes()` 共用：两处若各写一份，
    就会出现“探活说健康、实际取不到数”的分叉（本仓库已犯过多次）。
    """
    return isinstance(r, dict) and M.isnum(r.get("px")) and r["px"] > 0


def backup_quotes(keys=None):
    """备用源取行情，返回与 marketlib.quotes 兼容的结构 {f12: row}"""
    keys = keys or list(MAP.keys())
    tmap = [MAP[k][0] for k in keys if k in MAP]
    # 原先这里还有 `smap = [MAP[k][1] for k in keys if k in MAP]`，
    # 全文未使用（下面用的是 rmap / s_ 系列）。删掉，省一次列表构造。
    t = from_tencent(tmap)

    def _usable(r):
        """这一行的**值**是否真的可用（而非只看键在不在）。

        ⚠️ 原判据是 `if row is None` —— 它只挡得住"这个源完全没有该键"，
        挡不住"有键但字段残缺"。腾讯/新浪在限流期会返回 `{"name": ...}`
        这种**没有 px/chg** 的行，此时 row 非 None，后续源（新浪/百度/
        同花顺）**一个都不会被尝试**，最终把 `f2: None` 写进结果。
        实测（2026-09-27）：腾讯给 `{"hkHSI":{"name":"恒生指数"}}`、
        新浪能给出正确的 24510.09/-1.01，而 backup_quotes 返回的是
        `{'f2': None, 'f3': None, '_src': 'tencent'}` —— 有正确答案却不用。
        判据改为"px 是有限正数"：价格是这一行的**存在意义**，
        没有它这行就是垃圾，应当继续降级。
        """
        if not (isinstance(r, dict) and M.isnum(r.get("px")) and r["px"] > 0):
            return False
        # ⚠⚠ 2026-11-10（Codex 审查 P2）：**旧价格不是价格**。
        # 原判据只看"px 是有限正数"，于是腾讯回**昨日**那一行时
        # 仍被判为可用 → 选中它、而新浪的**当天**值一次都没被调用；
        # 下游 `quotes()` 的新鲜度闸门随后把它剔掉 → **返回 0 条行情**。
        # 有 `_ts` 时就能判；无 `_ts`（百度/简版）保持原有 `_ts_unknown` 逻辑不动。
        _ts = r.get("_ts")
        if _ts:
            try:
                _n = M.datetime.datetime.now()
                # ⚠⚠ **只在交易时段内**才判陈旧 —— 与 `quotes()` 的闸门同一口径。
                # `[实测]` 我第一版没加这个条件，直接结果：21:16 收盘后
                # 所有源都返回当日 16:14 的收盘价，被 20 分钟阈值判为陈旧
                # → `backup_quotes` 返回空 → `quotes()` 返回 0 条（而 HEAD 版本返回 2 条）。
                # 收盘后/盘前的“最近收盘价”就是正确语义，不能当陈旧丢。
                if (M.in_session(_n) or M.hk_session_open(_n)) \
                        and M.is_trading_day(_n.date(), warn=False) \
                        and M._is_ts_stale(_ts, _n, max_min=20):
                    return False
            except Exception:
                pass          # 判定本身出错时放行（与 `_is_ts_stale` 的取舍一致）
        return True

    out = {}
    for k in keys:
        if k not in MAP: continue
        tc, sc, nm = MAP[k]
        row = None; src = None
        if _usable(t.get(tc)):
            row = t[tc]; src = "tencent"
        if row is None:
            s = from_sina([sc])
            if _usable(s.get(sc)): row = s[sc]; src = "sina"
        if row is None:
            h = from_ths([k])
            if _usable(h.get(k)): row = h[k]; src = "ths"
        if row is None:
            # 百度降为**末位**（2026-10-16 用户指令「baidu源不要作为主用，降级」）。
            # 原顺位是 tencent→sina→baidu→ths 的第三位 —— 但百度对第三方
            # 客户端有**间歇性风控**（实测窗口数十分钟、见 RISK_BLOCKED 注释），
            # 风控期它不但自己不产出，还**先于同花顺吃掉一次降级尝试**：
            # 前两源挂掉时行情要白白多等一轮风控失败才轮到 ths。
            # 挪到末位后：常用链（tencent→sina→ths）完全不经过百度，
            # 只有 ths 也失败时才试它 —— 风控对行情链路的影响从"每轮必撞"
            # 变成"双兜底都失效才可能撞上"。
            b = from_baidu([k])
            if _usable(b.get(k)): row = b[k]; src = "baidu"
        if row:
            # ⚠️ 2026-11-06（P1-1）：备用源（百度 / 新浪简版）**结构上没有
            # 行情时间戳**，row 里带 `_ts_unknown=True`。本函数被 collect/alert
            # **直接调用**，不经过 marketlib.quotes() 的新鲜度闸门 ——
            # 不在这里拦，陈旧价就绕过整条防线。
            # 盘中 fail-closed 丢弃（宁可「数据缺失」也不给「昨收当现价」）；
            # 非交易时段放行（那时"最近收盘价"就是正确语义）。
            if row.get("_ts_unknown"):
                try:
                    _now = M.datetime.datetime.now()
                    _live = M.in_session(_now) or M.hk_session_open(_now)
                except Exception:
                    _live = False
                if _live:
                    M._daily_log_once(f"ts-unknown-{src}", "no-ts",
                                      f"备用源 {src} 的行情行无时间戳，"
                                      f"盘中无法验证新鲜度 → 丢弃（本日首次）")
                    continue
            out[k] = {"f12": k, "f14": row.get("name", nm),
                      "f2": row.get("px"), "f3": row.get("chg"),
                      "_src": src,
                      # 透传行情时间戳：下游据此判断数据新鲜度。
                      # 备用源没有东财的 f124，这个字段是唯一的时效依据。
                      "_ts": row.get("_ts", "")}
    return out

def health():
    """探测各源可用性。

    ⚠️ `MW_HEALTH_FIXTURE`（JSON 文件路径）可注入**固定探活结果** ——
    仅供自检的变异回归使用（见 selfcheck_mutation）。理由：本函数打
    **真实网络**，而备用源（baidu）有**间歇性风控**（实测连续 3 次探测
    全败、随后 8 次全成）。于是 `[数据源]` 段在风控期内**必然**打 ❌，
    使「沙箱基线不干净」的闸门**恒命中** → 整轮回归跑不起来。
    回归要测的是**检查器逻辑**能不能抓住注入缺陷，与"此刻外网通不通"
    无关；把它夹具化成恒定输入，才是可复现的判据。
    未设该变量时行为与原先**完全一致**（真探活）。

    ⚠️ 夹具可带**可选键** `_risk`（源名列表，如 `["baidu"]`）：声明这些源
    本轮"被风控拦截"，供变异回归构造该状态。`_risk` 在**任何**返回路径上
    都被 pop，不会作为源名泄漏给调用方；未带 `_risk` 时行为与原先一致
    （RISK_BLOCKED 为空、无风控分档、`[数据源]` 的 ❌ 语义不变）。
    """
    _fx = os.environ.get("MW_HEALTH_FIXTURE")
    # 本轮探测的风控标记清空（进程内集合跨轮会残留：自检本身每轮新进程，
    # 但 feeds 可能在同一进程里被多次调用 —— 上一轮的标记不能污染本轮）。
    # 放在夹具分支**之前**：夹具不设 `_risk` 时也要清空（同进程早前一次
    # 真探活若命中风控，不能把残留状态带进夹具供给的固定结果）。
    RISK_BLOCKED.clear()
    if _fx:
        try:
            with open(_fx, encoding="utf-8") as _fh:
                _d = json.load(_fh)
            if isinstance(_d, dict):
                if "_risk" in _d:
                    _rk = _d.pop("_risk")
                    if isinstance(_rk, list):
                        RISK_BLOCKED.update(x for x in _rk if isinstance(x, str))
                return _d
        except Exception:
            pass   # 夹具坏了就退回真探活（不静默吞掉结果）
    import marketlib as M
    res = {}
    # 东财（主源 + 备用主机，任一可达即算可用）
    #
    # ⚠️ 2026-10-22：探测顺序改成「**按健康度排序**，而不是硬编码 EM 先」。
    # 原写法先探 `M.EM` —— 那正是 `PERMANENTLY_DEAD` 里的 push2delay，
    # 于是每次 health() 都在 cj() 的封锁早退处**零发包**返回 None，
    # 再走一遍 `_perm_dead_log_once`，然后才去探真正能应答的 push2test。
    # 后果有两层：
    #   ① 每天 09:1x 的 `[数据源]` 段必然先命中那条「跳过已永久封锁的主机」
    #      日志（生产累计 19 行）—— 一条**恒真**的噪音，读者会以为
    #      "东财今天又被封了"，而它只是配置里写着别去碰；
    #   ② 探活结论与真实可用性脱节：`eastmoney` 键的含义是"东财能不能
    #      取到数"，而代码先问了一台**按设计不该问**的主机。
    # `_ordered_em_hosts()` 把永久封锁的排末位、按滚动成功率排前面，
    # 正好是 health() 想要的语义；取前两台即可（health 只是布尔探活，
    # 不需要把四台全打一遍 —— 那是纯耗配额）。
    try:
        _em_hosts = M._ordered_em_hosts()[:2]
        d = M.cj(f"{_em_hosts[0]}/ulist.np/get?secids=1.000001&fields=f2"
                 f"&ut={M.UT}&fltt=2&invt=2", timeout=8, retries=1)
        # ⚠️ 必须套 bool()：_has_data 现在返回**内层 dict**（见其 docstring），
        # 直接赋值会让 res["eastmoney"] 存成 dict，而下游 selfcheck 读的是
        # 真假值语义 —— 虽然 dict 的假值判断也对，但把 dict 写进"可用性布尔
        # 表"会让日志/面板显示成 `{'f2': 3700}` 这种看不懂的东西。
        ok = bool(M._has_data(d))
        if not ok and len(_em_hosts) > 1:
            d2 = M.cj(f"{_em_hosts[1]}/ulist.np/get?secids=1.000001&fields=f2"
                      f"&ut={M.UT}&fltt=2&invt=2", timeout=8, retries=1)
            # 同上一行：`d2 and d2.get("data")` 在 d2 是 list/str/int 时
            # AttributeError。selfcheck 的 [响应形状鲁棒性] 只扫变量名 `d`，
            # 这里是 `d2` —— 同一个惯用法，同样要修。
            ok = bool(M._has_data(d2))
            if ok: res["eastmoney_backup"] = True
        res["eastmoney"] = ok
    except Exception:
        res["eastmoney"] = False
    # 腾讯
    # ⚠ 2026-11-10（Codex）：用**值级**判据而不是 `bool(容器)`。
    # 东财回 `f2: null`、腾讯回零价行，`bool({...})` 一律判“健康”——
    # 那个源实际取不到数，而 `[数据源]` 段长期全绿。
    t = from_tencent(["sh000001"])
    res["tencent"] = any(_usable_row(r) for r in t.values()) if isinstance(t, dict) else False
    # 新浪
    # 与 MAP 同码（2026-10-15 复核 P2-4）：原先写死 s_sh000001 简版，
    # MAP 已切全量格式 —— health 探测的必须是**实际备用链走的路径**，
    # 否则探的是简版、用的是全量，health 绿而实际路径坏。
    s = from_sina([MAP["000001"][1]])
    res["sina"] = any(_usable_row(r) for r in s.values()) if isinstance(s, dict) else False
    # 同花顺
    _th = from_ths(["000001"])
    res["ths"] = any(_usable_row(r) for r in _th.values()) if isinstance(_th, dict) else False
    # 百度（探活**保留**，但它在降级链里已降为末位 —— 见 backup_quotes；
    # health() 仍单独探它，因为"这个源还能不能用"与"排在第几位"是两件事，
    # selfcheck 的风控分档/❌ 判定都依赖这份探活结果。）
    # ⚠️ 2026-10-27 修（P3）：原先写的是 `bool(from_baidu(["000001"]) or
    # from_baidu(["000001"]))` —— 同一个请求写了两遍。`from_baidu` 失败时
    # 返回 `{}`（falsy），`or` 于是执行**第二次完全相同**的调用：风控窗口内
    # 每次 `health()` 白打一倍请求，而每次 `from_baidu` 都走 `_curl` →
    # `RL.wait`，**消耗日配额**。这不是"重试"（两次紧邻、没有任何退避，
    # 风控下必然同时失败），纯属笔误式重复。
    _bd = from_baidu(["000001"])
    res["baidu"] = any(_usable_row(r) for r in _bd.values()) if isinstance(_bd, dict) else False
    return res

if __name__ == "__main__":
    print("=== 数据源健康检查 ===")
    h = health()
    for k, v in h.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print()
    print("=== 备用源取行情 ===")
    q = backup_quotes(["000001","000688","HSTECH","00700"])
    for k, v in q.items():
        print(f"  {v['f14']:<12}{v['f2']:>11,.2f}  {v['f3']:>+6.2f}%")
