#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热点板块识别（基于实测验证的算法）

核心结论（实测）：
  1. Jaccard 对"包含关系"是盲的 —— 出版/大众出版 J=0.643 但重叠度 100%
     必须用 Jaccard ∪ 重叠系数(min) 双度量
  2. 贪心去重优于层次单链接（后者有链式合并残留）
  3. f24 = 60日涨跌幅，是区分"启动 vs 涨完"的关键字段（已用K线校准）
  4. 位置惩罚（减法）比任何"放量未大涨"加权都有效
  5. 权重不敏感，区分度来自去重与位置惩罚
"""
import os, re, time, datetime, sys, fcntl

# 允许 MW_DIR 覆盖：测试可在沙箱里跑而完全不碰线上状态文件
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # src/
from paths import p_state, CACHE_DIR
from marketlib import UT, cj as _cj, log as _log, clist as _clist, _has_data
import marketlib as M   # 原子读写助手 _atomic_write_json / _load_json

CACHE = os.path.join(CACHE_DIR, "members")

BLACKLIST_EXACT = {
 "融资融券","深股通","沪股通","陆股通","富时罗素","标准普尔","MSCI中国","MSCI概念",
 "机构重仓","QFII重仓","基金重仓","社保重仓","券商重仓","保险重仓","信托重仓",
 "专精特新","央国企改革","央企改革","国企改革","中字头","养老金",
 "创业板综","科创板","科创50","北交所概念","注册制次新股","次新股","核准制次新股",
 "转债标的","破净股","长期破净","破发股","破增发价股","高送转","举牌","含可转债",
 "小盘股","大盘股","中盘股","低价股","高价股","高成长股","高市净率","微利股",
 "超跌股","趋势股","近期新高","历史新高","昨日涨停","昨日连板","昨日首板",
 "沪深300","上证50","中证500","深成500","上证180","AB股","AH股","B股","GDR",
 "预盈预增","预亏预减","业绩预增","业绩预减","壳资源","参股新三板","周期股",
 "金融地产风格","内贸流通","融资融券标的","标普道琼斯A股","题材股","ST股",
 "股权转让","参股银行","参股保险","参股券商","独角兽","创投",
 "REITs","机构调研","高股息精选","红利破净股","红利股","价值股","反转股",
 "东方财富热股","科创板做市股","权重股","证金持股","股权分散","股权激励",
 "贬值受益","长江三角","深圳特区","西部大开发","一带一路","券商金股",
}
BLACKLIST_PAT = [r"^昨日", r"涨停", r"连板", r"首板", r"打板", r"跌停",
 r"重仓", r"股通", r"标的$", r"^含[ABH]股", r"次新", r"^ST", r"^st",
 r"预增$", r"预减$", r"预盈", r"预亏", r"新高$", r"新低$", r"^破",
 r"解禁", r"增持$", r"减持$", r"回购$", r"高送转", r"送转", r"^融资",
 r"融券", r"^沪股", r"^深股", r"指数$", r"成份", r"成分",
 # ⚠️ 原为 `r"(?!消费风格)风格$"`（2026-10-11 审查删除）：负向先行断言
 # **不生效** —— `(?!X)Y` 只在"同一位置"成立才拦，而 re.search 会继续
 # 尝试其他起点：起点=2 时 `(?!消费风格)` 后面不是"消费风格"，断言成立，
 # 然后 `风格$` 匹配成功 → **仍然被拦**。实测：
 #   re.search(r"(?!消费风格)风格$", "消费风格") -> True（被拦）
 #   re.search(r"风格$", "消费风格")             -> True（被拦）
 # 两者**完全等价** —— 那个负向断言是空操作，"消费风格"永远被过滤。
 # 而"消费风格"是 sectors.json 里**用户显式配置**的 watch+push 板块
 # （90.BK1711，note="消费。风格指数(按因子筛股)，非真实行业分组"），
 # 于是它永远进不了热点榜 —— 而【热点】段是 AI 的主要素材之一。
 # 改法：删掉这个空操作断言，让"消费风格"走正常候选池。
 # ⚠️ 不能改成 `r"(?<!消费)风格$"` —— 那会把"金融地产风格"等**真实**
 # 风格类板块一起拦掉（它们本就该被拦，但理由不同：它们是"风格分类"
 # 而非"行业分组"）。"消费风格"的特殊性在于它是**用户显式要看的**，
 # 而"金融地产风格"不是 —— 这个区别只有配置知道，代码不该猜。
 r"^大盘", r"^中盘", r"^小盘", r"^低价", r"^高价",
 # 原为 r"股$" catch-all，误杀"金融控股"等真实主题，改为显式枚举风格类
 r"(大盘|中盘|小盘|微盘|权重|题材|趋势|超跌|破发|破净|低价|高价|百元|"
 r"红?利|价值|成长|周期|反转|次新|微利|绩优|亏损|ST|机构|基金|QFII|社保|券商|保险|信托)股$",
 r"季报", r"中报", r"年报", r"首亏", r"扭亏", r"减亏", r"高振幅",
 r"高换手", r"炸板", r"触板", r"成份股$", r"做市股$", r"热股$", r"金股$",
 r"权重股$"]
_PAT = [re.compile(p) for p in BLACKLIST_PAT]

def is_statistical(n):
    return n in BLACKLIST_EXACT or any(p.search(n) for p in _PAT)

cj = _cj

def fetch_sectors():
    """拉全部行业+概念板块（含 f24=60日涨跌幅）"""
    F=("f2,f3,f6,f8,f10,f12,f14,f20,f21,f24,f62,f66,f69,f104,f105,f128,f134,f184")
    out=[]
    # pz=500：行业496个/概念504个，一页取全，避免分页把请求数放大11倍
    for fs,tag in (("m:90+t:2","ind"),("m:90+t:3","con")):
        # `budget_s`：单次板块列表的总耗时上限（见 marketlib.clist 的说明）。
        # 取 60s —— 正常情况下一次 clist 实测 0.2~2s，60s 只在"主机能连但慢"
        # 的黑洞型故障下才会命中，那正是要止损的场景。
        #
        # ⚠️ `max_pn` 从 3 提到 8（2026-10-22）：3 是按 **pz=500 不被截断**
        # 算的（行业 496 / 概念 516，两页足够）。但服务端可能把 pz 截到
        # 自己的上限（实测明文 HTTP 主机对 pz=500 只返 100 行，且翻页偏移
        # 也按 100 算）—— 那时 3 页只能拿到 300 个板块，**静默少 40%**。
        # 提到 8 覆盖 100×8=800 > 516。**代价为零**：clist 现在按服务端
        # `total` 判终止，正常路径第 2 页就 break，不会真的发 8 次请求。
        # ⚠️ 2026-11-24（P1）：改走 `M.sector_rows()` —— 与 `alert._sector_rows`
        # **共用同一份全量名单**。原先两处各拉一遍同一批板块，
        # 一轮 19 次请求而 firecrawl 额度只有 11/min、DATA_BUDGET 120s，
        # 19 次需要 127s —— 算术上就不可能取全。
        # 见 `marketlib.sector_rows` 上方的账。
        got, _tot, _complete = M.sector_rows(fs, max_pn=8)
        for x in got:
            # ⚠️ 必须判 dict。clist() 会把 diff 里的元素**原样透传**，而
            # diff 的形状由上游决定（可能是 list 含字符串/None），
            # 裸做 x["_tag"]=tag 会 TypeError: 'str' object does not support
            # item assignment —— 实测条目为 str/None/int 三种都抛。
            # 后果：整轮热点榜静默消失（build_brief 的【热点】段直接没有）。
            if not isinstance(x, dict):
                continue
            x["_tag"]=tag; out.append(x)
    return out

MEMBERS_BUDGET = 40     # members 拉取的整轮时间上限（秒）。
                        # 全新环境首轮要拉 ~870 个板块的成分股，每个 0.2~2s，
                        # 最坏能跑几十分钟（实测沙箱冷启动 400s 超时都没跑完）。
                        # 预算用尽就跳过剩余板块 —— 它们的成分股数未知，
                        # 本轮不参与热点评选（下轮缓存补齐后自然进入）。
                        # 成分股缓存永久有效，所以只有首轮会受限。
_mem_deadline = [0.0]   # 本轮 members 截止时刻（analyze 开头设置）

# 成分股缓存的有效期（秒）。板块成分股会随指数公司调仓变化，
# 而原实现**只看文件存在与否**，无 TTL、无版本校验 —— 实测把 mtime
# 改成 400 天前，`members()` 仍直接返回旧集合、不发任何请求。
# 后果不只在"名单旧"：`n=len(ms)` 与 `len(ms)<8/10` 的过滤都吃这个数，
# 成分股增删几只会让边缘板块突然通过/不通过候选池，而 hot 的排名
# 正是从候选池算起的 —— 排名会随一份看不见的陈旧名单漂移。
# 取 7 天：调仓是低频事件，7 天足够新，又不会把 1000 个板块的请求量
# 重新推高（原来是一生只拉一次）。
MEMBERS_TTL = 7 * 86400

_swept = [False]          # 本轮是否已清扫过过期缓存

def _sweep_members():
    """删除过期的成分股缓存文件。每轮最多执行一次。

    ⚠️ 为什么需要：`members()` 的 TTL 只在**读取时**判过期 ——
    过期文件不会自动消失，只有在**下次访问同一板块**时才会被覆盖。
    于是"曾经进过候选池、后来消失的板块"会留下**永不被访问也永不被删**
    的孤儿文件。实测 2026-10-01：1002 个文件里 **145 个（14%）已过期**，
    最老的 9 天 —— 而 TTL 就是 7 天。

    单文件很小（~800B），所以这不是磁盘问题；问题是**孤儿文件会掩盖
    "这个板块还在不在"** —— 排查"某板块为什么没进热点榜"时，看到
    缓存文件存在会误以为它近期被拉取过。

    性能：1000 次 os.path.getmtime 约 3ms，且每轮只做一次；
    放在 `analyze_cached` 里、**在缓存命中提前 return 之前**是不行的
    （命中时不该做额外 IO），所以挂在 `members()` 上、用 `_swept` 去重。
    """
    if _swept[0]:
        return
    _swept[0] = True
    try:
        now = time.time()
        for f in os.listdir(CACHE):
            if not f.endswith(".json"):
                continue
            fp = os.path.join(CACHE, f)
            try:
                if now - os.path.getmtime(fp) > MEMBERS_TTL:
                    os.unlink(fp)
            except Exception:
                continue
    except Exception:
        pass


def members(code):
    """成分股（带本地缓存，7 天过期）"""
    _sweep_members()
    # ⚠ 2026-11-10（安全审查 M1）：`code` 直接拼进路径，若上游 `f12`
    # 返回 `../../logs/watch` 这种值就是路径穿越（缓存目录可写）。
    # 现实中 f12 是板块代码（如 BK1036），但守约定不能靠上游“乖”。
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{1,12}", code):
        _log(f"板块代码形状异常，拒绝作为缓存文件名: {code!r}", "ERR")
        return set()
    p=os.path.join(CACHE,f"{code}.json")
    if os.path.exists(p):
        try:
            if time.time() - os.path.getmtime(p) < MEMBERS_TTL:
                _c = M._load_json(p, [])
                # ⚠️ 必须校验形状，不能直接 set(_c)。
                # 缓存文件可能被写坏 / 来自旧版本 / 手工编辑过，而
                # `set("notalist")` 会把字符串拆成**单个字符**当作成分股：
                # 实测返回 {'a','i','l','n','o','s','t'}，len=7 —— 这些"股票"
                # 会通过 8/10 的候选池门槛进热点榜，且去重时与任何板块的
                # mem 交集都是空（永不判重），同类坏缓存之间又必然高重叠
                # （误合并）。全程无报错、无日志。
                # 只接受"非空字符串列表"；不合法就当缓存未命中，重新拉取。
                if isinstance(_c, list) and _c and all(
                        isinstance(x, str) and x for x in _c):
                    return set(_c)
                M.log(f"成分股缓存形状异常（{code}），重新拉取", "CFG")
        except Exception: pass
    if _mem_deadline[0] and time.time() > _mem_deadline[0]:
        # 预算用尽**必须留痕**（2026-10-08 审查）：原实现静默返回空集 ——
        # 板块就此退出候选池、若 pool>0 还会把这份偏差热点榜缓存 600s，
        # 而日志里零痕迹（对照：VR 预算用尽有明确日志）。按日去重避免刷屏。
        M._daily_log_once("hot", "members-budget",
                          "members 预算用尽，本轮部分板块成分股未拉全"
                          "（热点榜为子集结果，本日首次）")
        return set()        # 预算用尽：本轮跳过，下轮续拉
    # ⚠️⚠️ 2026-10-27 修（P1）：**必须把剩余预算传给 `_clist`**。
    # 原先这里裸调 `_clist(f"b:{code}", "f12")` —— 预算只在**这个调用之前**
    # 判一次，而单次 `clist` 内部最多翻 12 页、每页 2 台主机各一次 `cj`，
    # 单次 `cj` 最坏 ≈175s（直连 3 次重试 + 代理 + firecrawl）。
    # 冷启动（成分股缓存未建）或 EM 主机"能连但慢"的黑洞型故障下，
    # **一个板块**就能吃掉几十分钟，而 launchd 不并发同一 job ——
    # 后续槽位全部静默消失（正是 `MEMBERS_BUDGET` 想防的事，却因为
    # 只判"调用之前"而形同虚设）。`fetch_sectors` 两处早就传了
    # `budget_s=60`，这里是同一文件里的漏网兄弟。
    # 传**剩余预算**而不是固定值：整轮 40s 是 `analyze()` 设的硬上界，
    # 每板块各自拿 40s 会让总耗时随板块数线性放大。
    _left = None
    if _mem_deadline[0]:
        _left = max(0.5, _mem_deadline[0] - time.time())
    got, _decl = _clist(f"b:{code}", "f12", budget_s=_left, with_total=True)
    # 同 fetch_sectors：clist() 原样透传 diff 的元素，形状由上游决定。
    # 裸做 x["f12"] 在条目为 str/None/int 时 TypeError（实测 3 种都抛），
    # 而异常会冒到 analyze 的热点段 —— 该段整体消失，且无任何日志。
    ids={x["f12"] for x in got if isinstance(x, dict) and x.get("f12")}
    if ids:
        os.makedirs(CACHE,exist_ok=True)
        # ⚠️ 必须加锁 + 锁内重读（2026-10-11 审查）：这里原先直接
        # `_atomic_write_json(p, sorted(ids))` —— 单次写不撕裂，但**读-改-写**
        # 会丢更新：本函数的 `ids` 是**这一轮**拉到的名单，而盘上可能已有
        # 上一轮（或另一个进程）写进去的更全的名单。直接覆盖 = 名单缩水，
        # 而 `n=len(ms)` 与 `len(ms)<8/10` 的候选池门槛都吃这个数 ——
        # 边缘板块突然不通过候选池，热点榜排名随一份看不见的陈旧名单漂移。
        #
        # ⚠️ 不能直接用 `marketlib._locked_update`：它把内容**强制当 dict**
        # （`if not isinstance(d, dict): d = {}`），而本文件是**裸 JSON 数组**
        # （见上面的 `_load_json(p, [])`）。用它会把名单整个变成 `{"90.BK1036":
        # [...]}`，下游 `set(_c)` 拿到的就是键字符串。实测：20 个并发写者
        # 用 `_locked_update` 后文件里只剩 1 条 —— 比不加锁还糟。
        # 故这里自己走同一把锁模式，但保留 list 形状。
        # ⚠️ 锁文件必须**单一共享**，不能每板块一个（2026-10-11 审查）：
        # `members/` 下约 875 个板块文件，若锁文件写成 `p + ".lock"`，
        # 目录里会多出 875 个 `.lock` —— 而 `_sweep_members` 只清 `.json`
        # （`if not f.endswith(".json"): continue`），这些 `.lock` 永远
        # 不被回收，目录文件数直接翻倍，且"哪些是缓存、哪些是锁"变得
        # 难分辨（排查"某板块为什么没进热点榜"时，看到文件存在会误以为
        # 它近期被拉取过 —— 正是 `_sweep_members` 那段注释要避免的）。
        # 用**一个** `members/.lock` 串行化所有板块的写入：成分股拉取本身
        # 就是串行的（`_clist` 逐个板块调），单锁不会造成额外等待。
        try:
            with M._open_lockfile(os.path.join(CACHE, ".lock")) as _lk:
                fcntl.flock(_lk, fcntl.LOCK_EX)
                try:
                    _old = M._load_json(p, [])
                    if not isinstance(_old, list):
                        _old = []
                    _merged = sorted({x for x in _old if isinstance(x, str)}
                                     | ids)
                    M._atomic_write_json(p, _merged)
                    # ⚠⚠ 2026-11-10（Codex P2）：**残缺名单不能当完整缓存 7 天**。
                    # `clist` 翻页失败时会“返回已拿到的部分”（那是对的），
                    # 但本函数把它当成完整名单落盘，而 `MEMBERS_TTL=604800`（5 天）——
                    # 实测：声明 total=200、第二页失败 → 缓存 100 条，
                    # 上游恢复后又回 100 条且**不再发请求**，直到 7 天后。
                    # 而 `n=len(ms)` 是候选池门槛（<8/<10 剔除）的输入 ——
                    # 名单裁半会让边缘板块默名其妙地落榜。
                    # 修法不改缓存形状（读端无感）：把 mtime 拨旧到
                    # “再过 5 分钟就过期”，既保留已取到的部分，又能尽快重试。
                    if _decl is not None and len(_merged) < _decl:
                        try:
                            _exp = time.time() - MEMBERS_TTL + 300
                            os.utime(p, (_exp, _exp))
                        except Exception:
                            pass
                        _log(f"板块 {code} 成分股不完整（{len(_merged)}/{_decl}）"
                             f"—— 缓存标记为 5 分钟后重试", "FEED")
                finally:
                    try:
                        fcntl.flock(_lk, fcntl.LOCK_UN)
                    except Exception:
                        pass
        except Exception:
            # 锁不可用时退回原子写 —— 至少不留半截文件（行为与修复前一致）
            M._atomic_write_json(p, sorted(ids))
    return ids

def _z(vals):
    if not vals: return lambda x: 0.0
    m=sum(vals)/len(vals)
    sd=(sum((v-m)**2 for v in vals)/len(vals))**0.5
    if sd < 1e-9: return lambda x: 0.0    # 全相等：该项无区分度，退化为常数
    return lambda x: (x-m)/sd

def _num(v, dv=0.0):
    """接口偶发返回 "-" 等非数值，统一兜底。语义同 marketlib.isnum。"""
    return v if M.isnum(v) else dv

def _cl(v,lo,hi): return max(lo,min(hi,v))

HOT_CACHE = p_state("hot_cache.json")
# 缓存有效期（秒）。必须**不大于**调度间隔（plist 的 StartInterval=300s）。
# `[实测]` 2026-10-20：原值 600 是间隔的两倍 —— 意味着第 2、4、6… 轮
# 全部命中上一轮的缓存，读者看到的「今日涨幅 TOP3」/热点方向最多**滞后
# 10 分钟**，而这条消息每 5 分钟就在更新别的数字（盘面/资金），
# 同一屏里两套时间基准，读者无从分辨。
# 成本核算：`analyze()` 的取数只有 2 次 clist（pz=500，行业+概念各一页），
# 成分股另有 7 天缓存。每轮多 2 次请求 × 约 112 轮/日 ≈ 224 次，
# 占日配额 2000 的 11% —— 换每轮数据都是当轮的很划算。
# ⚠️ 不能设得比间隔更小：那只是让缓存永不命中、白白多算一次，没有收益。
HOT_TTL = 300

def analyze_cached(top=8, ttl=HOT_TTL, force=False):
    """带缓存的 analyze。避免同一轮内重复拉全市场板块。

    ⚠️ TTL 与调度间隔对齐（300s）—— 缓存的作用是**去重同一轮内的多次调用**
    （build_brief 与兜底模板各调一次），不是跨轮复用。跨轮复用会让热点榜
    与同一条消息里的其它数字不同步，见 HOT_TTL 的说明。
    """
    if not force and os.path.exists(HOT_CACHE):
        try:
            c = M._load_json(HOT_CACHE, {})
            # ts 可能被外部改坏（缓存文件手工编辑/半截写入），非数值相减会抛 TypeError
            ts = c.get("ts")
            age = time.time() - ts if M.isnum(ts) else 1e9
            # ⚠️ 时钟回拨（NTP 校时/手动改表）会让 `time.time() - ts` 为**负**，
            # 于是 `age < ttl` 恒真 —— 把"未来时刻"的缓存当成最新鲜的，
            # 且 `_cached` 会被写成负数显示给用户（「缓存 -300 秒」）。
            # 负 age 一律当未命中重算（重算的代价是 2 次 clist，比用错数据便宜）。
            if M.isnum(ts) and age < 0:
                M.log(f"hot 缓存时间戳在未来（时钟回拨？ts-now="
                      f"{-age:.0f}s），重算", "FEED")
                age = 1e9
            # ⚠️ 命中缓存时必须校验 **schema 完整性**，不能只判 data 非空。
            # `data` 是 {"strong": [...], "pool": [...], ...}，而旧版本写入的
            # schema、手工编辑、部分写入都可能缺 "strong" 键。实测：
            #   data 有 strong -> 正常返回
            #   data 缺 strong -> **仍被当有效缓存返回**，下游
            #     `if hot and hot.get("strong")` 为假 -> 热点板块段与
            #     相关新闻段**静默消失**（news_txt 为空串），日志里零记录。
            # ts 的 isnum 守卫与半截 JSON 的降级都是对的（实测走重算），
            # 唯独缺键这一条漏了。缺键一律视为未命中，走重算。
            # ⚠️ `top_up`/`top_dn` 也必须查（2026-10-12）：旧版本写下的缓存
            # **没有这两个键**，命中后 build_brief 取到 None —— 涨跌榜段
            # 静默消失（且旧缓存 30 分钟才过期）。与 "strong" 缺键同型：
            # 键在不在必须逐个查，不能只查一个代表。
            if age < ttl and isinstance(c.get("data"), dict) \
                    and "strong" in c["data"] \
                    and "top_up" in c["data"] and "top_dn" in c["data"] \
                    and (c["data"].get("start") is None
                         or isinstance(c["data"].get("start"), list)):
                d = c["data"]
                d["_cached"] = int(age)
                return d
            if age < ttl and c.get("data"):
                M.log("hot 缓存 schema 不完整（缺 strong/top_up/top_dn），重算", "FEED")
        except Exception:
            pass
    r = analyze(top=max(top, 8))
    if r.get("pool"):
        try:
            # mem 是 set，json 不可序列化 —— 必须剥离后再写，
            # 否则 json.dump 抛 TypeError 被静默吞掉，缓存永不生效。
            def _slim(d):
                if isinstance(d, dict):
                    return {k: (sorted(v) if k == "mem" and isinstance(v, set)
                                else _slim(v)) for k, v in d.items()}
                if isinstance(d, list): return [_slim(x) for x in d]
                return d
            # 原子替换，避免被 kill 时截断
            # ⚠ 2026-11-10（Codex P2）：`_atomic_write_json` 失败时**返回 False 而不报错**，
            # 原写法只 catch 异常 → 写盘失败一点痕迹都没有（实测：
            # 把缓存父目录换成普通文件后真实写失败，日志 0 行）。
            # 而缓存失效会让每轮重算热点榜（耗配额、变慢）。
            _ok_w = M._atomic_write_json(
                HOT_CACHE, {"ts": time.time(), "data": _slim(r)})
            if _ok_w is False:
                _log("hot 缓存写入返回 False（未落盘）—— "
                     "下一轮将重算", "ERR")
        except Exception as e:
            _log(f"hot 缓存写入失败: {e}", "ERR")
    r["_cached"] = 0
    return r


# ---------- 量比（f10 已可用；自算是精算，失败时退回 f10）----------
VR_BASE_FILE = p_state("vr_base.json")

VR_BUDGET = 80         # 量比自算的整轮时间上限（秒）。
                       # 预算分配（cron 间隔 300s，留 30s 余量）：
                       #   基础取数 ~10s + members 40s + 量比 80s + AI 140s = 270s
                       # 只有全新环境的首轮会吃满这些预算；缓存建立后稳态约 50s
                       # 冷启动首轮算不完是正常的，下轮续算（结果落 vr_base.json 缓存）
                       # 注意：analyze 在最坏情况下 AI 会吃 2×900s，本函数的预算
                       # 只约束它自己这一段，不参与那个最坏值（见 LOCK_STALE 注释）

def _vr_load():
    """载入当日量比基线（跨进程复用，避免每轮 cron 重复拉K线）。"""
    # ⚠️ 返回值必须是**字典**。vr_base.json 在磁盘上，可能被手工编辑、
    # 或来自旧版本格式（"base" 写成列表）—— _load_json 只保证"是合法 JSON"，
    # 不保证形状。实测：base 为列表时 _VR_BASE 变成 list，
    # kline_stats 里 `_VR_BASE.get(key)` 抛 AttributeError，
    # 整个热点分析（含 build_brief 的热点段）失效。
    try:
        d = M._load_json(VR_BASE_FILE, {})
        if not isinstance(d, dict):
            M.log(f"vr_base.json 顶层不是对象（{type(d).__name__}），按空处理", "CFG")
            return {}
        if d.get("date") == datetime.date.today().isoformat():
            b = d.get("base")
            if isinstance(b, dict):
                return b
            if b is not None:
                M.log(f"vr_base.json 的 base 不是对象（{type(b).__name__}），按空处理", "CFG")
    except Exception:
        pass
    return {}

def _vr_save_one(key, val):
    """把**单个**板块的量比基准合并写回（flock 内重读 → 只改这一个键）。

    ⚠️ 2026-11-05（P2）：原实现把进程启动时加载的整份 `_VR_BASE` 整份写回
    （`_vr_save(_VR_BASE)`）—— analyze / collect / webui 多进程同跑时，
    后写的会把其它进程刚算好的板块整批覆盖（lost-update），表现为
    "某些板块的量比反复重算、启动榜抖动"。只写自己算出的那个键。
    写盘形状与 `_vr_load` 同源：`{"date": 当日, "base": {...}}`；
    date 不是今天则整份作废（与 `_vr_load` 的跨日判据一致）。
    """
    _today = datetime.date.today().isoformat()

    def _mut(d):
        if d.get("date") != _today:
            d["date"] = _today
            d["base"] = {}
        _b = d.get("base")
        if not isinstance(_b, dict):
            _b = d["base"] = {}
        _b[key] = val
        return True

    M._locked_update(VR_BASE_FILE, _mut)

_VR_BASE = _vr_load()
# ⚠️ 2026-10-22：原先是 ("https://push2test", "https://push2delay")，
# 而 push2delay 的 HTTPS 侧 **0/8 全端点 rc=52**（硬封，见 marketlib 主机矩阵）
# —— 第二台是纯白打，每轮为每个板块多付一次必然失败的请求。
#
# ⚠️ 但**不能**把 push2his（无论 HTTP 还是 HTTPS）当第二候选：它的板块
# 日K是**另一个指数基点**（实测 90.BK1030 近 5 日恒定 ratio=0.986336，
# 见 marketlib._codes_for_host）。本函数算的是**成交额的 5 日均值** ——
# 成交额在两台上确实逐位一致，所以比值**当前**安全；但同一条K线里的
# 收盘价（px）会喂给 `ret63 = (px[-1]/px[-63]-1)*100`，那个字段是
# **比率型、看起来安全**、实际会在兜底生效时把"60 日涨幅"整体平移。
# 一条序列里同时有安全列与危险列时，只按"我要的那列安全"来选源，
# 是在赌将来没人加第二个消费者 —— 这个项目已经因此栽过多次。
# 故第二候选**留空**（只保留 push2test）：取不到就让量比走 base=0 的
# 默认路径（下一轮再试），比混入另一基点的序列好。
# 若将来要恢复冗余，正确做法是**只取成交额列**并显式写明"不得用于
# 任何跨日价格比率"，而不是直接换 URL。
# ⚠️⚠️2026-11-21（P0）：剔除"不可能存在的交易日"的K线。
#
#与 marketlib._kline_ohlc同一判据（那边只作用于 levels，本文件的量比/
#涨幅路径当时没覆盖）。`[实测]`2026-11-21（周六）push2test给出
#2026-11-21收2257.74的假根，单日 +19.3%、成交额仅3.04亿
# （真实交易日约1600亿）——它会同时污染 ret63与5日均额 base,
#而 base会写进 vr_base.json缓存**一整天**。
def _drop_impossible_bars(rows):
    _t = datetime.date.today()
    _out = []
    for r in rows:
        _d = None
        try: _d = datetime.date.fromisoformat(str(r[0])[:10])
        except Exception: _d = None
        if _d is None: continue
        if _d > _t: continue
        if not M.is_trading_day(_d, warn=False): continue
        _out.append(r)
    return _out


VR_HOSTS = (M.EM_HIS_HTTP,)


# 日内累计成交占比曲线（实测拟合，单位：分钟 -> 累计占比）
# 数据来源：5 个板块真实分时 trends2 统计
_CUM_PTS = [(30,0.376),(60,0.518),(90,0.610),(120,0.695),
            (150,0.778),(180,0.834),(210,0.893),(240,1.000)]

def _cum_ratio(elapsed):
    """把"已过分钟数"换算成"日内累计成交占比"，消除 U 型分布偏差。"""
    if elapsed >= 240: return 1.0
    # ⚠️ 必须夹住 0：负 elapsed 会返回**负的累计占比**，进而让量比变成负数
    # （today_amt / (base * 负值)）。kline_stats 在 elapsed < 15 时提前 return，
    # 所以当前不可达 —— 但这是公开函数，任何新调用方传负数就会中招。
    elapsed = max(0, elapsed)
    if elapsed <= _CUM_PTS[0][0]:
        return _CUM_PTS[0][1] * (elapsed / _CUM_PTS[0][0])
    for i in range(1, len(_CUM_PTS)):
        x0, y0 = _CUM_PTS[i-1]; x1, y1 = _CUM_PTS[i]
        if elapsed <= x1:
            return y0 + (y1-y0) * (elapsed-x0) / (x1-x0)
    return 1.0

def kline_stats(code, today_amt, now=None, budget_s=None):
    """量比 = 今日成交额 / 近5日同期均额。

    ⚠️⚠️ 2026-11-24 更正：**`f10` 对板块已经有效了**。
    原注释写「f10 对板块和个股都返回 "-"，无法直接取用」—— 那在写下时
    是事实，但现在不成立，而这句**过时的话**正是 15:09 那次缺陷的温床：
    代码里到处假定"只有自算值可信"，于是自算一失败就只能报「量比未知」，
    把一个现成的真值扔掉。

    `[实测]` 2026-11-24 全量扫描（行业 300 + 概念 300）：
        f10 有效 **600/600**（无一为 "-" 或 0）
    与自算值逐条对比 14 例（同一时刻、同一批板块）：
        比值 0.98 ~ 1.27，中位 **1.08** —— 同一个量、同一量纲。

    两处差异来源（已知、不影响排序）：自算用成交额口径并按 U 型累计曲线
    折算，接口 f10 的口径未公开。二者都是"放量/缩量"的可靠信号。

    所以本函数现在的定位是**精算**，不是唯一来源：失败时上层退回 f10
    （见 `analyze()` 里 `_vr_src` 的三态）。**不要**再假定"没有自算值就等于未知"。
    用成交额而非成交量：避免不同日价格差异带来的偏差。

    基线按 (代码, 日期) 缓存 —— 5日均值当日不变，所以每个板块每天只请求
    一次，之后全天零请求。今日成交额直接用 clist 已返回的 f6，不额外拉取。
    """
    import datetime as _dt
    _kt0 = time.monotonic()      # 本次调用的起点（预算基准）
    now = now or _dt.datetime.now()
    hm = now.hour*60 + now.minute
    if hm <= 9*60+30: return None, None
    if hm >= 15*60: elapsed = 240
    elif hm >= 13*60: elapsed = 120 + (hm - 13*60)
    elif hm > 11*60+30: elapsed = 120          # 午休：无新成交，冻结在上午收盘
    else: elapsed = hm - 9*60-30
    if elapsed < 15: return None, None    # 开盘太短，噪声大

    key = f"{code}:{now.date().isoformat()}"
    c = _VR_BASE.get(key)
    # ⚠️ 光校验 _VR_BASE 是 dict **不够**，还要校验**取出来的值**是 dict。
    # vr_base.json 在磁盘上，可能被手工编辑 / 来自旧版本 / 半截写入，写出
    # `"base": {"90.BK1036:2026-09-27": "坏值"}` 这种"顶层形状全对、某个值形状错"
    # 的形态。实测（2026-09-27，三种值形态）：
    #   值为 str  -> AttributeError: 'str' object has no attribute 'get'
    #   值为 list -> AttributeError: 'list' object has no attribute 'get'
    #   值为 None -> 不抛（下面 _num 兜住），但同样是无效条目
    # 异常冒到调用方 analyze.py:2210，那里只记一行无堆栈的 "HOT EXC" 就把 hot
    # 置 None —— 后果是 build_brief 的【热点】段与紧随其后的**相关新闻段**
    # 整轮消失。已实测该链路（合成候选池 + 坏 vr_base）。
    # 当作**未命中**处理（而非丢弃）：走重算，成功后会把正确值写回，等于自愈。
    if not isinstance(c, dict):
        c = None
    if c is None:
        hist = []; ret63 = None
        for host in VR_HOSTS:
            # ⚠ 2026-11-10（Codex P2）：预算必须**穿进单次 cj**。
            # 原来 VR_BUDGET 只在板块之间检查，而一条请求链最坏是
            # 直连 3 次重试 + 代理 + firecrawl ≈ 175s；实测一条链吃掉 191.5s。
            # 于是“80s 预算”根本管不住已在跑的那一条。
            if budget_s is not None:
                _remain = budget_s - (time.monotonic() - _kt0)
                if _remain <= 1:
                    _log(f"量比取数预算耗尽（剩 {_remain:.1f}s），"
                         f"板块 {code} 放弃自算", "FEED")
                    break
            # ⚠⚠ 2026-11-11（审查，P2）：上面那段注释写着「预算必须
            # **穿进单次 cj**」，但实际调用**没传 timeout** ——
            # 修了一半：每台主机仍按默认 20s、重试 3 次、再加代理与
            # firecrawl，单轮最坏 191.5s —— 80s 预算依然管不住已在跑的那条。
            # 把剩余预算换算成本次请求的超时（至少 5s），让链路真的受预算约束。
            _to = None
            if budget_s is not None:
                _to = max(5, int(budget_s - (time.monotonic() - _kt0)))
            d = _cj(f"{host}/stock/kline/get?secid=90.{code}"
                    f"&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
                    f"&klt=101&fqt=1&end=20500101&lmt=70&ut={UT}",
                    **({"timeout": _to} if _to else {}))
            _inner = _has_data(d, "klines")
            if _inner:
                # 条目必须是 str 才能 split。_has_data 只证明了 klines 是
                # 非空真值 —— 若主机把 klines 写成 [null, 42] 或嵌套 list，
                # `r.split` 会 AttributeError（实测条目为 None 时抛）。
                # 用 isinstance 过滤而不是 try 包住：这里失败会整条 K 线
                # 弃用，而逐条过滤还能救回其中正常的那些。
                rows = [r.split(",") for r in _inner["klines"]
                        if isinstance(r, str)]
                rows = _drop_impossible_bars(rows)
                rows = [r for r in rows if r[0] != now.date().isoformat()]
                for q in rows:
                    try: hist.append(float(q[6]))       # f57=成交额
                    except (ValueError, IndexError): pass
                px = []
                for q in rows:
                    try: px.append(float(q[2]))         # f53=收盘
                    except (ValueError, IndexError): pass
                # f24 在备用主机上不可信（实测 13/14 不符），故自算涨跌幅。
                # ⚠️ 实际跨度是 px[-1]/px[-63] = **62 个交易日**（约 3 个月），
                # 不是"60 日"。原先字段名/注释都写 60 日，与实现差 2 天 ——
                # 量级上无实质影响，但排查时按"60日"去核对会得出错误结论。
                # 保留 f24 这个字段名（下游已依赖），注释说明真实口径。
                if len(px) >= 63 and px[-63] > 0:
                    ret63 = (px[-1]/px[-63]-1)*100
                break
        base = (sum(hist[-5:])/len(hist[-5:])) if hist else 0.0
        c = {"base": base, "ret63": ret63}
        # 取数失败(base=0)不落盘：否则该板块当天被永久锁死在默认量比，不再重试
        if base > 0:
            _VR_BASE[key] = c
            _vr_save_one(key, c)
    # ⚠️ `.get(k, default)` 只在**键缺失**时给默认值，兜不住"键存在但值为 None"。
    # c 来自 _VR_BASE —— 那是 vr_base.json **反序列化**来的，JSON 里写
    # `"base": null` 就得到 None。实测：None <= 0 抛 TypeError，冒到
    # analyze_cached -> build_brief 的热点段整段消失。
    # 数值一律走 _num（语义同 marketlib.isnum，排除 nan/inf/bool）。
    base = _num(c.get("base"), 0.0); ret63 = c.get("ret63")
    if not M.isnum(base) or base <= 0: return None, ret63
    # A股日内成交量是 U 型分布，不是均匀分布。用实测累计成交占比曲线折算，
    # 否则早盘量比系统性虚高：10:00 实际已成交 37.6%（而非均匀假设的 12.5%），
    # 会让一个成交额恰好等于均值的板块被算成量比 3.1，启动榜门槛失效。
    return today_amt / (base * _cum_ratio(elapsed)), ret63

def analyze(top=8):
    # ⚠️ 2026-11-20（P1）：自己的 40s 还要再被**全局取数上限**钳一次。
    # 三个子预算（clist 60s + members 40s + 量比 80s）能叠加到 180s，
    # 而 AI 的预算是「240s − 取数耗时」—— 取数越慢 AI 越没时间，
    # 慢轮必然退模板。见 `marketlib.DATA_BUDGET` 的实测分布。
    _mem_deadline[0] = time.time() + M.data_cap(MEMBERS_BUDGET, "members")
    raw=fetch_sectors()
    rows=[]
    for x in raw:
        # ⚠️ f14/f12 必须用 .get()：clist 偶发返回缺字段的行（接口改版、
        # 限流返回部分字段），裸下标会 KeyError 冒到 analyze() 顶层 ——
        # 整轮热点榜全丢，而不是少一个板块。同类守卫在 collect.py 的快照行
        # 已加过（那里是 f3/f14 三个字段全裸下标）。
        n=x.get("f14")
        if not n: continue
        if is_statistical(n): continue
        chg=x.get("f3"); f24=x.get("f24")
        if not M.isnum(chg): continue
        # 注：此处不能用 f24 过滤 —— 备用主机的 f24 不可信（实测偏差达 32pp）。
        # 真正的"已涨完"过滤在自算 ret63 之后（见 analyze 尾部）。
        amt=_num(x.get("f6"))
        if amt < 3e8: continue          # 成交额<3亿：微型板块会劫持占比类指标
        f134=x.get("f134")
        if M.isnum(f134) and f134>=800: continue  # 宽基/统计口径兜底
        # members 要发一次请求，必须放在所有免费过滤之后 —— 原先排在成交额
        # 过滤之前，冷启动时会为 1000 个板块各拉一次成分股
        _code = x.get("f12")
        if not _code: continue
        ms=members(_code)
        # _tag 由 fetch_sectors() 无条件注入（第 67 行），故生产路径下必存在；
        # 但 .get() 与同函数里其它字段（f14/f3/f12/f134…）保持同一口径 ——
        # 少一个裸下标，就少一个"上游换实现时整轮热点榜全丢"的入口。
        _tag = x.get("_tag") or ""
        if len(ms)<(8 if _tag=="ind" else 10): continue
        up=_num(x.get("f104")); dn=_num(x.get("f105"))
        rows.append(dict(tag=_tag,code=_code,name=n,n=len(ms),mem=ms,
            chg=chg,f24=f24 if M.isnum(f24) else 0.0,
            pct=_num(x.get("f184")), xlpct=_num(x.get("f69")),
            vr=_num(x.get("f10"), 1.0), turn=_num(x.get("f8")),
            # ⚠️⚠️ `_vr_ok` 必须在**构造时**就给出（2026-11-24，P1）。
            # 原先这个键只在"走完 kline_stats 那一趟"时才写 —— 于是
            # **量比预算一耗尽**（`for r in head` 一进门就 break，`vr_done=0`），
            # 整批 head 行**根本没有这个键**，而渲染层写的是
            # `r.get('_vr_ok', True)`（默认**真**）→ 把没算过的 `vr` 当成实测值印出去。
            # `[实测]` 2026-11-24 15:09「收盘定局」：日志「量比预算 80s 用尽，
            # 已算 0/32 个」，而推送里照样写「量比1.0」。
            #
            # ⚠️ 但**不能**一律标 False —— 那会走向另一个极端：把
            # `f10` 这个**真值**也一起藏起来。`[实测]` 2026-11-24 全量扫描
            # （行业+概念各取 300 条）：**f10 有效 600/600**。
            # 与自算值逐条对比 14 例：比值 0.98~1.27（中位 1.08）——
            # 同一个量、同一量纲，不是兜底常数。
            #
            # 所以这里分**三态**，各有自己的出处（`_vr_src`）：
            #   · 自算成功   -> vr=自算值,  _vr_ok=True, _vr_src="kline"
            #   · 只有 f10   -> vr=f10,    _vr_ok=True, _vr_src="f10"
            #   · 两样都没有 -> vr=1.0,    _vr_ok=False（兜底常数，不许印成实测）
            _vr_ok=M.isnum(x.get("f10")) and x["f10"] > 0,
            _vr_src=("f10" if (M.isnum(x.get("f10")) and x["f10"] > 0) else None),
            inflow=_num(x.get("f62")), amt=amt, f134=f134, up=up, dn=dn,
            breadth=up/(up+dn) if (up+dn) else 0.5, lead=x.get("f128") or ""))
    if not rows:
        # ⚠️ 候选池为空有**两个完全不同的原因**，原日志只写「clist 可能被限流」
        # 把排查方向带偏了：
        #   ① clist 真的没取到数据（限流 / 接口故障）—— `raw` 为空；
        #   ② clist 取到了数据，但**全被本地过滤条件滤光** —— `raw` 非空。
        # ②才是常态：09:17 那一轮是**集合竞价时段**，当日成交额 f6 还没开始累计，
        # 所有板块 f6=0 < 3 亿门槛（`amt < 3e8`），于是 1000 个板块被滤光、
        # 池子空掉。实测 2026-10-22 收盘后同一份数据 872/1012 个板块能过门槛，
        # 证明 clist 当时是通的。
        # `[实测]` 10-09 至 10-22 连续 10 个交易日都在 09:17~09:21 报这一行，
        # 每次都被当成「限流」排查 —— 而真正的原因是盘前成交额为 0。
        # 故按 raw 是否为空分开报，并把真实原因写清楚。
        if not raw:
            _log("hot: 候选池为空 —— clist 未取到任何板块数据（限流或接口故障）")
        else:
            _log(f"hot: 候选池为空 —— clist 取到 {len(raw)} 个板块但全被本地过滤"
                 f"条件滤光（盘前/低成交额时段 f6 尚未累计，低于 3 亿门槛）")
        return {"strong":[],"start":[],"top_up":[],"top_dn":[],
                "pool":0,"dedup":0}

    # 第一轮打分：不含量比（自算需逐个拉K线，故延后；f10 已在构造时入行）
    def _score(rs, with_vr):
        zc=_z([r["chg"] for r in rs]); zp=_z([_cl(r["pct"],-30,30) for r in rs])
        zx=_z([_cl(r["xlpct"],-30,30) for r in rs])
        zt=_z([_cl(r["turn"],0,15) for r in rs]); zb=_z([r["breadth"] for r in rs])
        z24=_z([_cl(r["f24"],-50,60) for r in rs])
        zv=_z([_cl(r["vr"],0,5) for r in rs]) if with_vr else (lambda v: 0.0)
        wv = 0.20 if with_vr else 0.0
        for r in rs:
            r["score"]=(0.30*zc(r["chg"]) + 0.25*zp(_cl(r["pct"],-30,30))
                      + wv*zv(_cl(r["vr"],0,5)) + 0.15*zx(_cl(r["xlpct"],-30,30))
                      + 0.10*zt(_cl(r["turn"],0,15)) + 0.10*zb(r["breadth"])
                      - 0.35*z24(_cl(r["f24"],-50,60)))      # 位置惩罚
    _score(rows, with_vr=False)
    rows.sort(key=lambda r:-r["score"])

    # 去重：贪心逐对（连通分量会链式合并 —— 实测最大分量吞掉152个不相似板块）
    # 双度量：Jaccard>0.60 或 重叠系数>0.80（后者解决父子包含关系，Jaccard对其是盲的）
    kept=[]
    for r in rows:                      # rows 已按 score 降序
        dup=False
        for k in kept:
            inter=len(r["mem"]&k["mem"])
            if not inter: continue
            jc=inter/len(r["mem"]|k["mem"])
            ov=inter/min(len(r["mem"]),len(k["mem"]))
            if jc>0.60 or ov>0.80: dup=True; break
        if not dup: kept.append(r)
    # 量比自算：只对去重后的头部算（每天每板块仅1次请求，之后走缓存）。
    # 注意：只有头部这批用了含VR的分数，尾部保持不含VR的分数，
    # 两种量纲不可混排 —— 所以只重排头部，尾部按原序接在后面。
    # 冷启动时把 VR_N 收窄：首轮没有基线，每个板块都要现拉 K 线，
    # 64 个 × 1.5s ≈ 96s > VR_BUDGET(80s)，实测只能算完 8/64 就超时，
    # 等于首轮热点榜几乎全靠兜底量比 1.0，排序失真。
    # 有基线后（当日已算过）再放开到完整数量。
    _has_base = bool(_vr_load())
    VR_N = max(top*8, 40) if _has_base else max(top*4, 20)
    head, tail = kept[:VR_N], kept[VR_N:]
    # 冷启动（当天首次跑）要现拉这 40 个板块的 K 线，每个 3 次重试 × 25s 超时，
    # 最坏 40×3×25 = 3000 秒 —— 实测沙箱冷启动跑 6 分 42 秒仍在算。
    # 而 cron 间隔只有 300 秒，这轮跑不完下轮就叠上来。
    # 故设整轮硬预算：超时就用已有基线（vr 缺失的按 1.0 处理，排序略失真但能出报告）。
    # 同上：自身 80s 预算再被全局上限钳一次（见 `_mem_deadline` 的注释）。
    vr_deadline = time.time() + M.data_cap(VR_BUDGET, "量比")
    vr_done = 0
    for r in head:
        if time.time() > vr_deadline:
            _log(f"量比预算 {VR_BUDGET}s 用尽，已算 {vr_done}/{len(head)} 个，"
                 f"其余按基线 1.0 处理（下轮续算）")
            break
        # r 由本文件 rows.append(dict(...)) 构造，但 code/amt 可能为 None
        # （上游板块列表缺字段时）—— 裸下标虽不抛，kline_stats(None) 会走
        # 网络请求再失败，白等一轮超时。显式跳过更快也更清楚。
        _rc = r.get("code")
        if not _rc:
            # ⚠⚠ 2026-11-11（审查，P2）：`vr` 缺失时不能当成真实的 1.0。
            # 与「缺失金额显示 ≈0」是同一类缺陷：读者看到「量比1.0」
            # 会当成「交量正常」的实测值，而事实是**没取到**。
            # `vr` 本身保留 1.0 是为了排序不因缺键而报错；
            # 另记 `_vr_ok=False`，渲染层据此输出「量比未知」。
            r["vr"] = 1.0; r["_f24_ok"] = False; r["_vr_ok"] = False
            continue
        # 把**剩余**预算传进去：否则「板块之间才检查」等于没有上界
        vr, ret63 = kline_stats(_rc, r.get("amt"),
                                budget_s=max(1.0, vr_deadline - time.time()))
        vr_done += 1
        # ⚠ `_vr_ok` 与 `vr` 同一行给出：数据层保留数值（排序用），
        # 渲染层看标记（决定该显示数字还是「未知」）。
        if M.isnum(vr):
            r["vr"] = vr
            r["_vr_ok"] = True
            r["_vr_src"] = "kline"
        else:
            # 自算失败（K 线取不到 / 预算耗尽 / 页数不够）：
            # **退回 f10**（构造时已放进 vr），而不是写 1.0 或标"未知"。
            # f10 是接口的真值，实测 600/600 有效、与自算比值 0.98~1.27。
            # 若连 f10 都没有，`_vr_ok` 保持构造时的 False。
            r["_vr_ok"] = bool(M.isnum(r.get("vr")) and r["vr"] > 0
                               and r.get("_vr_src") == "f10")
        # f24 在备用主机上不可信（实测13/14不符），用 K 线自算的 63 日涨跌幅覆盖。
        #
        # ⚠️ 取数失败（ret63=None）时**必须显式标记不可信**。
        # 原实现只在成功分支里 `r["_f24_ok"]=True`，失败分支什么都不写 ——
        # 于是 head 行（已尝试自算、但失败）既不进下面 tail 的
        # "标记为不可信"循环（那个循环只遍历 tail），也没有 _f24_ok，
        # 而阶段标签用的是 `r.get("_f24_ok", False)`（正确判 False），
        # brief() 却**无条件打印 r["f24"]**（接口原值）。
        # 实测：K线取不到时 brief 输出 `60日+88.0%`（接口原值），
        # 而同一行的 stage 显示「延续（位置未知）」—— 自相矛盾，
        # 且这个数字会原样进 AI prompt，被当成"这个板块 60 日涨了 88%"。
        # 标记出来，让下游（brief / 启动榜）能一致地判断。
        if M.isnum(ret63):
            r["f24"] = ret63
            r["_f24_ok"] = True        # 标记"这个 f24 是自算的、可信"
        else:
            r["_f24_ok"] = False       # 明确标记不可信（与 tail 一致）
    # 未算到的补 1.0，保证后续 _score 不会因缺键出错
    for r in head[vr_done:]:
        # 本轮预算耗尽、没轮到算的：同样不能冒充实测值。
        #
        # ⚠️⚠️ 原写法是 `if "vr" not in r:` —— 而 `vr` 在**构造时**就已经写进
        # 每一行了（`vr=_num(x.get("f10"), 1.0)`），所以这个条件**恒为假**，
        # 整个循环是个空操作：既不补 1.0（本来就有），也**不标记 `_vr_ok`**。
        # 一道写着"不能冒充"的守卫，实际什么都没做 —— 典型的
        # 「判据写在注释里、没写在代码里」。
        #
        # 现在：**只**把"连 f10 都没有"的行留给兜底常数语义。
        # 构造时已按 f10 定过 `_vr_ok`，这里不动它 —— 一律置 False 会把
        # 600/600 都有效的 f10 一起藏掉（那是真值，不是兜底）。
        if not M.isnum(r.get("vr")) or r["vr"] <= 0:
            r["vr"] = 1.0
            r["_vr_ok"] = False
            r["_vr_src"] = None
    # ⚠️ tail（head 之外的部分）的 f24 是**接口原值，不可信** ——
    # 自算校正只覆盖 head 的前 VR_N 个。原实现让不可信的 f24 流入两处：
    #   ① 阶段标签（rows 全量遍历）—— "高位/超跌/启动"判断失真
    #   ② 启动榜筛选（`r["f24"]<=10`）—— 不可信值绕过"已涨完"过滤
    # 实测构造数据：62 日 +80% 的板块能进强势榜。
    # 这里明确打上不可信标记，下游据此跳过而非误用。
    for r in tail:
        if not r.get("_f24_ok"):
            r["_f24_ok"] = False
    _score(head, with_vr=True)
    head.sort(key=lambda r:-r["score"])
    # 用自算的 63 日涨跌幅做"已涨完"过滤（接口 f24 不可信，前面已跳过该过滤）
    # 用自算的 63 日涨跌幅做"已涨完"过滤 —— 只对**可信**的行生效。
    # 不可信的行若也过滤，等于拿错误数据做排除（可能误杀真机会）；
    # 不过滤则可能放行"已涨完"的板块。两害相权：宁可放行（用户自己看得到
    # 涨幅），也不要因错误数据误杀。日志里已有 _f24_ok 标记可查。
    # ⚠️ 「已涨完」过滤**只作用于选股（strong/start）**，不作用于涨跌榜
    # （2026-11-01 修，P1）。旧写法直接把 head 过滤后合并成 kept，而下面
    # top_up/top_dn 取自 kept —— 于是一个 63 日涨幅 >40% 的热门题材，
    # 哪怕**当天涨 9.5%** 也不进涨幅榜；高位板块当天跌 9.5% 同样不进跌幅榜。
    # `[实测]` 沙箱构造：+9.5% 的板块在 top_up 里查无此名，榜上是 +2.1/+2.0/+1.9。
    # 涨跌榜回答"今天涨/跌最多的三个板块"，与"它过去 63 天涨了多少"无关；
    # 把它排除等于对读者隐瞒当天最极端的两个方向。
    # 故先留一份 `kept_all`（f24 排除之前的全池）供涨跌榜用，
    # `kept` 仍是被过滤后的池，供 strong/start 用。
    kept_all = head + tail
    head = [r for r in head
            if not (r.get("_f24_ok") and M.isnum(r.get("f24")) and r["f24"]>40)]
    kept = head + tail

    # 阶段标签（用快照字段，无需额外请求）。
    # ⚠️ 只信自算过的 f24：接口原值在备用主机上实测 13/14 不符（偏差达 32pp），
    # 用它判"高位/超跌"会给出错误标签。不可信时退化为只按当日涨跌判断。
    for r in rows:
        # 注：原先这里还 `or r.get("_f24_src") == "kline"` —— 但
        # "_f24_src" 在整个代码库里**只被读、从未被写**（实测 grep：
        # 本文件出现 1 次，就是这一行），恒为 None，该子条件恒 False。
        # 已删除，避免读者以为还有第二条可信来源。
        _f24_ok = r.get("_f24_ok", False)
        _f = r["f24"] if _f24_ok else None
        if _f is not None and r["vr"]>=1.5 and 0<r["chg"]<=2.5 and _f<=10 and r["pct"]>=3:
            r["stage"]="启动"
        elif _f is not None and (_f>15 or (_f>8 and r["chg"]<0)):
            r["stage"]="高位"
        elif _f is not None and _f<-10:
            r["stage"]="超跌"
        elif _f is None:
            r["stage"]="延续（位置未知）"
        else:
            r["stage"]="延续"

    # 启动榜：放量 + 主力吸筹 + 涨幅温和 + 位置低
    strong_set = {r["code"] for r in kept[:top]}
    start=[r for r in kept if r["code"] not in strong_set
           and r["vr"]>=1.5 and 0<r["chg"]<=2.5
           and r.get("_f24_ok", False) and r["f24"]<=10
           and r["pct"]>=3 and r["up"]>r["dn"]]
    start.sort(key=lambda r:-(r["pct"]*_cl(r["vr"],0,4)))
    # 涨跌榜 TOP3（纯当日涨跌幅，2026-10-12 加）。
    # ⚠️ 排序池用 `kept_all`（**去重 + 构造过滤，但不含 f24「已涨完」排除**），
    # 不能是 `rows`，也不能是 `kept`：
    #   · rows 未去重 —— 父子板块（"半导体"vs"半导体设备"）会各占一席，
    #     一条消息里出现两个高度重叠的"上涨第1/第2"，读者以为是两个方向。
    #   · rows 的过滤在构造时就生效了（is_statistical / amt>=3亿 / f134<800 /
    #     成分股数），kept_all 继承这些过滤，正是涨跌榜该有的：不想要"昨日涨停"
    #     "次新股""全部A股"这类统计口径，也不想要成交 2000 万的僵尸板块。
    #   · **kept 又多了 f24>40 的排除** —— 那会让当天最热/最惨的板块消失
    #     （见上方 kept_all 的注释，2026-11-01 修）。
    # 为什么不用 strong/start：那两个是**算法打分/形态筛选**的结果，
    # 与"今天涨得最多的三个板块"是两种问题；用户明确要的是后者。
    # 数值守卫：chg 已在 rows 构造时用 M.isnum 过滤，但池里可能混入
    # tail（未自算 f24 的行），chg 仍可信 —— 排序时再兜一次 None。
    top_dn, top_up = _split_boards(kept_all)
    return {"strong":kept[:top],"start":start[:top],
            "top_up":top_up, "top_dn":top_dn,
            "pool":len(rows),"dedup":len(kept_all)}


def _split_boards(kept_all):
    """涨跌榜排序与切片。**抽成纯函数**供自检用固定夹具验证。

    ⚠⚠ 2026-11-11（审查，P2）：原先自检在自己手里 `sorted(...)` 一遝、
    再断言它降/升序 —— 自闭环，对固定夹具必然成立。把排序键取反号时
    自检**零错误**。抽出纯函数后，生产与自检用同一份代码，
    自检就能用固定夹具直接验证**真正在用的那份**。
    这是「自检验证自己」一类缺陷的标准修法：把被验证的东西抽成
    纯函数，夹具可控，断言才有判别力。
    """
    _by_chg = sorted([r for r in kept_all if M.isnum(r.get("chg"))],
                     key=lambda r: r["chg"])
    top_dn = _by_chg[:3]
    top_up = list(reversed(_by_chg[-3:]))
    return top_dn, top_up

# ⚠️ 这里曾有一个 `brief(hot)` 格式化函数，**全库零调用点**（实测 grep
# `HOT.brief|hot.brief|\.brief\(` 无结果）—— 真正在用的格式化在
# analyze.build_brief 里。留着它的代价不只是"多一段死代码"：
# 它的 f24 守卫（只在 _f24_ok 时打印 60 日涨幅）是**唯一**写对的地方，
# 而实际生效的那份是无条件打印的。于是"修好了"与"生效了"分家：
# 读代码的人看到 hot.brief 已经防住了错误数据，就不会再去查
# analyze.build_brief —— 而那才是真正往 AI prompt 里塞数字的那条路径。
# 已删除，守卫直接落在 analyze.build_brief 上（那边有注释指向此处）。

if __name__=="__main__":
    t=time.time()
    h=analyze()
    print(f"用时 {time.time()-t:.1f}s")
    print(h)
