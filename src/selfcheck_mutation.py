#!/usr/bin/env python3
"""变异测试：量化 selfcheck.py 54 个检查项的整体判别力。

对每个变异（对应真实历史缺陷类），注入到被测模块 → 跑沙箱自检 →
看是否被抓住（rc≠0 或输出含 ❌）。漏网的就是检查项盲区。
"""
import os, re, signal, sys, shutil, subprocess, tempfile, time

# 生产根与源码目录（2026-10-31 分层后解耦）：
#   ROOT = ~/.market-watch      —— 运行数据根（conf/ state/ logs/）
#   SRC  = ~/.market-watch/src  —— 11 个主模块所在（变异体注入目标）
ROOT = os.path.expanduser("~/.market-watch")

# 互斥锁句柄（见 main() 的互斥闸门；模块级是为了 global 声明有落点）
_LOCK_FH = None
SRC = os.path.join(ROOT, "src")

# ---------- 本进程内探测结果夹具（`MW_PROBE_FIXTURE`）----------
#
# 为什么需要：`[WebUI 应答]` 段探的是**本机 127.0.0.1 的生产面板**，不是
# 沙箱（`MW_DIR` 只改数据目录，改不了 socket 的绑定地址）。于是这一项
# 有两类**与判别力无关**的假信号：
#   ① 宿主负载高 → 8 个端点全部超时 → 基线带 ❌ → 基线闸门当场中止，
#      整轮回归跑不起来（实测 2026-10-10 load1≈50 时连试 3 次全中止）；
#   ② `webui-port-dead` 把端口改成 8799（无人监听）后，探的是**空地址**，
#      而不是生产面板 —— 它测的是「无人监听」这个分支，而生产面板
#      此时是好的。两种故障混在同一份输出里，判据无法归因。
# 夹具 = 本进程内起三个假监听：一个正常应答（8 个端点全 200）、
# 一个 accept 后永不回复（超时）、一个**绑完立刻关闭**（必然拒连）。
# 真实生产面板从此**完全不参与**回归。
# ⚠️ 三个都要有，不能只给前两个：`webui-port-dead` 的语义是「端口没人监听
# → ConnectionRefused」，若把它的 8799 也映到**挂死**端口，两条变异体就
# 挤进同一个「超时」分支，**「拒连」这条归因分支反而无人覆盖** ——
# 而它正是排查方向不同的那一条（拒连→查进程/端口，超时→查宿主负载）。
# ⚠️ 不设该变量时行为与原先**完全一致**（真探生产端口），生产自检不受影响。
_PROBE_NORMAL_PORT = None
_PROBE_HANG_PORT = None
_PROBE_DEAD_PORT = None


_PROBE_FIXTURE_LOCK = None


def _probe_fixture_start():
    """起三个假监听 (正常应答, 永不回复, 必拒连)，返回三元组端口。

    ⚠️ **幂等**：回归全程只起一次（`apply_mutant` 的 webui-hang 分支与
    `run()` 都要读端口，而 apply_mutant 在 run 之前执行）。

    ⚠️⚠️ **必须加锁**（2026-10-28 并行化）：幂等判据是"全局端口变量非
    None"，而并行下多个 worker 会同时看到 None → 各起一套监听，只有
    最后写入的那组端口被 `run()` 读到；其余监听线程泄漏、端口被后续
    worker 覆盖，`MW_PROBE_FIXTURE` 在 worker 之间互相串台。
    加锁后与串行时**行为等价**（只有第一次真起监听）。
    ⚠️ 锁必须是**模块级、惰性建**：`threading.Lock()` 在 import 期建会
    在 fork 后跨进程复用（macOS 上 fork 的锁状态未定义）。本模块不起
    子进程式并行，只用线程池，惰性建即可。
    """
    global _PROBE_FIXTURE_LOCK
    if _PROBE_FIXTURE_LOCK is None:
        import threading as _thr
        _PROBE_FIXTURE_LOCK = _thr.Lock()
    with _PROBE_FIXTURE_LOCK:
        return _probe_fixture_start_body()


def _probe_fixture_start_body():
    import socket as _sk, threading as _th
    global _PROBE_NORMAL_PORT, _PROBE_HANG_PORT, _PROBE_DEAD_PORT
    if _PROBE_NORMAL_PORT is not None:
        return _PROBE_NORMAL_PORT, _PROBE_HANG_PORT, _PROBE_DEAD_PORT
    srv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
    srv.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0)); srv.listen(32)
    _PROBE_NORMAL_PORT = srv.getsockname()[1]

    def _http_ok():
        while True:
            try:
                c, _ = srv.accept()
            except Exception:
                return
            try:
                c.recv(65536)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                          b"Content-Type: application/json\r\n"
                          b"\r\n{}")
            except Exception:
                pass
            finally:
                try: c.close()
                except Exception: pass
    _th.Thread(target=_http_ok, daemon=True).start()

    hsrv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
    hsrv.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
    hsrv.bind(("127.0.0.1", 0)); hsrv.listen(32)
    _PROBE_HANG_PORT = hsrv.getsockname()[1]

    def _accept_forever():
        conns = []
        while True:
            try:
                c, _ = hsrv.accept()
                conns.append(c)      # 持有连接、永不写入（真"假死"形态）
            except Exception:
                return
    _th.Thread(target=_accept_forever, daemon=True).start()

    # 必拒连端口：绑定一个临时端口后**立刻关闭**，端口随即无人监听，
    # 新连接得到 RST → 客户端 `ConnectionRefusedError`，正是
    # `webui-port-dead` 要测的分支。
    #
    # ⚠️ 两种看着更"稳"的写法都不成立（2026-10-11 实测）：
    #   · 绑完**不关** —— macOS/BSD 上"已绑定但未 listen"的 socket 会把
    #     到达的 SYN **直接丢弃**（既不 RST 也不 accept），客户端挂到
    #     超时 → 报 `TimeoutError`，与 `webui-hang` 挤进同一分支，
    #     **「拒连」这条归因分支反而没人测**。
    #   · 硬编码一个数字 —— 那个端口可能真被别的进程占着，变异体会测到
    #     "有人听"，判据完全跑偏。
    # 所以：现绑现关，并且**自证**（下面真的连一次，不是拒连就换端口重试）。
    _PROBE_DEAD_PORT = None
    for _try in range(8):
        dsrv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        dsrv.bind(("127.0.0.1", 0))
        _cand = dsrv.getsockname()[1]
        dsrv.close()
        _probe = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        _probe.settimeout(2.0)
        try:
            _probe.connect(("127.0.0.1", _cand))
        except ConnectionRefusedError:
            _PROBE_DEAD_PORT = _cand     # 自证成立
            break
        except Exception:
            pass                          # 超时/其它 → 这个端口不合用，换一个
        finally:
            _probe.close()
    if _PROBE_DEAD_PORT is None:
        # 8 次都没拿到"稳定拒连"的端口（极罕见：宿主在猛开短连接）。
        # 不能拿一个"可能是超时"的端口冒充 —— 那会让 webui-port-dead
        # 测错分支。整个夹具判定失败，回归侧会当异常处理。
        raise RuntimeError("无法取得必拒连端口（8 次尝试后仍被复用）")
    return _PROBE_NORMAL_PORT, _PROBE_HANG_PORT, _PROBE_DEAD_PORT

# 每个变异: (名称, 目标文件, 原串, 替换串, 说明)
MUTANTS = [
    # --- 面板转义（[面板转义完备性] 应抓）---
    ("esc-drop-quote",
     "webui.py",
     '.replace(/"/g,"&quot;").replace(/\'/g,"&#39;")',
     '.replace(/</g,"&lt;")',
     "esc() 不再转义引号 → 属性注入"),

    # --- 面板状态词（[面板状态词映射] 应抓）---
    ("rmap-empty-value",
     "webui.py",
     'const RMAP = {no_trigger:"无触发（监控正常运行）"',
     'const RMAP = {no_trigger:""',
     "rmap 某值清空 → 前端 || 兜底显示英文枚举"),

    # --- 共享常量漂移（[常量一致性] 应抓）---
    # ⚠️ 走**特殊处理**（`apply_mutant` 里按 name 分支），不用固定锚点。
    # 原因：原锚点是 `_FALLBACK_MAX_PUSH = 6` —— 硬编码了一个**会变的数**。
    # 2026-10-22 把 analyze.MAX_PUSH 从 6 提到 10，这条锚点当场 0 匹配，
    # 而 0 匹配在框架里是**静默跳过**（apply_mutant 返回 False → 记「锚点未匹配」），
    # 不算漏网 —— 于是这条测试的判别力无声消失，正是本项目反复栽的
    # 「锚点未匹配却报全绿」那类坑（见记忆
    # market-watch-mutant-fixture-own-trigger / market-watch-duplicated-truth-breaks-anchors）。
    # 现在改成：注入时**现读** analyze.MAX_PUSH 的真值，把 webui 的兜底副本
    # 改成一个不同的数 —— 真值怎么变都不会断锚。
    ("fallback-maxpush-drift",
     "webui.py",
     "__FALLBACK_MAXPUSH_DRIFT__",
     "",
     "webui 兜底副本与 analyze.MAX_PUSH 漂移",
     "兜底副本过期 _FALLBACK_MAX_PUSH"),

    # --- 阈值函数返回 None（[阈值取值函数返回值] 应抓）---
    ("hstech-threshold-none",
     "marketlib.py",
     "    _fallback = TH.get(key)\n    if isnum(_fallback): return _fallback\n    _fallback = TH.get(\"hstech_up\")\n    if isnum(_fallback): return _fallback",
     "    return None",
     "hstech_up_threshold 返回 None → 调用方裸比较 TypeError"),

    # --- 降级链断（[网络降级链] 应抓）---
    ("fallback-chain-cut",
     "marketlib.py",
     # ⚠️ 2026-11-17：锚点随「熔断键统一为 _host_key / _bare_host」同步更新。
     # 原锚写的是 `host=host`（**裸主机名**时代），而 `cj()` 现在把 `host`
     # 改成 `_host_key(url)` 后，兜底必须传 `_bare_host`（见该处长注释）
     # —— 旧锚在源码里已经找不到，回归会报「锚点未匹配」（假漏网）。
     # 注入语义不变：把兜底调用换成 `return None`。
     "if limit and host and (_bare_host in PERMANENTLY_DEAD or _host_dead(host)):\n        return _fallback_get(url, ref=ref, host=_bare_host, timeout=timeout)",
     "if limit and host and (_bare_host in PERMANENTLY_DEAD or _host_dead(host)):\n        return None  # 注入：兜底链断",
     "cj() 的 _host_dead 早退分支兜底调用被切断",
     "兜底链断"),

    # --- 代理冷却闸门被删（[网络降级链] 应抓）---
    # 2026-10-14 九维度审查 P1：firecrawl 有 900s 落盘冷却而代理零记忆 ——
    # 黑洞型代理故障每轮重付 20s×N（慢轮吞槽位放大器）。修法是 proxy_get
    # 顶部加冷却闸门。变异体删掉闸门 → 回归"每轮重付"，检查项应抓。
    ("proxy-cooldown-gate-removed",
     "marketlib.py",
     '    if time.time() < _proxy_fail_get(_hk):\n        _proxy_fail_bump(_hk)\n        return None',
     '    if False:\n        return None  # 注入：代理冷却闸门失效',
     "[网络降级链] 代理失败冷却被删 → 黑洞代理每轮重付 20s",
     "顶层 _proxy_fail_get() 无任何引用"),

    # --- Me 类从剥除清单漏掉（[安全闸门] 应抓）---
    # 2026-10-14 实测 13 个 Me 码位全绕。变异体把 Me 从类别清单删掉 →
    # U+0488 拆字用例放行 → BLOCK 用例红。
    # ⚠️ 2026-10-16 锚点修复：今天的 Cs（代理码点）修复在 `"Sm",` 后
    # 追加了 `"Cs"` 与注释行，锚点尾部的 `)` 不再存在 —— 变异体从"抓住"
    # 静默退化成"锚点未匹配、跳过"（正是台面上看不见的那种退化）。
    # 锚点收在 `"Pd", "Sm",`（唯一前缀，不含尾括号）以对后续清单增删免疫。
    ("mask-me-category-removed",
     "marketlib.py",
     '"Cf", "Zs", "Zl", "Zp", "Mn", "Mc", "Cc", "Me",\n                      "Sk", "Lm", "Pd", "Sm",',
     '"Cf", "Zs", "Zl", "Zp", "Mn", "Mc", "Cc",  # 注入：Me/Sk/Lm/Pd/Sm 全漏掉\n                      ',
     "[安全闸门] Me/Sk/Lm/Pd/Sm 类拆字绕过（恒生科技҈ETF联接、清ˊ仓、华夏×全球科技放行）",
     "❌ 拦截[基金名(U+0488 组合用西里尔百万号/Me)]"),

    # --- 新鲜度 _ts 丢失（[新鲜度字段透传] 应抓）---
    ("ts-drop",
     "marketlib.py",
     '"_ts": row.get("_ts", "")}',
     '"_ts_x": row.get("_ts", "")}',
     "_hk_quotes_via_tencent 构造行时 _ts 键名被改 → 新鲜度闸门失效"),

    # --- 磁盘卫生轮转 glob 错（[磁盘卫生] 应抓）---
    ("hygiene-glob-wrong",
     "selfcheck.py",
     "", "",
     "磁盘卫生轮转扫错目录 → 零覆盖",
     "磁盘实际有"),

    # --- 限流 cap 归零（[限流] 应抓）---
    ("ratelimit-cap-zero",
     "marketlib.py",
     "def __init__(self, min_interval=0.15, daily_cap=2000):",
     "def __init__(self, min_interval=0.15, daily_cap=0):",
     "daily_cap=0 → 配额闸门失效"),

    # --- 桩名单漏符号（[桩泄漏] 应抓）---
    ("stub-list-drop",
     "selfcheck.py",
     '"PENDING", "DCACHE_FILE", "DEAD_HOST_FILE",',
     '"DCACHE_FILE", "DEAD_HOST_FILE",',
     "从 _snap_stubs 名单删 PENDING"),

    # --- grep 定位指引噪音（[检查项可失败性] 段守卫应抓）---
    ("grep-noise",
     "selfcheck.py",
     'f"ok_all=False、要么能计入 _unverified 计数）")',
     'f"ok_all=False、要么能计入未验证）")',
     "成功消息含「未验证」→ grep 指引失效"),

    # --- 检查项不可失败（[检查项可失败性] 应抓）---
    # ⚠️ 原先这里有个 `section-unfailable`，定义与实际注入严重脱节
    # （MUTANTS 里写的是插一行 `_unused_rl=0`，apply_mutant 里却是把整段
    # 替换成"只打数"），且语义与下方 `meta-guard-unfailable-section` 重复。
    # 已删除，保留下面那个定义清晰、锚点可核对的。

    # ============ 第十二轮扩充：覆盖行为测试类检查项 ============

    # --- 备用源静默失败（[备用源失败可见性] 应抓）---
    # 2026-10-15 复核后 from_baidu 风控分支改走 _daily_log_once（P2-2
    # 按日去重防 11 行刷屏），锚点随之更新；[备用源失败可见性] 同步把
    # 去重文件指向临时路径，注入后三例仍逐次探到「零日志」。
    ("baidu-silent-fail",
     "feeds.py",
     '''M._daily_log_once("baidu", "risk",
                                  f"百度风控拦截（{R.get('msg') or R.get('code')}）"
                                  f"—— 该源本轮不可用，降级到下一顺位（本日首次）")
                continue''',
     "continue  # 注入：风控不再留日志（静默失败）",
     "from_baidu 风控拦截时不再记日志 → 静默失败"),

    # --- 事件键映射缺失（[事件键一致性] 应抓）---
    ("ev-alias-drop",
     "webui.py",
     'return "watch_" + k[5:]      # fund_90.BK1036 -> watch_90.BK1036',
     'return k      # 注入：fund_* 不再映射成 watch_*',
     "_ev_alias 不再把 fund_* 映射成 watch_* → 快照事件面板不显示"),

    # --- 标题规范化失效（[输出格式规范化] 应抓）---
    ("norm-title-noop",
     "analyze.py",
     "def _norm_title(t, slot):",
     "def _norm_title(t, slot):\n    return t   # 注入：规范化失效",
     "_norm_title 直接返回原文 → 时段名不再纠正"),

    # --- 推送形态下限（[推送形态下限] 应抓）---
    # ⚠️ 2026-10-08 审查：`[推送形态下限]` 曾是 64 段中**唯一零变异体覆盖**
    # 的段 —— 它的三条断言（纯标点被拦 / 可降级文本仍降级 / _norm_title
    # 跳过前置空行）此前没有任何变异体指向。下面两条各打一个断言。
    # 形态：摘掉降级链的"实义字符"闸门 → '账户。；' 按句剔除后剩 '；'，
    # _safe 放行 → 群里收到一条只有一个分号的消息（真实历史缺陷，2026-09-27）。
    ("push-punct-allow",
     "analyze.py",
     "            if t2 and t2 != t and _has_core:",
     "            if t2 and t2 != t:  # 注入：实义字符闸门被摘",
     "降级链不再要求实义字符 → 纯标点碎片被放行上线"),

    # 形态：_norm_title 不再跳过前置空行 → lines[0] 是空串、_TITLE_RE
    # 不匹配 → 整个函数静默失效（AI 自造的时段名原样上线）。
    ("norm-title-lead-blank",
     "analyze.py",
     "    _i = 0\n    while _i < len(lines) and not lines[_i].strip():\n        _i += 1\n    if _i >= len(lines):\n        return t\n    m = _TITLE_RE.match(lines[_i])",
     "    _i = 0  # 注入：不再跳过前置空行\n    if _i >= len(lines):\n        return t\n    m = _TITLE_RE.match(lines[_i])",
     "_norm_title 不跳过前置空行 → 带前导换行的标题静默不纠正"),

    # --- umask 644 回归：创建函数退回裸 open()（[状态文件权限] 应抓）---
    # 真缺陷形态：`_mk600` 退回 `open(path, "w")` —— umask 022 下建出 644。
    # 判据 ② 的探针真调这个函数并 stat 产物的 mode，所以抓得到；
    # 只扫现存文件的判据抓不到（旧文件已经是 600 了）。
    ("mk600-back-to-open",
     "runlog.py",
     '    with os.fdopen(_mk600(path), "w", encoding="utf-8") as f:\n'
     '        f.write(text)',
     '    with open(path, "w", encoding="utf-8") as f:  # 注入：退回 umask\n'
     '        f.write(text)',
     "runlog._write600 退回裸 open → umask 022 下建出 644"),

    # 真缺陷形态：marketlib 的锁文件创建退回 `open(p, "a")`。
    ("openlockfile-back-to-open",
     "marketlib.py",
     '    return os.fdopen(\n'
     '        os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600),\n'
     '        "a", encoding="utf-8")',
     '    return open(p, "a", encoding="utf-8")  # 注入：退回 umask',
     "marketlib._open_lockfile 退回裸 open → 锁文件 644"),

    # 真缺陷形态：自检自己的 flock 闸门退回 `open(..., "w")`
    # —— 这条正是新判据**当场抓到的真实缺陷**（生产 selfcheck.lock 曾是 644）。
    ("selfcheck-lock-back-to-open",
     "selfcheck.py",
     '        _lockf = os.fdopen(\n'
     '            os.open(p_log("selfcheck.lock"),\n'
     '                    os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600), "w+")',
     '        _lockf = open(p_log("selfcheck.lock"), "w")'
     '  # 注入：退回 umask',
     "自检 flock 闸门退回裸 open → selfcheck.lock 644"),

    # --- 失败记忆缓存：过期条目不清理 / 读键与写键不对应（[失败记忆缓存] 应抓）---
    # 真缺陷形态一：写入时不再剔除过期条目 → cache/none_ts.json 永久留着
    # 陈旧时间戳（实测 2026-10-08 留着 15.5 天前两条），而这个文件的语义是
    # "只活 30 分钟"、正常早该为空 —— 陈旧条目让"写入路径还在不在跑"
    # 无法从文件本身判断。
    ("none-ts-no-prune",
     "marketlib.py",
     # ⚠️ 锚点必须跟着被测代码走（2026-10-11）：`_levels_stamp_none` 改走
     # `_locked_update`（加锁防 lost-update）后，原地写法整体变了 ——
     # 旧锚点直接失配、`apply_mutant` 返回 False，回归输出成「跳过」，
     # 看起来像通过而实际**零判别力**（同
     # [[market-watch-mutant-fixture-own-trigger]]）。
     # 新锚点落在 mutate 闭包的**函数体**上：把「剔除过期」整段删掉，
     # 只留写入 —— 语义与旧变异体完全相同（写入不再清理过期条目）。
     '        def _mu(d):\n'
     '            _now = time.time()\n'
     '            for _k in [k for k, v in list(d.items())\n'
     '                       if not isinstance(v, (int, float))\n'
     '                       or (_now - v) > LEVELS_NONE_TTL]:\n'
     '                d.pop(_k, None)\n'
     '            d[f"levels:{secid}"] = _now',
     '        def _mu(d):\n'
     '            d[f"levels:{secid}"] = time.time()  # 注入：不再剔除过期条目',
     "写入不再清理过期条目 → [失败记忆缓存] 应报陈旧条目残留"),

    # 真缺陷形态二：alert 侧写 `alert:<id>` 而读裸 id → 恒判"已过期" →
    # 每轮都重试，"失败记忆"这个功能**静默失效**（不报错、无任何信号）。
    ("none-ts-key-mismatch",
     "alert.py",
     '        ts = d.get(f"alert:{secid}", 0)',
     '        ts = d.get(secid, 0)  # 注入：读键与写键不对应',
     "alert 读键与写键不对应 → [失败记忆缓存] 应报记忆读不回来"),

    # --- launchd 日志路径与 harden_logs 名单漂移（[定时任务] 应抓）---
    # 真缺陷形态：plist 的 StandardErrorPath 指向 webui.err，而脚本入口只
    # 收紧 webui.out —— launchd 按 umask 022 建的 webui.err 一直是 644
    # （同机其他本地用户可读），且没有任何地方会报出来。
    # 判据是**交叉对账**（plist 的路径 vs 脚本 harden_logs 的 AST 实参），
    # 两个方向各一个变异体：漏列 与 多列。
    ("harden-log-underlist",
     "webui.py",
     '    _harden_logs("webui.out", "webui.err")',
     '    _harden_logs("webui.out")  # 注入：err 不再收紧',
     "webui.err 未列进 harden_logs → [定时任务] 应报交叉不一致"),

    ("harden-log-overlist",
     "webui.py",
     '    _harden_logs("webui.out", "webui.err")',
     '    _harden_logs("webui.out", "webui.err", "webui.typo")'
     '  # 注入：多列一个 plist 不写的名字',
     "harden_logs 多列一个 plist 不写的名字 → [定时任务] 应报"),

    # --- 金额闸门把裸逗号当千分位（[安全闸门] 应抓）---
    # 真缺陷形态：`AMT_RE` 的 w/k 缩写分支退回 `[\d,]+` —— 该字符类允许
    # 逗号**单独**成立，于是英文正文里的 `, while` 被匹配成金额 `, w`
    # （`, ` 当千分位、`w` 当"万"）。`[实测]` 2026-10-08 14:03 生产轮：
    #   watch.log `重写：命中禁用词 ', w'` → 触发无谓重写 → 那次重写又撞上
    #   CLI 符号链接瞬断 → 整条推送退化成兜底模板，前一次 384s 的正文白丢。
    # 判据是 [安全闸门] 的 ALLOW 用例（英文逗号必须放行），取自生产原文。
    ('amtre-bare-comma',
     'marketlib.py',
     '    r"|(?:\\d+(?:\\.\\d+)?[wWkK])(?![功率瓦条件台个只名次股秒时])"',
     '    r"|(?:[\\d,]+[wWkK])"  # 注入：退回裸逗号千分位且去掉词边界',
     '金额闸门把裸逗号当千分位 → 英文 `, while` 被误判成金额',
     '❌ 放行: 沪深300指数走弱'),

    # --- 宏观语境词被删（[安全闸门] 应抓，2026-11-05）---
    # 生产实证：调研播报里「美国 10 月非农（预期 6.3 万、前值 2.9 万）」
    # 整句被 `_safe_summary` 剔掉（宏观数据被当个人金额）→ 群里少了
    # 明日最重要的经济事件。`_AMT_CTX` 补了非农/前值等宏观口径词。
    ('amt-ctx-macro-dropped',
     'marketlib.py',
     '    r"|非农|新增就业|就业人数|失业率|CPI|PPI|社融|赤字|财政|人口|前值|公布值|目标价|EPS|每股收益|每股|股息|股息率|市盈率|市净率|北向|沪股通|深股通|南向|港资|两融|锂价|铜价|铝价|铁矿石|螺纹钢|动力煤|焦炭|原油|商品价|期货|现货|报盘)")',
     '    r")")  # 注入：去掉市场语境词',
     '金额闸门缺宏观语境词 → 非农/前值这类公开统计量被当个人金额拦掉'
     '（2026-11-05 生产：调研播报少了「明日 10 月非农」这句）',
     '❌ 放行: 11/6 21:30 美国 10 月非农'),

    # --- 报价语境词被删（[安全闸门] 应抓，2026-11-06）---
    # 「中际旭创 -9% 失守 500 元」是公开行情；语境词没了就会被当个人金额，
    # 整句从播报里消失（2026-11-03/04 plan 草稿里真实出现过这种写法）。
    ('amt-quote-ctx-dropped',
     'marketlib.py',
     '    r"(?:失守|站上|涨至|跌至|回落至|报收|收报|报于|收于|现报|股价|收盘价)")',
     '    r"(?:__never__)")  # 注入：去掉报价语境',
     '金额闸门缺报价语境词 → 个股/指数价位（「失守 500 元」）被当个人金额'
     '剔除，播报里看不到关键价位',
     '❌ 放行: 中际旭创 -9% 失守 500 元'),

    # --- AI 推理块不再剥除（[AI 通道] 应抓）---
    # 真缺陷形态：`_strip_think` 直接 return t → ` thinking…</think>` 整块
    # 思维链进 `_safe()` 与结构下限校验：轻则无谓重写，重则结构标签数不够
    # 而退化成模板，而真正的正文就在推理块后面。
    # `[实测]` 2026-10-08：用生产同款调用（`claude -p <prompt>`，
    # stdin=DEVNULL）复现，stdout 以 `' thinking'` 开头。
    ("think-strip-off",
     "analyze.py",
     '    t = _THINK_RE.sub("", t)\n'
     '    t = _THINK_OPEN_RE.sub("", t)\n'
     '    return t.strip()',
     '    return t  # 注入：不再剥推理块',
     "推理块不再剥除 → [AI 通道] 应报推理块处理错误",
     "推理块处理错误"),

    # --- DeepSeek 伪工具调用标记（DSML）不再作废（2026-10-26 加）---
    # `[实测]` 生产 11:34 那轮：AI 想调 Bash 数中文字数，工具不可用时把
    # **调用语法本身当正文吐出来**，尾部带整段 `EOF` / `python3 - <<'PY'`
    # shell 脚本。开头标签被 `_strip_preamble` 当元叙述剥掉、**尾部残留
    # 全留下** —— 那次侥幸被结构闸门判「缺关注板块」触发重写才没进群；
    # **正文段齐全时脚本会直接推给读者**。
    # `[统计]` 生产会话里共 7 次（10-08 / 10-15×2 / 10-18×2 / 10-26×2）。
    # prompt 里禁止工具调用语法（2026-10-28 加）。
    ("prompt-dsml-ban-dropped",
     "analyze.py",
     '   ⚠️ **你没有可用工具，也绝不要输出任何工具调用语法**（`<｜｜DSML｜｜…>`、\n'
     '   `<function_calls>`、`<invoke>`、shell 脚本、`python3 - <<PY` 之类）。',
     '   需要数字就按上面给的现成数据写，不要试图计算或校验。',
     "[AI 通道] prompt 不禁止工具调用语法 → 模型按训练习惯把 DSML/"
     "shell 脚本当正文吐，整轮白跑一次重写（历史 8 次，最近 2026-10-28 09:48）",
     "prompt 里没有禁止输出工具调用语法"),
    ("dsml-not-voided",
     "analyze.py",
     '        if _has_dsml(_out):\n'
     '            _log("AI 输出含 DeepSeek 伪工具调用标记（DSML）—— 整块作废，"\n'
     '                 "走结构重写；不尝试抠正文（尾部会残留 shell 脚本）")\n'
     '            _out = "（AI 输出含伪工具调用标记，已作废）"',
     '        pass  # 注入：DSML 输出不再作废',
     "[AI 通道] DSML 伪工具调用标记必须整块作废"
     "（尾部残留的 shell 脚本会直接进推送）",
     # ⚠️ must_contain 必须与自检**实际报出的首条 ❌** 逐字对齐（2026-10-26 实测）：
     # 注入后 `_has_dsml` 从 `ai_analyze` 里消失，先触发的是**接线检查**
     # （"ai_analyze 没调用 _has_dsml"），而不是"195 条误判"那条。
     # 原先写 "DSML 判据" —— 判据确实抓住了（rc=1、❌=2），却因文案对不上
     # 被记成漏网。同 [[market-watch-mutation-anchor-drift]] 的 must_contain 一类。
     "没调用 _has_dsml"),

    # --- DSML 作废时置成空串（会误触发 AI 熔断）---
    # 空串会命中 `ai_analyze` 的「`not out` → 通道不可用 → 熔断 10 分钟」
    # 分支，把**内容格式问题**误判成**通道故障**，所有推送退兜底模板 10 分钟。
    # DSML 恰恰说明通道是通的（模型把工具语法写进了正文）。
    # 正确形态是给一个必然过不了结构闸门的短占位串。
    ("dsml-void-to-empty-string",
     "analyze.py",
     '            _out = "（AI 输出含伪工具调用标记，已作废）"',
     '            _out = ""',
     "[AI 通道] DSML 作废不得置空串"
     "（空串会触发「通道不可用→熔断 10 分钟」，把内容问题误判成通道故障）",
     "DSML 作废置成了"),

    # --- DSML 判据写错（正则不匹配全角竖线）---
    # 变异体把 U+FF5C（全角竖线）换成半角 `|` —— 判据恒假，
    # 等于这条防线不存在。
    ("dsml-regex-halfwidth",
     "analyze.py",
     '_DSML_RE = re.compile("[｜]{2}\\\\s*DSML\\\\s*[｜]{2}")',
     '_DSML_RE = re.compile(r"\\|\\|\\s*DSML\\s*\\|\\|")',
     "[AI 通道] DSML 判据必须匹配**全角**竖线"
     "（半角正则对真实输出恒不命中，等于这条防线不存在）",
     "生产原文形态的 DSML 标记未被识别"),

    # --- 面板时段归因（[面板时段归因] 应抓）---
    # ⚠️ 不用"改 rmap 文案"作变异 —— 那**不是缺陷**（改文案无害），
    # 混进来只会虚增"漏网"数。此处用真缺陷：把事件类归因到错误的槽位。

    # --- JSON 形状契约：502 页当成功（[JSON 形状契约] 应抓）---
    ("json-shape-502",
     "marketlib.py",
     "def cj(url, ref=REF_EM, retries=3, timeout=20, limit=True):",
     "def cj(url, ref=REF_EM, retries=3, timeout=20, limit=True):\n    return {'data': {'diff': []}}  # 注入：恒定返回空形状",
     "cj 恒返回合法空形状 → 掩盖真实失败"),

    # --- 安全闸门放行禁用词（[安全闸门] 应抓）---
    ("gate-banned-allow",
     "marketlib.py",
     "def safe(t):",
     "def safe(t):\n    return (True, None)  # 注入：闸门恒放行",
     "safe() 恒放行 → 禁用词不再拦截",
     "❌ 拦截[人称(你)]"),

    # --- 桩名单覆盖面：_stub() 助手形态的桩漏进名单（[桩泄漏] 应抓）---
    # 真缺陷形态：`_isolate_run_deps` 用 `_stub(模块, "名字", 值)` 桩了
    # `hot.analyze_cached`，而扫描器原先只认 `X.attr = ...` 赋值形态 ——
    # 这个符号从未进过 `_SNAP0` 名单，漏还原也查不出来。
    # 变异体把名单里那一条删掉，验证扫描器**真的**能报出来。
    # ⚠️ 反向验证过（2026-10-11）：第一版扫描器分支写在 for 循环**之外**，
    # 只执行一次、`_n` 是循环残留值 → 恒不命中 → 删了名单也报 ✅。
    ("stub-list-hot-missing",
     "selfcheck.py",
     '                (_h, "hot", ("analyze_cached", "analyze", "HOT_CACHE")),\n',
     '',
     "[桩泄漏] 应报「hot.analyze_cached 不在 _snap_stubs 名单里」"),

    # --- 安全闸门：Po 类词内分隔号绕过（[安全闸门] 应抓）---
    # 真缺陷形态：`_MASK_CHARS_EXTRA` 只手工列了 `·`/`•` 两个，而 Po 类里
    # 同语义的还有一批（U+30FB/U+FF65/U+2027/U+FE52/U+FF0E）。在基金名
    # 中间插一个就能让词表匹配断裂，而正则侧不覆盖基金名 —— 闸门形同虚设。
    # 实测（2026-10-11 审查）：`safe('减仓华夏・全球科技')` -> 放行。
    ("gate-po-dot-bypass",
     "marketlib.py",
     # ⚠️ 第一版写反了：new 里写成 `and ch not in "..."` —— 那是**继续剥**
     # 而不是**不再剥**，变异体实际只多剥了几个字符，行为没变，
     # 于是自检"漏网"报得毫无意义（`[实测]` rc=0、❌数=0）。
     # 正确形态是把 `_MASK_CHARS_EXTRA` 换成**空串** —— 剥除名单空了，
     # Po 分隔号与 `·`/`•` 全部不再被剥，拆字绕过复现。
     '_MASK_CHARS_EXTRA = ("\\u00b7\\u2022"        # \\u00b7 \\u2022 \\u539f\\u6709\\u7684\\u4e24\\u4e2a\\u8bcd\\u5185\\u5206\\u9694\\u53f7',
     '_MASK_CHARS_EXTRA = (""  # 注入：词内分隔号名单被清空',
     "Po 类词内分隔号绕过 → 基金名被拆字后闸门放行",
     "❌ 拦截[基金名(间隔号拆断)]"),

    # --- 安全闸门：PLAN_RE 只认「周」（[安全闸门] 应抓）---
    # 真缺陷形态：模式 `第\s*[0-9一二三]\s*周` 只覆盖「周」，而 prompt 规则 4
    # 禁的是「第N周**这类**计划内部编号」—— 「第2天」「第 3 阶段」「第4项」
    # 「第2轮」全部漏网，暴露的是同一件事（发送者有一份带编号的内部计划）。
    # ⚠️ 锚点 2026-10-26 随「日/天」拆出而更新：`PLAN_RE` 的单位枚举已
    # 去掉「天|日」（改由 `PLAN_DAY_RE` 单判，见 marketlib.py）。
    # 本变异体仍测同一件事（单位枚举收窄 → 「第N阶段」类漏网）。
    ("gate-plan-num-narrow",
     "marketlib.py",
     'PLAN_RE = re.compile(\n'
     '    r"第\\s*[0-9０-９一二三四五六七八九十]{1,3}\\s*"\n'
     '    r"(?:周|阶段|项|步|轮|批|次|期|回合|部分|章节)")',
     'PLAN_RE = re.compile(r"第\\s*[0-9一二三]\\s*周")  # 注入：只认「周」',
     "PLAN_RE 只认「周」→ 「第3阶段」等计划编号全漏网",
     "❌ 拦截[计划编号(第4项)]"),

    # --- 「第N日/天」的判据被拓宽成无条件命中（[安全闸门] ALLOW 应抓）---
    # 2026-10-26 加：修「第四日」假阳性时，把中文数字形态改成了
    # 「同子句有计划语境词才拦」。若这条守卫被退回无条件命中，市场语言
    # 「恒指连续第三日收在均线上」会被误拦 → 每次白付一轮 AI 重写
    # （生产实据 `[10-22 09:19:12] 重写：命中禁用词 '第四日'`）。
    ("gate-plan-day-unconditional",
     "marketlib.py",
     'PLAN_DAY_RE = re.compile(\n'
     '    r"第\\s*[0-9０-９]{1,3}\\s*[日天]"\n'
     '    r"|" + PLAN_DAY_CTX + r"[^，。；！？]{0,12}第\\s*[一二三四五六七八九十]{1,3}\\s*[日天]"\n'
     '    r"|第\\s*[一二三四五六七八九十]{1,3}\\s*[日天][^，。；！？]{0,12}" + PLAN_DAY_CTX)',
     'PLAN_DAY_RE = re.compile(   # 注入：退回无条件命中\n'
     '    r"第\\s*[0-9０-９一二三四五六七八九十]{1,3}\\s*[日天]")',
     "[安全闸门] 「第N日」的中文数字形态必须要求计划语境"
     "（无条件命中会把「连续第三日收在均线上」误拦）",
     "误拦 第四日"),

    # --- `a/b` 的数值过滤被摘（[安全闸门] ALLOW 应抓日期误拦）---
    # ⚠️ 2026-10-26：与上面的 `gate-fraction-date-falsepos` 是**同一处注入**，
    # 故只保留上面那条（名字沿用历史，避免基线里多出一个等价重复项）。

    # --- 元说明块：自称词表漏词（[AI 通道] 应抓）---
    # 真缺陷形态：`_META_MARKS` 只有 4 个词，而 prompt 规则 1 明列
    # 「写作说明」「字数统计」「会话环境」等 —— 漏掉的那两个照样是
    # 元叙述，照样会整段进群。
    ("ai-meta-marks-narrow",
     "analyze.py",
     '_META_MARKS = ("写作说明", "写作备注", "不属于推送正文", "本条说明",\n'
     '               "字数统计", "会话环境")',
     '_META_MARKS = ("元说明块", "写作备注", "不属于推送正文", "本条说明")',
     "_META_MARKS 漏「字数统计」「会话环境」→ 这两个词的元块不截",
     "元说明块处理错误"),

    # --- 元说明块：多段分隔线只截最后一段（[AI 通道] 应抓）---
    # 真缺陷形态：从**最后**一个分隔线行截断，于是「正文 + --- + 元块A +
    # --- + 元块B」会留下元块A —— 两个都是元叙述，都该剥掉。
    ("ai-meta-multi-sep",
     "analyze.py",
     '    # ① 从**第一个**分隔线行截断（其后全部视为附加块）\n'
     '    for _i, _ln in enumerate(lines):',
     '    # ① 注入：从最后一个分隔线行截断\n'
     '    for _i in range(len(lines) - 1, -1, -1):\n'
     '        _ln = lines[_i]',
     "多段分隔线时较早的元块残留 → 元叙述仍进群"),

    # --- 序列化回归：[序列化回归] 应抓 ---
    # ⚠️ 不用"给 _atomic_write_json 加 dumps 校验"作变异 —— 那是**增强**
    # 不是缺陷，混进来只会虚增"漏网"数。此检查项守的是 pending.json
    # 可序列化，真缺陷是"写入不可序列化对象"，但那需要改数据流，
    # 用 emit 侧更合适（见下方 collect 相关）。

    # --- 静态未绑定调用：引入一个未定义函数调用（[静态未绑定调用] 应抓）---
    ("unbound-call",
     "alert.py",
     "def should_alert(now, state, news_fn=None, quotes_fn=None):",
     "def should_alert(now, state, news_fn=None, quotes_fn=None):\n    _undefined_helper_xyz()  # 注入",
     "alert 引入未定义函数调用"),

    # --- 跨模块调用参数个数：多传一个位置参数（[跨模块调用参数个数] 应抓）---
    ("arity-mismatch",
     "alert.py",
     'M.threshold("fund_abs", 50)',
     'M.threshold("fund_abs", 50, 99)',
     "alert 调用 M.threshold 多传一个位置参数 → 运行期 TypeError"),

    # --- 节假日表为空（[节假日表] 应抓）---
    # ⚠️ 不能用 `HOLIDAYS = ` 字面替换成 `()` —— 那会在原 dict 内容前
    # 留下残体，制造**语法错误**（测的是"模块导入失败"而非"空表"）。
    # 见下方 apply_mutant 的特殊处理：整块 dict 替换成 `{}`。
    ("holidays-empty",
     "marketlib.py",
     "\nHOLIDAYS = {",
     "\nHOLIDAYS = {}",   # 占位，实际由 apply_mutant 特殊处理
     # ⚠⚠ 2026-11-13（复核，如实登记）：本条与 `trading-day-always-true`
     # **症状完全相同**，文本层面无法区分。
     # `[实测]` 两条注入后报出的是**同一批** ❌，一字不差：
     #   ❌ 公告休市日 2026-10-05（国庆假期）被判成**交易日** —— HOLIDAYS 漏填了它…
     #   ❌ 公告休市日 2026-10-07（国庆末日）被判成**交易日** —— …
     #   ❌ 公告休市日 2026-10-06（国庆假期）被判成**交易日** —— …
     # 交叉验证：把任一条的 must_contain 拿去套另一条的输出，照样命中。
     # 原因：`HOLIDAYS` 清空与 `is_trading_day` 恒真，对**调用方**而言
     # 是同一个结果 —— 公告休市日被当成交易日。检查项只看结果，
     # 无从判断是列表空了还是判据坏了。
     # 所以这 2 条**合起来只算 1 条判别力**，不要当成 2 条覆盖来读。
     # 真要拆开，得让 `[节假日表]` 段额外区分「表空」与「表非空但判据坏」，
     # 那是检查项的改造，不是匹配串能解决的 —— 留作已知限制。
     "HOLIDAYS 清空 → 所有休市日被当交易日"),

    # ============ 第十三轮扩充：复杂行为测试项 ============

    # --- 快照静默回退（[快照复用] 应抓）---
    # 这项的判据是"注入新鲜快照 → 断言复用**真的返回了数据**"，
    # 最危险的失效形态正是"函数存在、���调用、但返回 None"（静默回退自取）。
    ("snapshot-silent-fallback",
     "analyze.py",
     '''        d = M._load_json(M.PENDING, None)
        if not isinstance(d, dict):
            return None
        if d.get("date") != _pnow(now).date().isoformat():
            return None''',
     '''        d = M._load_json(M.PENDING, None)
        if not isinstance(d, dict):
            return None
        return None   # 注入：静默回退自取''',
     "_quotes_from_snapshot 恒返回 None → 快照复用静默失效",
     "返回空 —— 会静默回退到自取"),

    # --- urgent 分支缺 return（一轮双推，[紧急推送配额] 应抓）---
    ("urgent-no-return",
     "analyze.py",
     # ⚠⚠ 2026-11-07：锚点原本是 `_r.setdefault("slot", None)` 那三行。
     # 后来在它们**前面**加了一个无条件 `return _r`（“无论成败都返回”），
     # 于是那三行变成**死代码**，而变异体删的就是死代码里的 return
     # —— 注入了也不产生行为差异，成为**等价变异体**，
     # 全量回归里一直报“漏网”。已删掍死代码，锚点改到活的那个 return
     # （即紧跟在 F13 耗时日志之后）。
     '''        # 无论成败都返回：失败时同样不该再走通用分支（那会推一条空的
        # "事件类"消息，并在 `hits=[]` 下把配额扣掉）。
        return _r''',
     '''        # 无论成败都返回：失败时同样不该再走通用分支（那会推一条空的
        # "事件类"消息，并在 `hits=[]` 下把配额扣掉）。
        # 注入：return 被删 → 落到通用推送分支（历史真实缺陷）''',
     "urgent 分支缺 return → 一轮推两条并重复扣配额"),

    # --- 快照跨天不复用（[快照复用] 应抓）---
    ("snapshot-crossday",
     "analyze.py",
     '''        if d.get("date") != _pnow(now).date().isoformat():
            return None''',
     '''        if False:
            return None''',
     "_quotes_from_snapshot 不再拒绝跨天快照"),

    # --- storage 完整性闸门失效（[快照复用] 应抓）---
    ("storage-gate-off",
     "analyze.py",
     "def _storage_from_snapshot(max_age_s=360, now=None):",
     "def _storage_from_snapshot(max_age_s=360, now=None):\n    return None   # 注入：分时完整性闸门失效",
     "_storage_from_snapshot 恒返回 None → 快照分时段缺失"),

    # --- 日志去重失效（[跨进程日志去重] 应抓）---
    # 判据是行为化的："真的调三次，数日志行数增量（应为 1）"。
    # 真缺陷：去掉去重、每次都记（历史真实缺陷，日志噪音刷掉诊断）。
    ("log-dedup-off",
     "marketlib.py",
     '''def _perm_dead_log_once(host):
    """永久封锁主机的跳过日志，**每天只报一次**（跨进程）。"""
    _daily_log_once(
        host, "permdead",
        f"跳过已永久封锁的主机（直连与兜底均不尝试）: {host}"
        f" —— 站点若已解封，请从 marketlib.PERMANENTLY_DEAD 移除")''',
     '''def _perm_dead_log_once(host):
    """注入：去重失效，每次都记。"""
    log(f"跳过已永久封锁的主机: {host}")''',
     "_perm_dead_log_once 去重失效 → 每轮打一行，日志噪音"),

    # ============ 第十五轮：覆盖剩余 5 项 ============

    # --- 采集新鲜度阈值放大（[采集新鲜度] 应抓）---
    # 判据：盘中 age > FRESH_LIMIT_IN_SESSION 才算停摆。把阈值放大到
    # 极大 → 真停摆永远不报。自检从 collect.FRESH_LIMIT_IN_SESSION 读它。
    ("fresh-limit-huge",
     "collect.py",
     "FRESH_LIMIT_IN_SESSION = SCHED_INTERVAL * 3",
     "FRESH_LIMIT_IN_SESSION = SCHED_INTERVAL * 99999  # 注入：停摆不报",
     "新鲜度阈值放大 → 盘中停摆永远不报"),

    # --- 交易日判定恒真（[采集新鲜度]/[节假日表] 应抓）---
    # is_trading_day 是"今天该不该采集"的唯一判据；恒真则休市日也当交易日。
    ("trading-day-always-true",
     "marketlib.py",
     "return d.weekday() < 5 and d.isoformat() not in HOLIDAYS",
     "return True  # 注入：恒为交易日",
     # ⚠⚠ 2026-11-13（复核，如实登记）：与 `holidays-empty` **症状相同**，
     # 报出的是一字不差的同一批 ❌，两条的 must_contain 互相都能命中。
     # 合起来只算 1 条判别力 —— 详见 `holidays-empty` 处的详细说明。
     "is_trading_day 恒真 → 休市日被当交易日"),

    # --- 调度方式：plist 改成失效的 StartCalendarInterval（[调度方式] 应抓）---
    # 这是历史真实故障：StartCalendarInterval 事件从不投递。
    # 自检读 plist 应报出"用了 calendar"。
    ("sched-calendar-dead",
     "__PLIST_MARKETWATCH__",
     "<key>StartInterval</key>",
     "<key>StartCalendarInterval</key>",
     "marketwatch plist 从 StartInterval 改成 StartCalendarInterval（历史失效调度）"),

    # --- 调度档位退回 Background（[调度方式] 应抓）---
    # 2026-10-21 加：这是**全天卡顿的根因**，而且它藏得极深 —— plist 里
    # 一个词，不改任何代码，表现却是"程序莫名其妙跑不完"。
    # 同一段 20 万次循环的纯 CPU 代码：默认档位 **0.33 秒**，
    # `taskpolicy -b`（等价 Background）**60 秒跑不完**（180 倍以上）。
    # 当天 `analyze` 跑了 5 分 28 秒只累计 **0.17 秒** CPU，进程 R 却零推进。
    #
    # ⚠️ 锚点用 `<key>Label</key>` 注入而不是替换已有字符串：生产 plist
    # 现在**完全没有** ProcessType 键（判据是"不得出现该键"），
    # 所以变异方向是**加回去**，不是改值。
    # `Adaptive` 也测（实测 pri=4，同样被降级）—— 两个取值都该报。
    ("sched-processtype-background",
     "__PLIST_MARKETWATCH__",
     "<key>Label</key>",
     "<key>ProcessType</key><string>Background</string><key>Label</key>",
     "[调度方式] plist 不得含任何 ProcessType"
     "（Background 实测慢 180 倍、Adaptive 也只有 pri=4；"
     "系统繁忙时拿不到 CPU，整轮跑不完、槽位整段被吞）",
     "ProcessType"),

    # ============ 第十七轮：元守卫自身的两个盲区 ============

    # --- 元守卫漏收整段（[检查项可失败性] 应抓）---
    # 真实缺陷：`[数据源]` 是 `_main()` 里唯一少写前导 `\n` 的段标题
    # （`print("[数据源]")` 而非 `print("\n[数据源]")`）。元守卫按
    # `startswith("\n[")` 收集段，于是**整个 `[数据源]` 段从未被纳入
    # 可失败性检查**，它的 4 处 ok_all=False/_unverified 落在所有段
    # 区间外，而守卫照样报「✅ 55 个检查项均可失败」。这跟本项目反复
    # 修的根因同类：**判据依赖了被测对象自己可能不遵守的书写约定**。
    # 修复：守卫判据改用 strip()（容忍有无前导换行）+ 加"事实锚"
    # （可失败标记必须落在某个段区间内）。
    # 变异形态：去掉标题的**方括号** —— 让任何"收集段标题"的判据都认不出
    # 它（比只去掉 \n 更狠：修复后的 strip 判据仍会漏），验证事实锚是否报警。
    ("meta-guard-miss-section",
     "selfcheck.py",
     'print("\\n[数据源]")',
     'print("\\n数据源")',   # 占位，实际由 apply_mutant 特殊处理
     "元守卫漏收整段 → 段内可失败标记成孤儿（事实锚应抓）"),

    # --- 段内无可失败标记（[检查项可失败性] 应抓）---
    # 把 `[跨进程冷却]` 段的 ok_all=False / _unverified 计数全部改成 noop，
    # 制造「只打数不下判断」的仪表盘段，验证 _mg_bad 判据仍有效。
    ("meta-guard-unfailable-section",
     "selfcheck.py",
     "__SECTION_NOOP__",   # 占位，实际由 apply_mutant 特殊处理
     "",
     "元守卫：整段改成不可失败（ok_all 全改成 noop）"),

    # ============ 第十八轮：填零覆盖段的坑 ============

    # ⚠️ 试过 `clist-dead-not-flagged`（clist 恒 False → [板块列表]
    # 只打 ⚠️ 不置位、rc 0），**实测漏网**。核实后确认那是**合理的设计
    # 选择**而非缺陷：clist（热点板块）不可用时「热点功能受限但不致命」，
    # 段内注释明确说明不置位，且与「限流」段的可失败判据性质不同。
    # 变异体必须对应**真缺陷**，否则只会虚增漏网数 —— 移除。
    # 给 [板块列表] 找真缺陷形态要在别处（例如 clist 返回**错误形状**
    # 而非 False，让后续消费点崩溃），已留待后续轮次。

    # --- [状态文件] 类型校验（[状态文件] 应抓）---
    # 真缺陷形态：state.json 的 hourly_retry 被写成 list（而非 dict）。
    # analyze.py:1619 `st.setdefault("hourly_retry", {})[hourly] = ts` 在
    # 值为 list 时直接 TypeError，崩在「截止时间抢占」关键路径上（历史
    # 真实缺陷，已在 load_st 加自愈，本段负责**主动**报出来）。
    # 数据注入型变异：改沙箱 state.json 的字段类型，不改源码。
    ("state-hourly-retry-type",
     "__DATA_STATE_JSON__",
     "", "",
     "state.json hourly_retry 类型错(list) → [状态文件] 应报"),

    # --- load_st 归一被短路（[状态文件] 行为判据应抓）---
    # 2026-10-21 加：`[状态文件]` 的静态检查只证明"生产 state 此刻正常"，
    # 新加的行为判据（沙箱造全坏 state → 断言归一后类型归位 + ERR 留痕）
    # 守的是「归一真的在干活」。本变异体把类型闸门改成恒真 —— 坏值原样
    # 通过，行为判据必须报 ❌。
    ("loadst-normalize-noop",
     "analyze.py",
     "            if not isinstance(_v, _want):",
     "            if False:   # 注入：类型归一被短路",
     "[状态文件] load_st 必须真的把一个类型不对的键换成空值"
     "（短路后坏值原样流到下游，崩溃点跑到很远的地方）",
     "load_st 归一失效"),

    # --- AI 通道熔断被短路（[AI 熔断与双败归因] 应抓）---
    # 2026-10-21 加：熔断是"主备双败后别每轮再白等 270s"的唯一开关。
    # 把判据改成永假 = 熔断永不生效 —— 行为上等价于修复前的旧代码
    # （每轮都去撞 150s+120s 的首字节闸门），而**其它一切照常**
    # （推送仍发、模板仍用），线上极难察觉。本变异体守的就是这条。
    ("ai-cooldown-never-trips",
     "analyze.py",
     "    if _ai_cool_until > time.time():",
     "    if False:   # 注入：熔断永不生效",
     "[AI 熔断] 熔断判据必须真的能拦住调用"
     "（短路后每轮主备双败都要白等 270s，正是 2026-10-21 全天 7 轮的原状）",
     "熔断期内仍调用了 AI"),

    # --- 熔断清除路径被删（[AI 熔断与双败归因] 应抓）---
    # 只写不清的熔断会在通道恢复后继续压 10 分钟模板 —— 这是"省时间"
    # 反过来变成"漏 AI 产出"的那一半。成功 return 路径上的 clear 删掉，
    # 接线断言必须抓到"ai_fail_clear 不在 ai_analyze 里"。
    ("ai-cooldown-never-clears",
     "analyze.py",
     "                # 有产出 = 通道活着，清熔断（见 ai_fail_clear 的注释）。\n"
     "                M.ai_fail_clear()\n",
     "                # 注入：成功路径不再清熔断\n",
     "[AI 熔断] 通道恢复后必须自动解除熔断"
     "（不清的话恢复后还要白白发 10 分钟模板）",
     "ai_fail_clear"),

    # --- 自检探针把生产 AI 熔断器写坏（[自检对生产状态的副作用] 应抓）---
    # `[实测]` 2026-10-26：`[缺失数据的 AI 例外规则]` 的 prompt 拼装探针
    # 桩 `run_with_idle_watchdog` 返回**空正文** → `ai_analyze` 走
    # "空输出 = 通道不可用 → 熔断" 分支 → 写**生产** `cache/ai_fail.json`
    # 未来 10 分钟。生产日志 `[10-26 10:04:24] AI 通道熔断中（还有 5 分
    # 21 秒）` 反推置位 09:59:45 = 那次自检时刻；第二次 10:44:42 的
    # `cache/ai_fail.json` mtime 同样吻合。后果是自检把真实盯盘 AI 停了
    # 10 分钟，日志却写"主备双败"（指向网络）。
    # ⚠️ 这是**存量缺陷**（优化前备份 selfcheck.py:12891 同段）。
    # 本变异体把探针里的熔断文件重定向摘掉 —— 静态契约锚必须报出
    # "_AI_FAIL_FILE 的探针没有重定向"。
    ("sc-ai-probe-writes-prod-burnfile",
     "selfcheck.py",
     '                _A16.M._AI_FAIL_FILE = _br16\n',
     '                pass  # 注入：探针不再重定向熔断文件\n',
     "[自检对生产状态的副作用] 自检探针不得写生产 ai_fail.json"
     "（实测会把真实盯盘 AI 关 10 分钟，日志却写'主备双败'指向网络）",
     "_AI_FAIL_FILE 的探针没有重定向"),

    # --- 双败归因退化成"一律怪配置"（[AI 熔断与双败归因] 应抓）---
    # 真缺陷形态：`_dual_fail_verdict` 不分 stderr 内容、永远返回"查
    # modelOverrides"。这正是 2026-10-21 那 7 轮日志的**错误指引** ——
    # 配置是对的，真因在网关上游断流。退化成这样就等于把排查方向又
    # 带偏回去，而且日志读起来依然"很有道理"。
    # ⚠️ 2026-10-22：本变异体原锚定 `_dual_fail_verdict`（主备双败归因）。
    # 备用模型机制取消后该函数已删除，锚点变成 0 命中 → make_sandbox 里
    # apply_mutant 会报"锚点不匹配"，而这种情况**不会被计成漏网**，会被
    # 静默跳过 —— 等于一条判别力凭空消失。改为锚定单链版的
    # `_single_fail_verdict`，注入内容不变（把证据判断塌成常量）。
    ("dual-fail-verdict-collapsed",
     "analyze.py",
     '    if "unrecognized_model" in _s:\n'
     '        if "first-byte-gate-after-model-reject" in _s:\n',
     '    if True:   # 注入：不看证据，一律判成配置错\n'
     '        if True:\n',
     "[AI 熔断与双败归因] 三种 stderr 组合必须给三条不同结论"
     "（塌成一条就把排查方向永远指向 modelOverrides）",
     # ⚠️ must_contain 必须与 selfcheck.py 里**实际报出的那句话**逐字对齐
     # （2026-10-22 实测）：自检写的是「三种 stderr **特征**未产生…」，
     # 而这里原写「三种 stderr 组合」—— 判据实际已抓住（rc=1、❌=3、
     # 段正确），却因文案对不上被记成**漏网**。别名同源病：
     # 记忆 [变异体锚点里写死会变的值] 的镜像 —— 锚定的字符串必须
     # 从自检源码里抄，不能凭记忆写。
     "三种 stderr 特征"),

    # --- 生产 AI 调用漏拼 `--tools ""`（[AI 通道] 的调用点断言应抓）---
    # `[实测]` 2026-10-23 09:21：AI 为了核对「450 字上限」连调 9 次
    # Bash/Write，在 `/tmp/push_check/` 建目录、往 `~` 写
    # `tmp_push_msg.txt`/`tmp_count.py`（真落了盘，10-26 还在）。
    # `[实测]` 10-26 09:36：又试图 `python3 - <<PY` 数字数 → `-p` 非交互
    # 模式下 Bash 需审批 → 返回 `This command requires approval` →
    # 白等一轮（16s 发调用 + 154s 重新组织）。
    # 本变异体把调用点的 `*M.ai_tools_args()` 摘掉 —— 自检必须报出
    # "argv 里没拼 ai_tools_args()"。
    ("ai-tools-not-disabled",
     "analyze.py",
     '            [binp, "-p", *M.ai_model_args(model),\n'
     '             *M.ai_output_args(), *M.ai_tools_args()],',
     '            [binp, "-p", *M.ai_model_args(model),\n'
     '             *M.ai_output_args()],',
     "[AI 通道] 生产 AI 调用必须拼 ai_tools_args()（禁用内置工具）"
     "—— 否则模型会为数字数去调 Bash/Write，非交互下被拒白等一轮",
     "没拼 ai_tools_args"),

    # --- `ai_tools_args()` 拼错成缺空串（[AI 通道] 应抓）---
    # `["--tools"]` 少了那个空串时，CLI 会把**下一个 argv**（prompt 之后
    # 的参数，通常是别的 flag）当成 tools 值吃掉 → 参数解析错乱、直接
    # 报错退出 → 三处调用点全走 except → AI 静默退化成兜底模板。
    ("ai-tools-args-malformed",
     "marketlib.py",
     '    return ["--tools", ""]',
     '    return ["--tools"]',
     "[AI 通道] ai_tools_args() 必须产出 ['--tools', '']"
     "（少了空串会让 CLI 把下一个 argv 当 tools 值吃掉）",
     "ai_tools_args() 应为"),

    # --- 字数上限退回"硬上限，超了就是不合格"（2026-10-26 实测后补）---
    # `[实测]` 同一份 18428 字符的真实生产 prompt：
    #   带「硬上限，超了就是不合格」→ 模型在 thinking 里**逐字数字数**
    #     （45KB thinking、54 处 "count"、988s 才出第一个字、整轮 1312s）
    #   改成「不要逐字统计，按段落粗估」→ **62s** 出完整 8 段正文
    # 差 20 倍，且后者产出结构完全一致（触发/隔夜/关键位/关注/板块/建议/TOP3）。
    # 这条守的是"别再把数字数写回成硬指标"。
    ("prompt-wordcount-obsession-back",
     "analyze.py",
     '                      f"字数上限：**{sd.get(\'max\',300)} 字**"\n'
     '                      f"（**「关注板块」清单不计入**，先写满它再分配其余篇幅）\\n"',
     '                      f"字数上限：**{sd.get(\'max\',300)} 字**'
     '（硬上限，超了就是不合格；"\n'
     '                      f"**「关注板块」清单不计入**，先写满它再分配其余篇幅）\\n"',
     "[AI 通道] prompt 不得把字数写成「硬上限，超了就是不合格」"
     "（实测同一 prompt 下，此措辞让模型逐字数字数 12 分钟，改成粗估后 62s）",
     "硬上限"),

    # --- 耗时归因字段被吞（[执行流水新鲜度] 应抓）---
    # 2026-10-21 加：`data_ms`/`ai_ms` 是慢轮归因的唯一数据源。
    # `dur_ms` 只说"这轮慢"，分不清取数慢还是 AI 慢，而两者处置方向相反
    # （查网络/配额 vs 查网关/模型）。把写入口整段短路 = 归因字段永远
    # 不落盘，面板与自检都退回"只知道慢、不知道为什么"。
    ("runlog-attrib-dropped",
     "runlog.py",
     "    for _k, _v in ((\"data_ms\", data_ms), (\"ai_ms\", ai_ms)):",
     "    for _k, _v in ():   # 注入：耗时归因不落盘",
     "[执行流水新鲜度] runlog.run 必须把 data_ms/ai_ms 写进记录"
     "（吞掉后慢轮永远只能说「慢」、说不出「哪一段慢」）",
     "runlog.run 丢了 data_ms"),

    # --- 归因字段默认写成 0（[执行流水新鲜度] 应抓）---
    # 另一种退化形态：不传也写 0。语义上"取数耗时 0"与"根本没走到取数"
    # 是两件事 —— 早退轮次（非交易日/时间窗外）会被读成"取数瞬间完成"。
    ("runlog-attrib-zero-default",
     "runlog.py",
     "        if _v is None:\n            continue\n",
     "        if _v is None:\n            _v = 0   # 注入：缺省写成 0\n",
     "[执行流水新鲜度] 未传 data_ms 时不得写该键"
     "（写成 0 会把「没走到取数」读成「取数瞬间完成」）",
     "未传 data_ms 时仍写了该键"),

    # --- gap_s 不再落盘（[执行流水新鲜度] 应抓）---
    # 2026-10-21 加：`gap_s` 是「进程起来了但没留下任何痕迹」的**唯一**证据。
    # `[实测]` 13:27–15:04 有 42.6 分钟空白，那些轮次卡在 launchd 的
    # xpcproxy 阶段（Python 没起来），连 start 记录都写不了 —— 只有
    # 间隔能证明它们本该存在。整段删掉 = 退回"空白与没触发同形"。
    #
    # ⚠️ `must_contain` 锚在「未验证」而不是「断档」：沙箱的 runs.jsonl
    # 是从生产拷来的**历史记录**（25504 行，全是 10-21 之前的、没有 gap_s），
    # 所以变异后自检必然落进"今日暂无带 gap_s 的记录"→ 计入未验证。
    # 锚在「断档」的话这条变异体会永远 must_contain=False（夹具缺触发条件，
    # 同 [[market-watch-mutation-fixture-must-have-own-trigger]]）。
    # 而「未验证」恰好也是正确判据的一部分：字段没了就不该报 ✅。
    ("runlog-gap-not-recorded",
     "runlog.py",
     "    if phase == \"end\":\n"
     "        # 排除本进程自己的记录（本轮 start）——理由见 _last_ts 的 docstring。\n"
     "        _prev = _last_ts(",
     "    if False:   # 注入：不再记录间隔\n"
     "        _prev = _last_ts(",
     "[执行流水新鲜度] runlog 必须记录 gap_s"
     "（删掉后「进程起了却没写流水」与「launchd 没触发」又变成同形，"
     "且该项只能报未验证）",
     "未写出 gap_s"),

    # --- 断档判据的阈值被放大到永不触发（[执行流水新鲜度] 应抓）---
    # 退化形态：把 _GAP_ALERT 改成极大值，判据恒判 ok —— 检查项看起来
    # 一直在跑、一直是 ✅，而 42 分钟的断档也报正常。这是最隐蔽的一种
    # "假绿"：判据在、接线在、就是不报警。
    ("gap-threshold-never-fires",
     "selfcheck.py",
     "_GAP_ALERT = 900",
     "_GAP_ALERT = 10 ** 9   # 注入：阈值大到永不触发",
     "[执行流水新鲜度] 断档阈值必须真的能触发"
     "（放大到无穷等于判据恒绿，42 分钟空白也会报正常）",
     "流水断档判据错"),

    # --- 断档判据不按交易时段过滤（[执行流水新鲜度] 应抓）---
    # 另一种退化：把 _in_session_t 改成恒 True —— 隔夜 18 小时的间隔
    # 会被算进来，每天开盘第一轮都报「断档 18 小时」。这是**假红**，
    # 而假红会让真断档被淹没（同 [变异基线污染] 的教训）。
    ("gap-session-filter-removed",
     "selfcheck.py",
     "    return 9 * 60 + 25 <= _hm <= 15 * 60 + 35",
     "    return True   # 注入：不按交易时段过滤",
     "[执行流水新鲜度] 断档判据必须只统计交易时段"
     "（不过滤的话隔夜 18 小时每天都会报假红）",
     "流水断档判据错"),

    # --- `_isolate_run_deps` 漏挂 dcache 隔离（静态锚，2026-10-30）---
    # 真缺陷形态（栈回溯实测）：`run()` → `_run_after_inflight()` →
    # `build_brief()` 逐标的 `M.levels()` → `dcache_set()` **写生产
    # cache/daily.json**，取数失败时还 `_levels_stamp_none()` 写 none_ts。
    # 四个调用点里前三个（[演练开关]/[紧急推送配额]/[内容失效复查]）原先
    # **都漏了**这层隔离 —— 生产自检自己就被追踪到 19 次写 daily.json。
    # ⚠️ 判据只能是静态锚：沙箱里 `DIR` 就是沙箱，写的是沙箱副本，
    # `[自检对生产状态的副作用]` 按定义看不见（同 `fallback-section-dcache-leak`）。
    ("run-deps-dcache-not-isolated",
     "selfcheck.py",
     "        _stub_dc = _isolate_dcache()",
     "        _stub_dc = None   # 注入：run_deps 不再隔离 dcache",
     "[自检对生产状态的副作用] `_isolate_run_deps` 必须挂 dcache 隔离"
     "（漏了 = run() 逐标的 levels() 写生产 daily.json/none_ts）",
     "函数体内没有调用"),

    # --- `alert.NONE_TS_FILE` 未被一并重定向（静态锚，2026-10-30）---
    # 真缺陷形态：`alert.py` 自己有第二份 `NONE_TS_FILE` 常量指向同一个文件。
    # dcache 隔离改完后生产自检仍写 3 次 none_ts，栈顶全是
    # `alert.py:184 in _stamp_none` —— 后果是真实轮次 30 分钟内 3σ 告警静默。
    ("alert-none-ts-not-isolated",
     "selfcheck.py",
     "        if _orig_alert_none is not None:\n"
     "            try:\n"
     "                _AL.NONE_TS_FILE = _none_probe",
     "        if _orig_alert_none is not None:\n"
     "            try:\n"
     "                pass   # 注入：alert 的失败标记仍写生产 none_ts",
     "[自检对生产状态的副作用] `alert.NONE_TS_FILE` 必须与 "
     "`_LEVELS_NONE_FILE` 一并重定向（同一份文件的第二个常量，漏了 = "
     "自检往生产 none_ts 写 alert:*，真实轮次 3σ 告警静默 30 分钟）",
     "探针没有重定向到临时文件"),

    # --- [重写回退与流水编码] 段级 dcache 隔离必须留着（2026-10-30）---
    # 真缺陷形态（栈追踪实测）：本段 3 处 `produce_text` → `_norm_levels_line`
    # → `M.levels()` → `dcache_set()` 写**生产** daily.json；栈里
    # `dcache_set` 的 6/6 次生产写入全部来自本段的 `selfcheck.py:3504`。
    # 与 `fallback-section-dcache-leak` 同族（同一种漏隔离，不同段）。
    ("rewrite-fallback-dcache-leak",
     "selfcheck.py",
     "    _restore_dc16r = _isolate_dcache()",
     "    _restore_dc16r = (lambda: None)   # 注入：本段 dcache 隔离失效",
     "[重写回退与流水编码] 段级 dcache 重定向必须在位"
     "（漏了 = 3 处 produce_text 逐标的 levels() 写生产 daily.json/none_ts）",
     "段级 dcache 重定向未生效"),

    # --- [涨跌榜] 段级 dcache 隔离必须留着（2026-10-30）---
    # 真缺陷形态（守卫栈追踪实测）：本段 3 处 `produce_text`（⑩b 启动板块
    # 留痕、⑩c 15:00 港股措辞）收尾都跑 `_norm_levels_line` → `M.levels()`
    # → `dcache_set()`。删掉 `_STRUCT_LV_SLOTS` guard 后，正
    # 是 `selfcheck.py:3236/3255/3288` 三处把写落到了生产 `daily.json`。
    # ⚠️ 判据为什么是**接线断言**而不是行为断言（2026-10-30 实测）：
    # 把隔离换成空操作后**基线仍然 rc=0 / 0 ❌** —— 本段用例的 slot
    # （""/"11:30"/"15:00"）都不在 `_STRUCT_LV_SLOTS` 里，造段分支基线
    # 走不到。所以段首补了「DCACHE_FILE 不得等于生产路径」的当场断言，
    # 本变异体打的就是它。
    ("boards-section-dcache-leak",
     "selfcheck.py",
     "    _restore_cb_top = _isolate_dcache()",
     "    _restore_cb_top = (lambda: None)   # 注入：本段 dcache 隔离失效",
     "[涨跌榜] 段级 dcache 重定向必须在位"
     "（漏了 = 3 处 produce_text 逐标的 levels() 写生产 daily.json/none_ts）",
     "段级 dcache 重定向未生效"),

    # （此处曾计划一条 `prod-cache-guard-removed`"守卫自身被删"的变异体，
    #   **故意不加** —— 理由值得记下来：删掉守卫后，隔离齐备的代码**不会**
    #   再往生产缓存写任何东西，于是**没有任何检查项会红** → 判定为漏网。
    #   "守卫自己的判别力"不能用"删掉它看有没有人报"来测（那要求先人为
    #   造一处未隔离写入，属于夹具造触发条件 —— 可以做，但成本高于收益：
    #   上面三条隔离变异体已经各自证明"漏一处隔离 → 守卫会报 ❌"，
    #   守卫的判别力由此**间接但充分**地被验证了。）

    # --- 断档观察窗退回「只看今天」（[流水断档判据] 应抓）---
    # 真缺陷形态（2026-10-30 修掉的 P1）：自检固定 14:2x 跑，窗口写成"今天"
    # 就永远看不到 14:25–15:35 那段 —— 正好覆盖 summary(14:45) 与 15:00
    # 两个收盘槽位。`[实测]` 10-21 那场 42.6 分钟空白能被抓到纯属它横跨了
    # 当天跑点；同样的空白若发生在 14:30 就零信号。
    # ⚠️ 判据用**构造值**（`_gw_cases` 里"昨天盘内、24h 内"期望 True），
    # 不靠生产快照 —— 否则回归跑在盘中时"今天"也有盘内记录，变异体照样
    # 打 ✅，会变成时好时坏的 flaky 漏网。
    ("gap-window-today-only",
     "selfcheck.py",
     "    return _dt >= now - datetime.timedelta(hours=hours)",
     "    return _dt.date() == now.date()   # 注入：观察窗退回「只看今天」",
     "[执行流水新鲜度] 断档观察窗必须是滚动 24 小时"
     "（退回「只看今天」→ 14:25–15:35 的 summary/15:00 槽位永远不被检查）",
     "流水断档判据错"),

    # --- [投递结果] 的异常轮次结论被写死（2026-10-30 补零覆盖段）---
    # ⚠️ 为什么需要这条：R36 全量回归报「91 段，被变异体命中 90 段，
    # 零覆盖 1 段 —— [投递结果]」。根因是**该段在生产/沙箱里恒为绿**：
    # ①异常轮次要求当日 ≥3 条 analyze/error（沙箱实测 0 条）；
    # ②槽位漏投被 `MW_NOW_HM=240`（04:00）钉死在「所有槽位都没到点」。
    # 一段永远只打 ✅ 的检查项，**没有任何变异体能证明它会红** ——
    # 这正是"仪表盘冒充检查项"的形态（同 `[限流]` 的老教训）。
    # 本变异体把结论写死成 "fail"：`_error_streak_verdict()` 本身不改
    # （`[投递结果判据]` 段的 8 组构造用例照常通过），只改**接线**——
    # 测的是"判据结论 → 报警文案"这条链路真的通着。
    ("delivery-verdict-hardcoded-fail",
     "selfcheck.py",
     "            _ev_dl = _error_streak_verdict(_n_err_dl)",
     '            _ev_dl = "fail"   # 注入：异常轮次结论写死为故障',
     "[投递结果] 结论 → 报警的接线必须真的能红"
     "（写死 fail 时若仍打 ✅，说明该段是恒绿的仪表盘）",
     "当日 analyze 已"),

    # --- [投递结果] 槽位回看退回「只看当日」（2026-10-30 补，P1 修复的变异体）
    # 对应修复 `_slot_missed_win`（滚动窗口）。两个方向各一条：
    #   ① 逻辑退化 —— 循环只跑今天，昨天的盘后槽位又变回"永不检查"；
    #   ② 接线丢失 —— 产线改回调 `_slot_missed`，判据在但没人用。
    # ① 的判别力来自 [投递结果判据] 的构造值用例（沙箱里 MW_NOW_HM=240 +
    # MW_DL_WIN_H=0，行为层面恒空，构造值是唯一能看见它的地方）；
    # ② 的判别力来自同一段里 `_slot_missed_win` 的 AST 接线断言。
    # mc 都锚在"只在缺陷触发时出现"的 ❌ 文案上 —— 基线里这两句不出现。
    ("delivery-slot-today-only",
     "selfcheck.py",
     "    for _d_off in range(int(hours) // 1440 + 1, -1, -1):",
     "    for _d_off in (0,):   # 注入：只看当日，丢掉滚动窗口",
     "[投递结果] 槽位回看必须是滚动窗口"
     "（退回今日 → 14:30/summary/15:00 三个盘后槽位在 14:2x 的自检里永不检查）",
     "滚动窗口「14:24 昨日盘后漏投」"),

    ("delivery-slot-win-unwired",
     "selfcheck.py",
     "            _miss_dl = _slot_missed_win(_hm_dl, _pushed_dl, _td_win) \\\n"
     "                if _isdt_dl else []",
     "            _miss_dl = _slot_missed(_hm_dl, {s for (d, s) in _pushed_dl}) \\\n"
     "                if _isdt_dl else []   # 注入：产线改回只看当日",
     "[投递结果] 滚动窗口判据必须真的接进产线"
     "（判据在、接线退回 `_slot_missed` = 修复形同不存在）",
     "的调用点只有"),

    # --- 推送记录解析按日期裁窗口（2026-11-03 补，P2 修复的变异体）---
    # 对应修复 `_pushed_slots_from_lines`（如实解析、不按记录时间丢弃）。
    # 两个方向各一条：
    #   ① 逻辑退化 —— 解析器内部按日期丢弃旧记录（正是 P2 缺陷的形态）；
    #   ② 接线丢失 —— 产线绕开该函数，改用 inline `set()`（判据在、没人用）。
    # ① 的判别力来自 [投递结果判据] 的构造值用例（★窗口外的旧记录 + 多条混合
    #   两个用例都会红）；② 来自同一段的 AST 接线断言（调用点 2→1）。
    # mc 锚在"只在缺陷触发时出现"的 ❌ 文案上 —— 基线里这两句不出现。
    ("delivery-pushed-slots-date-filtered",
     "selfcheck.py",
     '            out.add((_t[:10], str(_p["slot"])))',
     '            if _t[:10] < "2026-11-02":\n'
     '                continue   # 注入：按记录日期裁窗口（退回 P2 缺陷形态）\n'
     '            out.add((_t[:10], str(_p["slot"])))',
     "[投递结果] 推送记录必须如实解析、不得按记录日期丢弃"
     "（丢弃 → 与补推截止口径脱节 → 把提前推成功的槽位误报成漏投）",
     "窗口外的旧记录仍须保留"),

    ("delivery-pushed-slots-unwired",
     "selfcheck.py",
     '                _pushed_dl = _pushed_slots_from_lines(_f_dl2)',
     '                _pushed_dl = set()   # 注入：产线不再调用解析函数',
     "[投递结果] 推送记录解析必须真的接进产线"
     "（解析在、产线绕开 = 修复形同不存在）",
     "的调用点只有"),

    # --- 日K腾讯分支去掉代理兜底（2026-11-03 加，P3 修复的变异体）---
    # 对应修复 `_tx_kline_fetch`。判据在 [网络降级链] 的构造值自测里
    # （直连空/WAF/异常三种形态都必须走代理）。这里把代理那一步删掉 ——
    # 正是修复前的裸 curl 形态。
    ("kline-tx-proxy-fallback-dropped",
     "marketlib.py",
     "        kl = _parse(_safe(proxy, url))",
     "        kl = None   # 注入：去掉代理兜底（退回裸 curl 形态）",
     "[网络降级链] 日K腾讯分支必须接代理兜底"
     "（去掉 → 本机 IP 被风控时 6 个指数关键位段整段消失）",
     "日K兜底"),

    # --- VIX 备用源被拔（2026-11-03 加，P3 修复的变异体）---
    # 对应修复 `_ov_sina_vix`。腾讯 usVIX 是冻结占位值，VIX 维度全靠新浪。
    # 这里把 overseas() 里的新浪源拿掉 —— 接线断言（_ov_sina_vix 按名引用）
    # 应抓。
    ("vix-sina-source-dropped",
     "marketlib.py",
     '                       ("fx", _ov_fx), ("vix", _ov_sina_vix))]',
     '                       ("fx", _ov_fx))]   # 注入：拿掉 VIX 备用源',
     "[网络降级链] VIX 备用源必须真的接进 overseas()"
     "（拿掉 → VIX 维度只剩腾讯冻结占位值）",
     "未接入 `_ov_sina_vix`"),

    # --- VIX 陈旧校验被去掉（2026-11-03 加）---
    # `_sina_vix_row` 的日期校验是**防腾讯式冻结重演**的那道闸门。
    # 去掉后，新浪若某天也冻结，会把几周前的 VIX 当今日涨跌幅推出去。
    # 判据在 [网络降级链] 的构造值自测（陈旧日期必须返回 None）。
    ('vix-stale-date-check-dropped',
     'marketlib.py',
     '        if _age < 0 or _age > _OV_STALE_H // 24:',
     '        if False:   # 注入：去掉日期新鲜度校验',
     '[网络降级链] VIX 备用源必须校验日期新鲜度（去掉 → 源冻结时把几周前的 VIX 当今日推送）',
     '❌ VIX 备用源解析不符'),

    # --- 模板兜底率阈值被抹平（[投递结果判据] 应抓，2026-11-03 加）---
    # 对应修复 `_ai_fallback_verdict` + runlog 的 ai_fallback 落盘。
    # 把判据改成恒 ok —— 构造值用例（过半必须 fail）当场红。
    ("ai-fallback-verdict-always-ok",
     "selfcheck.py",
     '    return "fail" if (_fb / _ai) >= _AI_FB_ALERT_RATIO else "warn"',
     '    return "ok"   # 注入：模板兜底率永不报警',
     "[投递结果判据] 当日过半轮次模板兜底必须报警"
     "（AI 通道实质不可用时读者拿到的全是模板文案）",
     "投递结果判据错"),

    # --- 主机判死不再留痕（[响应形状鲁棒性] 应抓）---
    # 2026-10-21 加：`_mark_dead` 此前**完全静默** —— cj() 重试耗尽后直接
    # 调它，一条日志都不打。`[实测]` 15:36:47 两台 EM 主机同时被判死，
    # watch.log 里没有任何主机失败记录，只能靠 dead_hosts.json 的 mtime
    # 反推。判死会改变后续 30 分钟的取数行为，必须可回放。
    ("mark-dead-silent",
     "marketlib.py",
     "    if _locked_update(DEAD_HOST_FILE, _mu):\n        pass",
     "    _locked_update(DEAD_HOST_FILE, _mu)\n    return   # 注入：判死不留痕",
     "[响应形状鲁棒性] 主机被判死必须留日志"
     "（静默判死的话「为什么某个源取不到数」事后完全无法回放）",
     "_mark_dead 未留痕"),

    # --- 日K取数去掉 Python 层超时（[子进程超时] 应抓）---
    # 2026-10-21 加：`curl -m 15` 管的是"整个传输"，**盖不住 DNS 解析卡死**
    # （resolver 挂起时 curl 停在 connect 之前，-m 不计时）。子进程永不返回，
    # 而 `_kline_ohlc` 在 build_brief 的关键位段里被裸调用 → 整轮退不出来、
    # 后续槽位全被 launchd 丢弃，日志无痕。
    ("kline-no-subprocess-timeout",
     "marketlib.py",
     "                    capture_output=True, timeout=20).stdout.decode(",
     "                    capture_output=True).stdout.decode(",
     "[子进程超时] 外部子进程调用必须带 Python 层 timeout"
     "（只靠 curl -m 挡不住 DNS 卡死，子进程挂住则整轮退不出来）",
     "未传 timeout"),

    # --- 子目录里的 no-timeout（[子进程超时] 扫描范围应抓）---
    # 2026-10-21 加：[子进程超时] 原先只扫 SRC **根目录**的 `*.py`，
    # `agent/run.py` 里 `subprocess.run(["uv", "run", ...])` 无 timeout
    # 判据完全看不见（真缺陷，已补 timeout=1800 + os.walk）。
    # 这条变异体在**子目录**里重新造出该缺陷：判据若退回单层扫描就漏网，
    # 保持 os.walk 就会报 `agent/run.py:NN  未传 timeout`
    # `must_contain="agent/run.py"` 防"根目录的某处报红冒认"。
    #
    # ⚠️ 锚点在 2026-10-25 迁到 Popen 形态：`agent/run.py` 的超时处理已从
    # `subprocess.run(timeout=1800)` 重写为 `Popen(...)` + `_p.wait(timeout=)`，
    # 旧锚点 `                       timeout=1800)\n` 已不存在（本轮全量回归
    # 因此报了「锚点未匹配，跳过」，两条跳过之一）。
    # 现在注入的是**去掉 wait 的 timeout**（Popen 本身没有 timeout 参数），
    # 这正是新的挂死形态；同时判据侧也已改为跟返回值要 wait/communicate 超时
    # （原先 Popen 一律豁免，会让这条变异体连"抓住"的机会都没有）。
    ("subdir-scan-blind",
     "agent/run.py",
     "        _rc = _p.wait(timeout=1800)",
     "        _rc = _p.wait()   # 注入：wait 不再带超时",
     "[子进程超时] 扫描范围必须含子目录"
     "（agent/run.py 在 agent/ 下，只扫根目录时它无超时也报不出来）",
     "agent/run.py"),

    # --- 探针只删主文件、不删 .lock 侧车（[自检对生产状态的副作用] 应抓）---
    # 2026-10-21 加：`[备用源失败可见性]` 的 permdead 探针走裸
    # `os.unlink(_perm_tmp)`，而该探针会让 `_daily_log_once` 经
    # `_locked_update` 建 `<path>.lock`。生产 cache/ 实测攒了 **34 个**
    # `.probe_permdead_*.json.lock`（10-13 起、0 字节、无人清），
    # 同期副作用段一直报「✅ 无临时文件残留」—— 因为它的残留 glob
    # 只认 `_sc_*` / `_selfcheck_*` 两个前缀，`.probe_*` 完全在射程外。
    # 两个缺陷同源：**清理与判据各用各的命名清单**。修法是把清理统一走
    # `_rm_probe`（自带 `.lock`），判据的 glob 补 `.probe*` / `*probe*`。
    ("probe-lock-leftover",
     "selfcheck.py",
     "            _Mfd._PERM_DEAD_LOG_FILE = _orig_perm\n"
     "            _rm_probe(_perm_tmp)",
     "            _Mfd._PERM_DEAD_LOG_FILE = _orig_perm\n"
     "            try:\n"
     "                os.unlink(_perm_tmp)\n"
     "            except OSError:\n"
     "                pass",
     "[自检对生产状态的副作用] 探针临时文件（含 .lock 侧车）必须清理干净"
     "（只删主文件会在生产 cache/ 里无限累积空锁）",
     "探针临时文件未清理"),

    # --- 残留判据必须认得 `.probe_*` 命名（[自检对生产状态的副作用] 应抓）---
    # 2026-10-21 加：探针命名换过三次（`_sc_` → `_selfcheck_` → `.probe_`），
    # 残留判据的前缀清单每换一次就漏一次 —— 生产 cache/ 里 34 个
    # `.probe_permdead_*.json.lock` 就是这么攒下来的（判据同期报 ✅）。
    # 这条**只注入缺陷、不动判据**：沙箱里放一个 0600 的 `.probe_*` 残留，
    # 判据必须报出来。将来若有人删掉 glob 的 `.probe*` 那两行，
    # 这条会变「漏网」—— 正是我们要的回归信号。
    # ⚠️ 不能像上一版那样**同时**删 glob 造变异：判据被删后必然看不见，
    # 变异体恒「漏网」，测不到任何东西（`[实测]` 2026-10-21 第一版）。
    ("probe-leftover-uncleaned",
     "__PROBE_LEFTOVER__",
     "", "",
     "[自检对生产状态的副作用] 残留判据必须认得 `.probe_*` 命名"
     "（探针命名换过三次，每换一次旧前缀清单就漏一次）",
     "探针临时文件未清理"),

    # --- [兜底模板与结构闸门] 段级 dcache 隔离必须留着 ---
    # 真缺陷形态（2026-10-22 实测）：本段有 9 处 `produce_text` + 若干
    # **直接调用**的下层函数（`_norm_levels_line`），它们都走
    # `M.levels()` → `dcache_set()`。段首那句
    #   `_A13.M.DCACHE_FILE = _cb13_probe`
    # 是整个段唯一的写隔离点 —— 删掉它，段内写入就落回 `daily.json`，
    # 后果是每轮沙箱自检多 44 次真实网络请求（16 次走 EM，**吃真实日
    # 配额**），且 `daily.json` 被覆写成只剩 12 键，后面两段的预热全失效。
    #
    # ⚠️ 判据不能用「生产文件被改动」——沙箱里 `DIR` 就是沙箱，
    # 写 `daily.json` 改的是**沙箱副本**，`[自检对生产状态的副作用]`
    # 按定义看不见（`[实测]` 2026-10-22 第一版就是这么写的，
    # 报 rc=0/❌数=0，纯漏网）。
    # 改用**静态锚**：段级重定向那一行必须存在。它守的是"隔离还在"，
    # 而隔离失效的直接后果是网络请求数 —— 那个没法在自检里断言
    # （自检不该依赖网络）。静态锚是这里能给出的最强判据。
    ("fallback-section-dcache-leak",
     "selfcheck.py",
     "        _A13.M.DCACHE_FILE = _cb13_probe",
     "        pass   # 注入：段级 dcache 隔离失效（写回沙箱 daily.json）",
     "[兜底模板与结构闸门] 段级 dcache 重定向必须在位"
     "（漏了 = 每轮多 44 次真实请求，其中 16 次吃 EM 日配额）",
     "段级 dcache 重定向"),
     # 同族第二条：`_LEVELS_NONE_FILE` 的段级重定向也必须留着。
     # 真缺陷形态（2026-10-30 修）：段内 7 处未加桩的 `produce_text` 会裸调
     # `M.levels()`；取数失败时 `levels()` 除了写 DCACHE_FILE（已被探针接住）
     # 还会 `_levels_stamp_none()` 写 `_LEVELS_NONE_FILE` —— 而它原先**没有**
     # 段级重定向，于是真去写生产 `cache/none_ts.json`。后果比 dcache 泄漏
     # 更重：真实轮次在 `LEVELS_NONE_TTL=1800` 秒内对该标的全部拿到 `None`，
     # **关键位段整段消失**，而自检自己印的是 ✅。
     # ⚠️ 判据同样是静态锚（写文件与否没法在自检里断言 —— 沙箱里 DIR 就是
     # 沙箱，"生产文件被改"按定义看不见；同 `fallback-section-dcache-leak`
     # 的实测记录）。
     ("fallback-section-none-ts-leak",
      "selfcheck.py",
      "        if _cb13_none_saved is not None:\n"
      "            _A13.M._LEVELS_NONE_FILE = _cb13_none_probe",
      "        if _cb13_none_saved is not None:\n"
      "            pass   # 注入：段级 none_ts 隔离失效（写回生产 none_ts.json）",
      "[兜底模板与结构闸门] 段级 _LEVELS_NONE_FILE 重定向必须在位"
      "（漏了 = 段内 levels() 失败会写生产 none_ts，真实轮次 30 分钟内"
      "关键位段整段消失）",
      "段级 _LEVELS_NONE_FILE 重定向"),

    # --- [已推送摘要] 规则不可执行（[已推送摘要] 应抓）---
    # 真缺陷形态：ai_analyze 不再调用 _today_push_digest（digest 恒空）。
    # SLOTS 里 13:30/15:00/summary 的 rule 都写着「不要重复盘中已说过的
    # 内容」，但 prompt 里若没有前文，AI 无从遵守 —— 规则成为空话。
    # 自检检查 ai_analyze 源码里是否引用了 _today_push_digest，删掉调用
    # 即应报「ai_analyze 未调用 _today_push_digest」。
    ("digest-not-injected",
     "analyze.py",
     "    digest = _today_push_digest(slot)",
     "    digest = ''  # 注入：前文不再注入 prompt",
     "ai_analyze 不再调 _today_push_digest → 「不要重复」规则不可执行"),

    # --- [自检对生产状态的副作用] 守卫自身的判别力 ---
    # 真缺陷形态：把 `[缓存逐条容错]` 的 `_HOST_FAIL_FILE` 重定向删掉，
    # 回到"原地改生产 cache/host_fail.json"。守卫判据是「AST 里找不到
    # 该重定向调用」，删掉即应报「`_HOST_FAIL_FILE` 的探针没有重定向到
    # 临时文件」。这是历史真实缺陷（第四轮修的），守卫正是为它而建。
    ("prodmutation-guard-blind",
     "selfcheck.py",
     '_hf_probe, _hf_restore = _redirect(_M8.RateLimiter, "_HOST_FAIL_FILE",\n                                           "hostfail")',
     '_hf_probe, _hf_restore = os.path.join(DIR, "state", "cache", "host_fail.json"), (lambda: None)  # 注入：不再重定向',
     "自检探针不再重定向 _HOST_FAIL_FILE → 原地改生产文件"),

    # --- [阈值默认值一致性] 三处不统一（[阈值默认值一致性] 应抓）---
    # 真缺陷形态：同一阈值键 `watch_fund_pct` 在 collect.py 与 analyze.py
    # 的字面默认值不同（8.0 vs 9.0）→ 同一时刻两模块用不同阈值，
    # 出现"collect 判它异动、analyze 判它不动"的自相矛盾。
    # 判据是正则扫三文件的 `threshold("key", 数字)`，改一处即应报。
    ("threshold-default-drift",
     "analyze.py",
     'threshold("watch_fund_pct", 8.0)',
     'threshold("watch_fund_pct", 9.0)',
     "阈值 watch_fund_pct 三处默认值不统一（8.0 vs 9.0）"),

    # --- [序列化回归] news.time 非字符串（[序列化回归] 应抓）---
    # 真缺陷形态：pending.json 的 news 条目 time 存成数字而非字符串 ——
    # datetime 那类 bug 曾让 pending.json 永远写不出（json.dumps 抛
    # TypeError）。自检要断言真的检查过条目，所以沙箱 pending.json 需有
    # news 才能进断言分支（空 news 走 `_unverified`）。
    ("news-time-not-str",
     "__DATA_PENDING_JSON__",
     "", "",
     "pending.json news.time 是数字 → [序列化回归] 应报"),

    # ============ 第十九轮：继续填零覆盖段 ============

    # --- [行情主机排序] 永久封锁主机未排末位（[行情主机排序] 应抓）---
    # 真缺陷形态：排序不再把 PERMANENTLY_DEAD 压到末位。历史真实故障：
    # push2delay 被封后 `_host_dead` 的 30 分钟 TTL 一过期，它就重新
    # 参与首位竞争（实测成功率仅 3.4% 却仍被先试）。
    #
    # ⚠️ 注入形态踩过一个坑：只把排序键的 `perm` 分量去掉**不足以触发** ——
    # push2delay 本来就同时 `dead=1`、成功率极低，即使不判 perm 仍排末位，
    # 变异体**漏网**（这是"注入没造出触发条件"，不是检查项盲区）。改成
    # 直接把永久封锁主机顶到首位，才真正复现"排序层未判 PERMANENTLY_DEAD"。
    ("em-hosts-perm-unsorted",
     "marketlib.py",
     "    return sorted(EM_HOSTS, key=key)",
     "    # 注入：把永久封锁主机顶到首位，不再压到末位\n"
     "    _perm_first = [h for h in EM_HOSTS\n"
     "                   if _host_of(h) in PERMANENTLY_DEAD]\n"
     "    _rest = [h for h in EM_HOSTS if _host_of(h) not in PERMANENTLY_DEAD]\n"
     "    return _perm_first + _rest",
     "永久封锁主机排在候选首位 → [行情主机排序] 应报"),

    # --- [响应形状鲁棒性] _has_data 恒真（[响应形状鲁棒性] 应抓）---
    # 真缺陷形态：`_has_data` 对任何输入都返回 True → 错误页/空包被当成
    # "有货"，真实失败被掩盖（同 [JSON 形状契约] 的 502 页问题）。
    ("has-data-always-true",
     "marketlib.py",
     "def _has_data(d, key=None):",
     "def _has_data(d, key=None):\n    return True  # 注入：恒判为有货",
     "_has_data 恒真 → 空包/错误页被当成有货"),

    # --- [计划窗口] deadline_dates 清空（[计划窗口] 应抓）---
    # 真缺陷形态：**sectors.json** 的 deadline_dates 为空 → 截止提醒这条
    # 推送当天不存在（历史缺陷：原先只打 ⚠️ 不置位，整天静默仍报绿）。
    # ⚠️ `config()` 读的是 sectors.json（不是 config.json）—— 数据注入
    # 型变异必须先确认目标文件，否则锚点静默不匹配。
    ("deadline-dates-empty",
     "__DATA_SECTORS_JSON__",
     "", "",
     "sectors.json deadline_dates 清空 → 截止提醒整天静默"),

    # --- [指数阈值同源] STATIC_SIGMA 被复制（[指数阈值同源] 应抓）---
    # 真缺陷形态：alert.STATIC_SIGMA 从"引用 marketlib 的同一对象"变成
    # "复制一份"（`dict(M.STATIC_SIGMA)`）→ 两处阈值从此可以各自漂移，
    # 同一指数在不同模块用不同 σ。
    ("sigma-copied-not-shared",
     "alert.py",
     "STATIC_SIGMA = M.STATIC_SIGMA",
     "STATIC_SIGMA = dict(M.STATIC_SIGMA)  # 注入：复制而非共享",
     "alert.STATIC_SIGMA 被复制 → 阈值可各自漂移"),

    # ============ 第二十轮：再填 5 段 ============

    # --- [采集取数形状] _q 去掉形状守卫（[采集取数形状] 应抓）---
    # 真缺陷形态：`_q` 不再保证返回值是 dict 或 None → 非 dict 值冒到
    # `x.get("f2")` 处 AttributeError，而 evaluate() 被 _main 裸调用，
    # 异常冒到兜底 except → 整轮采集记 "error"、pending.json 停更
    # （记忆条目 [market-watch _q 取值入口形状] 记的就是这个）。
    ("q-no-shape-guard",
     "collect.py",
     "    if not isinstance(q, dict):\n        return None",
     "    if False:  # 注入：去掉 q 的形状守卫\n        return None",
     "_q 不再验 q 形状 → 非 dict 冒到 .get 整轮采集报废"),

    # --- [快讯文本形状] _str 去掉 isinstance（[快讯文本形状] 应抓）---
    # 真缺陷形态：字段归一不再判 isinstance，非字符串 title/summary 直接
    # `.strip()` 炸 → 整批丢失 → alert 佐证闸门 fail-open 放行。
    ("news-str-no-isinstance",
     "news.py",
     '        return v.strip() if isinstance(v, str) else ""',
     "        return v.strip()  # 注入：不再判 isinstance",
     "news 字段归一去掉 isinstance → 非字符串整批丢失、闸门 fail-open"),

    # --- [缺失数据的 AI 例外规则] prompt 缺【例外】（该段应抓）---
    # 真缺陷形态：prompt 里删掉【例外】规则 → AI 会替 brief 里没给数据的
    # 板块编数字（记忆条目 [段内部分缺失静默] 的配套防护）。
    ("prompt-no-exception-rule",
     "analyze.py",
     '  ⚠️ **【例外】上面那条"必带"只对 brief 里**真的给了数据**的板块生效。**',
     '  ⚠️ 上面那条规则照旧执行。',
     "prompt 缺【例外】规则 → AI 替缺失板块编数字"),

    # --- [f-string 占位符] 引入未绑定裸名（该段应抓）---
    # 真缺陷形态：某个 f-string 里写了一个模块中从未绑定的名字 → 运行时
    # NameError（只在走到那条分支时炸，静态类型检查器未必报）。
    ("fstring-unbound-name",
     "marketlib.py",
     '                    f"{\'[\'+tag+\']\' if tag else \'\'} {m}\\n")',
     '                    f"{\'[\'+tag+\']\' if tag else \'\'} {m} {slotname_typo}\\n")',
     "f-string 占位符用了未绑定裸名 → [f-string 占位符] 应报"),

    # --- [缓存文件形状] vr_base 顶层非对象（该段应抓）---
    # 真缺陷形态：vr_base.json 顶层写成 list → hot._vr_load 的 `.get` 抛
    # AttributeError，【热点】段整轮消失且日志无堆栈（记忆条目
    # [market-watch 缓存逐条容错] 的同类）。数据注入型。
    ("vr-base-toplevel-list",
     "__DATA_VR_BASE_JSON__",
     "", "",
     "vr_base.json 顶层是 list → [缓存文件形状] 应报"),

    # ============ 第二十一轮：再填 3 段 ============

    # --- [推送通道] webhook 缺失（该段应抓）---
    # 真缺陷形态：config.json 缺 webhook → 推送必失败。数据注入型。
    ("webhook-missing",
     "__DATA_CONFIG_JSON__",
     "", "",
     "config.json 缺 webhook → [推送通道] 应报"),

    # --- [模块导入] 某模块语法错/导入失败（该段应抓）---
    # 真缺陷形态：给某模块塞一个语法错误 → import 失败 → 整条链路不可用。
    # 这是最基础的一项（模块都导不进来，其它检查全无意义）。
    ("module-import-broken",
     "hot.py",
     "import marketlib as M",
     "import marketlib as M\nimport _nonexistent_module_xyz  # 注入：导入必失败",
     "hot.py 导入不存在的模块 → [模块导入] 应报"),

    # --- [定时任务] plist 里少一个 job（该段应抓）---
    # ⚠️ 试过 `sched-job-missing`（删沙箱 LaunchAgents 里的 marketcollect
    # plist），**实测漏网** —— 且这是**测试环境无法隔离**，不是检查项盲区：
    # `[定时任务]` 读的是 `launchctl list`（**宿主机的真实 launchd 状态**），
    # 沙箱里删 plist 对它零影响。要覆盖它得真的 bootout 一个 job，代价与
    # 风险都不可接受（会停掉生产任务）。本段**只能靠人工核对**，变异测试
    # 覆盖不到 —— 已在记忆里标注这个限制。

    # ============ 第二十二轮：再填 4 段 ============

    # --- [跨进程冷却] 冷却不落盘（该段应抓）---
    # 真缺陷形态：`_fc_fail_set` 不写落盘文件（退化成进程内冷却）→
    # analyze/collect 每轮新进程，冷却每轮归零 → 每次 firecrawl 失败都
    # 白等 90s（历史真实缺陷，冷却形同虚设）。
    ("fc-fail-not-persisted",
     "marketlib.py",
     '        _atomic_write_json(_FC_FAIL_FILE,\n'
     '                           {"until": until,\n'
     '                            "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})',
     "        pass  # 注入：不再落盘，冷却退化成进程内",
     "_fc_fail_set 不落盘 → 跨进程冷却失效"),

    # --- [行情取数] quotes 恒返回空（该段应抓）---
    # 真缺陷形态：`quotes()` 取不到数且非配额耗尽 → `[行情取数]` 应报
    # 「取到 0 项」。注：quota 耗尽时它走 _unverified 分支（不报），所以
    # 变异体不能连带改配额。
    ("quotes-always-empty",
     "marketlib.py",
     "def quotes(ids, fields=\"f2,f3,f6,f12,f14,f24,f62,f104,f105,f184\",\n           allow_backup=True):",
     "def quotes(ids, fields=\"f2,f3,f6,f12,f14,f24,f62,f104,f105,f184\",\n           allow_backup=True):\n    return {}  # 注入：恒返回空",
     "quotes() 恒空 → [行情取数] 应报 0 项"),

    # --- [快讯] news 恒空（该段应抓）---
    # 真缺陷形态：`news()` 恒返回空列表（非配额耗尽）→ 段内只打 ⚠️
    # 「可能确实无快讯」，**不置位**。真缺陷是要让 fetch 崩 →
    # except 分支置位。改成抛异常。
    ("news-raises",
     "marketlib.py",
     "def news(limit=60, within_min=60, allow_backup=True):",
     "def news(limit=60, within_min=60, allow_backup=True):\n    raise RuntimeError('注入：news 崩溃')  # 注入",
     "news() 抛异常 → [快讯] except 分支应置位"),

    # --- [隔夜全球段] OVERSEAS 混入汇率误用标的（该段应抓）---
    # 真缺陷形态：OVERSEAS 里混入 usCNH/usCNY（实为 Cnh Industrial /
    # Market Vectors 两只**股票**，被误当汇率用）。该段 ④ 判据直接查
    # `OVERSEAS` 的代码列表，应报「含汇率误用标的」。
    #
    # ⚠️ 初版注入的是 `overseas()` 恒返回空 → **漏网**，核实后确认那是
    # **正确语义不是缺陷**：隔夜段取不到 = 网络失败，该段设计上计入
    # `_unverified` 而非 ❌（"探不到"不是缺陷）。变异体必须对应能让
    # 判据**真的报警**的缺陷，而不是"让它没数据"。
    ("overseas-fx-misuse",
     "marketlib.py",
     'OVERSEAS = [\n    ("usDJI",  "道琼斯"),',
     'OVERSEAS = [\n    ("usCNH",  "人民币"),\n    ("usDJI",  "道琼斯"),',
     "OVERSEAS 混入 usCNH 汇率误用标的 → [隔夜全球段] 应报"),

    # ============ 第二十三轮：再填 4 段 ============

    # --- [推送计数口径] push_ok 不排除 dry（该段应抓）---
    # 历史真缺陷：`push_ok` 只判 `p.get("ok")`、不排除 kind=="dry"，
    # 依赖"上游 DRY 一定写 ok=False"这个跨模块约定。约定被改坏后
    # 分子含演练、分母不含 → 卡片显示虚假 100% 成功率。
    ("push-ok-includes-dry",
     "webui.py",
     # 2026-10-15 复核：push_ok 口径扩为 not in ("dry","agent")（Agent 的
     # 摘要记账也写 pushes.jsonl 但不入主系统推送数），锚点随之更新；
     # [推送计数口径] 判据同步接受 != "dry" 与 not in (..., "dry", ...) 两种写法。
     '        "push_ok": sum(1 for p in pushes\n'
     '                       if p.get("kind") not in ("dry", "agent")\n'
     '                       and p.get("ok")),',
     '        "push_ok": sum(1 for p in pushes if p.get("ok")),  # 注入：不排除 dry',
     "push_ok 不再排除 dry → 成功率虚高",
     "未真正排除"),    # --- [缓存逐条容错] host_fail 整份丢弃（该段应抓）---
    # 历史真缺陷：`_load_host_fail` 逐条校验失效 → 一个坏条目让**整份**
    # 归零（其余好主机一起失去熔断保护）。
    ("hostfail-drop-all-on-bad",
     "marketlib.py",
     "                _ts = v.get(\"ts\")\n"
     "                if not isnum(_ts) or now - _ts >= 3600:\n"
     "                    continue",
     "                _ts = v.get(\"ts\")\n"
     "                if not isnum(_ts) or now - _ts >= 3600:\n"
     "                    raise ValueError('注入：坏条目整份丢弃')",
     "host_fail 一个坏条目 → 整份归零"),

    # --- [AI 调用看门狗] 调用方不再用看门狗（该段应抓）---
    # 真缺陷形态：AI 调用退回普通 `subprocess.run(timeout=)`，不再用
    # `run_with_idle_watchdog` → 慢调用被固定超时打断（历史真实缺陷：
    # 15:00 那轮跑 906s 被打断、白跑 15 分钟）。该段判据 ④ 扫**claude_bin
    # 调用方**的源码，凡调 AI 却没有 run_with_idle_watchdog 就报。
    #
    # ⚠️ 初版注入的是 `AI_IDLE_LIMIT` 放大 → **漏网**：该段测试时**自己**
    # 把 `_M14.AI_IDLE_LIMIT = 2`（测试用阈值），改模块级常量对它无效。
    # 检查项"自给阈值"时，要注入**判据真正读的东西**（这里是源码里的
    # 函数调用名），不是它运行期覆盖的常量。
    #
    # ⚠️ 2026-10-09 锚点改道：原先指 feeds.py（历史缺陷现场），但
    # feeds 的 AI 调用已随 claude_fallback 死链删除，锚点变死锚（v9 跳过）。
    # 判据 ④ 同期改成按 claude_bin 调用方自动发现 —— 注入点改到**现在
    # 唯一的** AI 调用方 analyze.py（2026-10-09 源码锚审计：该行全库唯一）。
    # 带 must_contain：必须是 ④ 报的那句、不许 baseline 杂散 ❌ 冒认。
    # ⚠️ 2026-10-13 改：调用行挪进 _invoke 内、形参多了 _env —— 锚点跟到
    # 新实现（判据 ④ 是 AST 级"调了 claude_bin 却不调看门狗"，注入点在哪
    # 个函数体无所谓，但行文本必须仍唯一匹配）。
    ("watchdog-removed-from-feeds",
     "analyze.py",
     "        _rc, _so, _se, _to = M.run_with_idle_watchdog(",
     "        _rc, _so, _se, _to = M._plain_run_XX(  # 注入：不再用看门狗",
     "analyze 调 AI 却不走看门狗 → [AI 调用看门狗] 应报",
     "调用了 claude_bin 却未接入"),

    # --- 输出读取退回阻塞式 read(n)（[AI 调用看门狗] ① 应抓）---
    # ① 号用例专为这个坑而设（read(n) 在管道上阻塞到读满 n 字节，慢速
    # 产出的进程永远凑不满 → last[0] 不更新 → 被判卡死杀掉），但**此前
    # 没有任何变异体验证 ① 真的能抓它** —— 判据在、射程空。
    # 注入：read1 改回 read(65536)。
    ("watchdog-read-blocks",
     "marketlib.py",
     '                chunk = stream.read1(65536) if hasattr(stream, "read1") \\\n'
     '                        else stream.read(4096)',
     "                chunk = stream.read(65536)  # 注入：退回阻塞式 read",
     "读取退回 read(65536) → [AI 调用看门狗] ① 应报「持续有输出的进程被判卡死」",
     "计时器没随输出重置"),

    # --- 首字节闸门失效（[AI 调用看门狗] ⑤ 应抓）---    # 真缺陷形态：`_first_byte_dead` 判定被删/恒 False —— 零产出挂起链
    # 退回「等满 600s 才切备用」，9 次卡死每轮白等 4 分钟。注入：判定恒 False。
    # must_contain 必须是 ⑤ 号用例的「首字节闸门失效」句，防 baseline 杂散 ❌。
    # --- 启动板块留痕被删（[涨跌榜] 源级锚应抓）---
    # 2026-10-13 补的观测：brief 有【启动板块】数据而产物缺段必须留日志。
    # 变异体删掉观测调用 → 源级锚（「启动板块段缺失」留痕）应报。
    ("start-board-missing-log-gone",
     "analyze.py",
     '            _log(f"AI 输出缺「启动板块」段（brief 含启动板块数据）"\n'
     '                 f"slot={slot or \'event\'}")',
     "                pass  # 注入：启动板块缺失不留痕",
     "[涨跌榜] produce_text 未对「启动板块段缺失」留痕应被抓",
     "启动板块段缺失"),

    ('first-byte-gate-dead',
     'marketlib.py',
     '        if _idle > idle_limit or _first_byte_dead or _text_dead:',
     '        if _idle > idle_limit or _text_dead:  # 注入：首字节闸门失效',
     '首字节闸门失效 → [AI 调用看门狗] ⑤ 应报「首字节闸门失效」',
     '首字节闸门失效'),

    # --- 首字节闸门触发了但不写来源标记（[AI 调用看门狗] ⑤ 应抓）---
    # 2026-10-17 新增：⑤ 的主判据从「耗时 <6s」改成「stderr 有来源标记」
    # （耗时在宿主高负载下会假红，实测 7s → 误推一条群告警）。判据换了
    # 就必须有对应变异体 —— 否则「标记没了」这个新失败形态没有任何东西
    # 验证 ⑤ 能抓住它。
    # 注入：删掉标记写入行。闸门本身照常 2s 杀进程（to=True、耗时 ~2s），
    # 只有 stderr 里没有 `[first-byte-gate` —— 耗时判据完全看不见它，
    # 正是这条新判据的存在理由。
    ("first-byte-marker-gone",
     "marketlib.py",
     '    elif timed_out and _fb_only:\n'
     '        _se = f"[first-byte-gate {first_byte_limit}s 触发，非 idle 闸门] " + _se\n',
     '',
     "首字节闸门触发时不写来源标记 → [AI 调用看门狗] ⑤ 应报「无首字节来源标记」",
     "无首字节来源标记"),

    # --- [窗口上界自洽] _win_end 不含 GRACE（该段应抓）---
    # 真缺陷形态：`analyze.main()` 的 `_win_end` 少了 `HOURLY_RETRY_GRACE`
    # → 最后一个槽位的宽限是**死代码**（main 先 return out_of_window，
    # 补推通道堵死）。该段判据直接读 analyze.py 里 `_win_end` 的源码
    # 表达式、查它含不含 GRACE。
    ("win-end-no-grace",
     "analyze.py",
     "        _win_end = _last_t + SLOT_TTL.get(_last_label, 20) + HOURLY_RETRY_GRACE",
     "        _win_end = _last_t + SLOT_TTL.get(_last_label, 20)  # 注入：宽限成死代码",
     "_win_end 不含 HOURLY_RETRY_GRACE → [窗口上界自洽] 应报"),

    # --- [槽位宽限上界] 宽限越过下一窗口（该段应抓）---
    # 真缺陷形态：`hourly_slot._grace_cap` 恒返回 None → 槽位的重试宽限
    # 不再止于**下一个窗口的起点**，会吃掉别人的时段（历史真实缺陷：
    # 14:30 的上界曾是 15:00，而 summary 窗口是 [14:45, 14:58]）。
    # 该段判据用「各窗口起点」作**独立锚**（不能用 HOURLY_RETRY_GRACE ——
    # 宽限就是它给的，改它两者同步变化，永远报 ✅）。
    ("grace-cap-disabled",
     "analyze.py",
     "    def _grace_cap(target):\n        later = [t for t in _all_starts if t > target]\n        return min(later) if later else None",
     "    def _grace_cap(target):\n        return None  # 注入：宽限不再止于下一窗口",
     "_grace_cap 失效 → 宽限越过下一窗口"),

    # --- [AI 通道] claude_bin 找不到 CLI（该段应抓）---
    # 真缺陷形态：`claude_bin()` 恒返回空 → 找不到 CLI → AI 静默退化为
    # 兜底模板（记忆条目 [market-watch claude CLI 瞬断]）。
    ("claude-bin-missing",
     "marketlib.py",
     "def claude_bin():",
     "def claude_bin():\n    return (None, None)  # 注入：找不到 CLI",
     "claude_bin 恒空 → [AI 通道] 应报未找到 CLI"),

    # --- [AI 通道] 抗瞬断重试被摘（该段应抓）---
    # 真缺陷形态：`claude_bin()` 的 6×1s 重试循环被删掉 → 符号链接在 npm
    # 自动更新窗口内悬空的那一瞬间，整条推送退化成兜底模板。
    # `[实测]` 2026-10-08 生产 watch.log：11:09:53 与 14:03:34 两轮各报一次
    # 「未找到 claude CLI」，两条推送因此退化成兜底模板（327/384 字符，
    # 而正常 AI 推送是 540~720 字符）。
    # 判据是**行为**的：桩掉 `_claude_bin_probe` 让它前 N 次返回空，
    # 若重试循环被摘，桩只会被调 1 次 → ❌。
    ("claude-bin-no-retry",
     "marketlib.py",
     "    for _attempt in range(CLI_PROBE_ATTEMPTS + 1):"
     "   # 1 次首探 + CLI_PROBE_ATTEMPTS 次重试",
     "    for _attempt in range(1):  # 注入：抗瞬断重试被摘",
     "claude_bin 不再重试 → [AI 通道] 应报重试行为异常"),

    # --- [港股取数降级] fail-closed 退回 except 分支（该段应抓）---
    # 历史真实缺陷：`index_quotes` 把「删掉港股键」只装在 `except` 分支里，
    # 于是只挡住**抛异常**那一种失败。腾讯还有一条**不抛异常**的失败路径
    # （`from_tencent` 对解析不出的行 continue、整批失败时安静返回 {} ——
    # 限流/返回体截断/字段位变动都会这样），此时港股的 EM 陈旧值原样留在
    # out 里，下一轮 check_index 拿它比阈值 → 假警报。
    # 实测（2026-09-27）：抛异常时港股键被删（0 条告警），返回 {} 时却推出
    # 「恒生指数 +4.61%（阈 3.54%）」，而 4.61% 正是注释点名要防的陈旧值。
    ("hk-failclosed-only-in-except",
     "alert.py",
     "        for k in hk:\n"
     "            out.pop(k.split(\".\")[-1], None)\n"
     "        try:\n"
     "            import feeds",
     "        try:\n"
     "            import feeds",
     "港股无条件摘除被删 → 静默返回空时陈旧值残留"),

    # --- [兜底模板建议段] 建议段退回固定空话（该段应抓）---
    # 历史真实缺陷（记忆条目 [market-watch 兜底模板建议段空话]）：
    # `_risk_txt`/`_opp_txt` 在 risk/opp 为空时**不看 `_no_data`**，
    # 直接输出两句固定套话「留意资金持续流出的方向」「留意资金承接的方向」。
    # 实测 2026-09-28 15:00 那轮（AI 900s 超时后退回模板）真实投递过。
    # 危害：`_pans` 段有「数据本轮不可用」标注、`_seg_txt` 有「数据暂缺」，
    # 唯独建议段两者都没有 —— 读者无法区分"真的没有机会"与"数据根本没拿到"。
    ("advice-back-to-empty-phrases",
     "analyze.py",
     "    _risk_txt = '；'.join(risk) if risk else (\n"
     "        \"数据本轮不可用，不据此判断方向。\" if _no_data\n"
     "        else \"本轮无资金数据（盘前常见），不据此判断资金方向。\" if _no_fund\n"
     "        else \"无板块触及流出阈值\")\n"
     "    _opp_txt = '；'.join(opp) if opp else (\n"
     "        \"数据本轮不可用，不据此判断方向。\" if _no_data\n"
     "        else \"本轮无资金数据（盘前常见），不据此判断资金方向。\" if _no_fund\n"
     "        else \"全场无相对占优方向（无净流入且无跌幅明显较小者）\")",
     "    _risk_txt = '；'.join(risk) if risk else '留意资金持续流出的方向'\n"
     "    _opp_txt = '；'.join(opp) if opp else '留意资金承接的方向'",
     "兜底模板建议段退回固定空话 → AI 超时时读者拿不到方向"),

    # --- [面板时段归因] 面板轻信 fired（该段应抓）---
    # 历史真实缺陷：`done = ... or (f"hourly_{key}" in fired)` 对 fired 完全
    # 信任，而 fired 有两个来源 —— 只有 `_commit()` 那个意味着"消息已发出"；
    # `load_st` 的陈旧清理会把被打断进程遗留的 inflight **转正**到 fired。
    # 代码用 `_fired_no_push` 纠正（fired 有、推送记录没有 → missed），
    # 删掉该纠正分支后，未发出的消息会被面板报成 done。
    ("panel-trusts-fired",
     "__DATA_PANEL_FIRED_NOPUSH__",
     "", "",
     "fired 有但无成功推送记录 → 面板应报 missed 而非 done"),

    # --- fired 里认不出的键在面板上无声消失（[面板时段归因] 应抓）---
    # 真缺陷形态（P2-7 第三项）：`_fired_labels` 原先是 `if label:`，
    # 所有映射都不匹配的键直接从 chips 里消失 —— 而 fired 是"当天该事件
    # 已推送过、不会再推"的去重记录，每少显示一个键就等于"有一条消息
    # 发过却查不到轨迹"。analyze.detect 新增一个事件键、或 state.json
    # 被写进污染数据时必然触发，面板会显示成"今天什么都没发生"。
    # 判据是**行为级**（真的调 `_fired_labels` 看输出里 raw 键还在不在），
    # 所以这里退回 `if label:` 就能被抓 —— 源码里 `未识别` 三个字仍在，
    # 查字符串的判据抓不到。
    ("panel-fired-label-dropped",
     "webui.py",
     '        if not label:\n'
     '            label = f"未识别 {raw}"\n'
     '        out.append({"label": label, "raw": raw})',
     '        if label:\n'
     '            out.append({"label": label, "raw": raw})',
     "fired 认不出的键被静默丢弃 → [面板时段归因] 应报「chips 里无声消失」"),

    # --- 慢轮吞槽位阈值（该段应抓）---
    # 判据 `_starved_verdict` 的 fail 分支被删（阈值形同虚设）→ 当日吞
    # 14 次（10-16 实测形态）仍报 ℹ️。`[吞槽位归因]` 用构造值直接喂，
    # 不依赖沙箱能否造出慢轮。
    ("starved-threshold-removed",
     "selfcheck.py",
     '    if starved_today >= _STARVED_ALERT:\n        return "fail"\n',
     '    pass  # 注入：吞槽位阈值判据被删\n',
     "吞槽位阈值判据被删 → [吞槽位归因] 应报「吞 14 次仍判 ok」（实得 warn）",
     "吞槽位归因判据错"),

    # --- 自检漏跑判据（该段应抓）---
    # 前三条各打断判定（`_sc_missed_trading_days`）的一个必要条件：交易日
    # 过滤 / 开机证据 / 今天边界；第四条打断**标记解析**（`_sc_mark_dts`）。
    # 解析路径的真实调用读 `selfcheck.out`，而沙箱**不带该文件**（自检输出
    # 走 subprocess 管道）—— 不抽成纯函数的话，正则被改坏在回归里没有任何
    # 东西会变（判据在、射程空）。所以四条都靠 `[自检新鲜度归因]` 的构造值。
    ("sc-fresh-holiday-not-skipped",
     "selfcheck.py",
     '        if is_td(d) and d.isoformat() in alive_days:',
     '        if d.isoformat() in alive_days:  # 注入：交易日过滤被删',
     "交易日过滤被删 → [自检新鲜度归因] 应报「休市日不算漏跑」判错"
     "（周末被算成漏跑）",
     "自检新鲜度判据错"),

    ("sc-fresh-alive-not-required",
     "selfcheck.py",
     '        if is_td(d) and d.isoformat() in alive_days:',
     '        if is_td(d):  # 注入：开机证据判据被删',
     "开机证据判据被删 → [自检新鲜度归因] 应报「机器没开的日子不算漏跑」"
     "判错（关机日被算成漏跑）",
     "自检新鲜度判据错"),

    ("sc-fresh-window-inclusive",
     "selfcheck.py",
     '    while d < today:',
     '    while d <= today:  # 注入：今天也被算进漏跑窗口',
     "今天边界写成闭区间 → [自检新鲜度归因] 应报「今天不参与判定」判错"
     "（今天永远算漏跑）",
     "自检新鲜度判据错"),

    ("sc-fresh-marker-regex-broken",
     "selfcheck.py",
     '^=== 盯盘系统自检 · (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}) ===$',
     '^= 盯盘系统自检 · (\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}) =$',
     "标记行正则被改坏（= 数量不对）→ [自检新鲜度归因] 应报「标准标记行」"
     "解析为空（生产里恒报「无历史 → 未验证」，自检等于永远不知道自己跑没跑）",
     "自检新鲜度判据错"),

    # --- [数据源] 部分源不可用（该段应抓）---
    # `health()` 里任一源假 → `_dead` 非空 → 报「N 个源不可用，降级链少 N 档」。
    # 只让 baidu+ths 假（不是全假）：全假会走进
    # `if not any(h.values())` 那条分支，而那条分支现在只计 `_unverified`
    # （缺陷 27：原实现真调当时的 `feeds.ensure_source()` → `claude_fallback()`
    # （两者均已于 2026-10-09 删除，见 dead-code-restore-* 变异体）
    # 唤起真 Claude，无总时长上限，实测挂起 >400s 且占着 selfcheck.lock）。
    # 也就是说「全坏」是未覆盖路径，「部分坏」才是判别力所在 —— 写
    # 「全坏」的变异体会因为挂起而被误判成「没抓住」。
    #
    # ⚠️ 走**数据注入型**而非改源码（2026-10-10）：`feeds.health()` 顶部加了
    # `MW_HEALTH_FIXTURE` 短路（见其 docstring —— 为了不让外部源的间歇风控
    # 污染基线闸门），而 `res = {}` 那行在短路**之后**，改源码对返回值毫无
    # 影响 → 实测报「漏网」（rc=0/❌=0）。修法：直接改**夹具文件**
    # `health_fixture.json` 的键值 —— 与 `sched-selfcheck-rc2-alarm` 同款
    # （数据注入型：源码不动，测的是「这种健康表出现时检查必须报」）。
    ("feeds-partial-dead",
     "__DATA_HEALTH_PARTIAL__",
     "", "",
     "2 个源假 → [数据源] 应报「降级链少 2 档」并置失败",
     "降级链少 2 档"),

    # --- [数据源] 风控分档：被拦截的源不得按「坏源」算（该段应抓）---
    # 真缺陷形态（2026-10-16 生产实测，17:42 那条 webhook 即此因）：
    # `from_baidu` 命中风控时若**不置** RISK_BLOCKED（或置位后被误清、
    # 或判据把风控源也计入 _dead），`health()["baidu"]` 的假与「解析坏/
    # 接口改版」同形 → 自检打 ❌ → ok_all=False → rc=1 → webhook 打扰。
    # 百度风控是**间歇性**的（实测 17:30 拦、18:0x 仍拦、18:4x 自愈，
    # 窗口数十分钟），重试救不了；把它分档成 ⚠️「本轮未验证」才不打扰。
    #
    # ⚠️ 走数据注入型（同 feeds-partial-dead）：夹具 `health_fixture.json`
    # 新增可选键 `_risk`（源名列表）—— `feeds.health()` 的夹具分支读它、
    # 填 `feeds.RISK_BLOCKED`，同时保持返回值里**没有** `_risk`（它是
    # 元信息、不是源；返回前 pop，否则会被当成一个源、打印 `⚠️ _risk`、
    # 并可能计入 _dead → 假 ❌）。造的是「baidu 假 + 有风控信号」这一
    # **唯一**输入形态（假 + 无信号就是 feeds-partial-dead，已被覆盖）。
    ("feeds-risk-blocked",
     "__DATA_HEALTH_RISK__",
     "", "",
     "源被风控拦截 → [数据源] 应报 ⚠️「本轮未验证」、不得按坏源算（不 ❌、不降级链损耗）",
     "baidu 被目标站点风控拦截"),

    # --- 风控信号（RISK_BLOCKED）在 from_baidu 侧的置位正确性（[备用源失败可见性] 应抓）---
    # `feeds-risk-blocked` 测的是**消费者**一侧（分档逻辑在位、[数据源] 用得上）；
    # 这两条测**生产者**一侧 —— `from_baidu` 置位的**方向**必须对：
    #   · 只丢了风控例的置位 → [数据源] 的风控分档永远不触发，风控又被打回
    #     ❌ → rc=1 → webhook 打扰（回到 2026-10-16 17:42 那条旧病）；
    #   · 无差别置位（结构改版/非 JSON 也置）→ **真的坏了**的源被误分档成
    #     "风控"、⚠️ 不计 rc → 源坏掉却不告警。两个方向都比打扰更危险
    #     （一个白打扰，一个坏源不告警）—— 各一条变异体。
    ("feeds-risk-signal-dropped",
     "feeds.py",
     '                RISK_BLOCKED.add("baidu")',
     '                pass  # 注入：风控信号丢失（风控例不再置位）',
     "from_baidu 命中风控却不置 RISK_BLOCKED → [备用源失败可见性] 应报",
     "未把源记入"),

    ("feeds-risk-flag-forged",
     "feeds.py",
     '    """百度股市通。返回 {key: {name,px,chg,high,low,preclose}}"""\n'
     '    out = {}',
     '    """百度股市通。返回 {key: {name,px,chg,high,low,preclose}}"""\n'
     '    RISK_BLOCKED.add("baidu")   # 注入：不管成败都置风控信号\n'
     '    out = {}',
     "非风控失败也置 RISK_BLOCKED → 真故障被误分档成风控（不计 ❌）→ 应报",
     "非风控失败也置了"),

    # --- [AI 通道] 看门狗超时但 stdout 非空（该段应抓）---
    # 缺陷二十八：`if _to:` 只打 ⚠️、既不置失败也不计未验证，于是若被杀前
    # 吐了一半输出，会继续走进 `got` 分支打印 **✅ 冒烟成功** ——
    # 一个**已被终止**的调用报成功，是最坏的误导。
    # 注入：覆写 run_with_idle_watchdog 让它返回 (0, "OK", "", timed_out=True)。
    ("ai-watchdog-timedout-ok-output",
     "marketlib.py",
     # ⚠️ 2026-10-26 签名加 `total_limit`（单轮 AI 预算闸门）—— 锚点必须
     # 跟着改，否则 apply_mutant 字面失配、静默变「跳过」，本变异体失去
     # 判别力（[[market-watch-mutation-anchor-drift]]）。
     # ⚠️ 2026-10-27：加 `total_limit` 后签名**折成两行**（源码实际形态），
     # 单行锚点仍失配 —— 锚点必须逐字节照抄源码的换行与缩进（27 空格）。
     "def run_with_idle_watchdog(args, idle_limit=None, first_byte_limit=None,\n"
     "                           total_limit=None, stdin_text=None, **kw):",
     "def run_with_idle_watchdog(args, idle_limit=None, first_byte_limit=None,\n"
     "                           total_limit=None, stdin_text=None, **kw):\n"
     "    if '回复 OK' in args:          # 只拦截 AI 冒烟那次调用\n"
     "        return (0, 'OK', '', True)\n"
     "    return _real_rwiw(args, idle_limit, first_byte_limit, total_limit, stdin_text, **kw)\n"
     "def _real_rwiw(args, idle_limit=None, first_byte_limit=None,\n"
     "               total_limit=None, stdin_text=None, **kw):",
     "看门狗已终止 + stdout 非空 → [AI 通道] 应报 ❌，不得打 ✅ 冒烟成功",
     "看门狗判定卡死"),

    # --- [定时任务] 有 job 未加载（该段应抓）---
    # 上一轮我把本段定性为「不可测 —— 读宿主机 launchctl 状态，沙箱隔离不了」。
    # **错的**：本段读的是 `launchctl list`（真实进程列表，**不是** plist 文件），
    # 所以**不需要沙箱隔离**就能测 —— 只要往 `_jobs` 里塞一个**幻影 label**，
    # 它在 `launchctl list` 里必然不存在 → 该段应报 ❌。
    # 上一轮之所以误判，是因为我删的是**沙箱里的 plist 文件**（那属于
    # `[调度方式]`，`sched-calendar-dead` 已在测），与本段无关。
    ("sched-phantom-job",
     "selfcheck.py",
     '    _jobs = ["com.mison.marketwatch", "com.mison.marketcollect",\n'
     '             "com.mison.marketwebui", "com.mison.marketselfcheck",\n'
     '             # 2026-10-15 复核补：调研 Agent（2026-10-13 上线）此前不在\n'
     '             # 对账名单 —— 它的 launchd 健康（未加载/反复失败）无人看。\n'
     '             # agent/run.py 已调 harden/rotate，[定时任务] 3c 对账覆盖其日志。\n'
     '             "com.mison.marketagent"]',
     '    _jobs = ["com.mison.marketwatch", "com.mison.marketcollect",\n'
     '             "com.mison.marketwebui", "com.mison.marketselfcheck",\n'
     '             "com.mison.marketagent", "com.mison.marketphantom"]',
     "有 job 未加载 → [定时任务] 应报 ❌（上一轮误判为不可测）"),

    # --- [板块列表] clist 探不通（该段应记为未验证）---
    # 缺陷三十：非配额原因的 `clist 不可用` 只打 ⚠️、既不置失败也不计未验证，
    # 于是总体报「✅ 正常（全部检查项均已验证）」，而 clist 其实探不通。
    # clist 是 hot.py（热点候选池）与 alert.py（板块数据）的**唯一来源**。
    # 本段合法地**不打 ❌**（缺热点池非致命），但**必须**计入未验证 ——
    # 故本变异体靠 runner 的第三种判据（基线差：新增未验证段）抓住。
    ("clist-unavailable",
     "marketlib.py",
     'def clist_available():\n    """探测 clist 是否可用。与 clist() 一致，含主机故障转移。"""',
     'def clist_available():\n    """探测 clist 是否可用。与 clist() 一致，含主机故障转移。"""\n'
     '    return False',
     "clist 探不通（非配额）→ [板块列表] 应记为未验证，不得报「全部已验证」"),

    # --- [AI 通道] 冒烟返回空 stdout（该段应报 AI 空输出并带出 stderr）---
    # 缺陷三十一：`class _R` 桩只定义了 `stdout`，而空输出分支要打
    # `stderr={r.stderr[:120]!r}` —— AI 返回空（CLI 降级的真实故障）时，
    # 读者拿到的是 `❌ AI 冒烟失败: '_R' object has no attribute 'stderr'`，
    # 指向**自检自身**而非要排查的 AI 通道，真正的 stderr 一个字都看不到。
    # 抓它必须注入「空 stdout + 非超时」：超时那条分支不走 stderr 读取。
    ("ai-smoke-empty-stderr-blinded",
     "marketlib.py",
     # ⚠️ 2026-10-26 签名加 `total_limit`，锚点同步（同上）。
     # ⚠️ 2026-10-27：签名折两行，锚点按源码逐字节（见上一条说明）。
     "def run_with_idle_watchdog(args, idle_limit=None, first_byte_limit=None,\n"
     "                           total_limit=None, stdin_text=None, **kw):",
     "def run_with_idle_watchdog(args, idle_limit=None, first_byte_limit=None,\n"
     "                           total_limit=None, stdin_text=None, **kw):\n"
     "    if '回复 OK' in args:          # 只拦截 AI 冒烟那次调用\n"
     "        return (0, '', 'boom', False)\n"
     "    return _real_rwiw(args, idle_limit, first_byte_limit, total_limit, stdin_text, **kw)\n"
     "def _real_rwiw(args, idle_limit=None, first_byte_limit=None,\n"
     "               total_limit=None, stdin_text=None, **kw):",
     "AI 空输出 → [AI 通道] 应报「AI 空输出」并带出 stderr，而非 AttributeError",
     "AI 空输出"),

    # --- [AI 通道] ai_model_args 的 None 分支失效（该段应抓）---
    # 真缺陷形态：`AI_MODEL=None`（不传 --model，用 CLI 默认模型）时，
    # 若 ai_model_args 的 None 分支写坏、产出 `["--model", None]`，CLI 会因
    # 无效模型名失败 → 三处调用点（analyze/feeds/selfcheck 冒烟）全走
    # except → AI 静默退化为兜底模板，日志里只有一个 traceback。
    # ⚠️ 判据是 [AI 通道] 段里的一致性断言（行为断言、不在冒烟分支内）——
    # runner 会设 MW_SKIP_AI_PROBE=1，若断言写在冒烟分支里本变异体会**假漏网**。
    # ⚠️ 2026-10-13 改：ai_model_args 加了 model 形参（备用模型链），返回行
    # 从 `["--model", AI_MODEL]` 变为 `["--model", m]`（m 经 AI_MODEL 兜底）。
    # 锚点跟着新实现走 —— 变异目标不变：None 兜底分支写坏、None 泄漏进
    # --model。判据仍是 [AI 通道] 的一致性断言（AI_MODEL=None → []）。
    ("ai-model-args-none-leak",
     "marketlib.py",
     '    m = AI_MODEL if model is None else model\n    return ["--model", m] if m else []',
     '    m = AI_MODEL if model is None else model\n    return ["--model", AI_MODEL]  # 注入：None 泄漏成 --model None',
     "ai_model_args 的 None 分支失效（产出 ['--model', None]）"
     "→ [AI 通道] 应报「与 AI_MODEL 不一致」",
     "与 AI_MODEL 不一致"),

    # --- 解释器版本检查：低版本必须 rc≠0（不是 rc=0 的"静默通过"）---
    # 缺陷三十二：版本检查原先 `print("⚠️…"); return False`，而 `_main`
    # 正常路径返回 int（0/1），`False == 0` → main() 拿到 rc=0、**不打印任何
    # 结论行**。盘面只有标题 + 一句 ⚠️，退出码 0 —— 「自检跑不起来（落错
    # 解释器）」与「系统一切正常」**无法区分**（第十二轮 SystemExit 同类）。
    # 注入 `if True:` 强制走该分支：修复前 rc=0 无 ❌（漏网），修复后 rc=1。
    # ⚠️ 本项在**任何 `[...]` 检查段之前**，故 ❌ 无段可归（段=—）——
    # 靠 rc≠0 判据抓住，不进覆盖率矩阵（它不是 56 段之一）。
    ("version-check-silent-pass",
     "selfcheck.py",
     "    _v = sys.version_info\n    if _v < (3, 12):",
     "    _v = sys.version_info\n    if True:   # 注入：强制走低版本分支",
     "低版本解释器 → 版本检查应 ❌ + rc=1，不得 rc=0 静默通过"),

    # --- [返回类型契约] 元守卫：`_main` 返回 `False`（该段应抓）---
    # 缺陷三十二的形状：`return False` 而 `_main` 正常返回 int，`False == 0`
    # → rc=0、无结论行，静默通过。注入点选版本检查的 `return 1`（生产解释器
    # 3.14 下 `if _v < (3,12)` 为假、该 return **不会被执行**）——
    # 于是本变异体**唯一**的可观测效果就是「[返回类型契约] 静态扫到它」。
    # 删掉该守卫 → 注入无效果 → rc=0 无 ❌（漏网），故能判别守卫的存在。
    ("return-contract-false",
     "selfcheck.py",
     "        return 1\n\n    # 1) 数据源",
     "        return False\n\n    # 1) 数据源",
     "[返回类型契约] 应抓住 _main 的 `return False`（False==0 致 rc=0 静默）"),

    # --- [节假日表] 多填一个休市日（该段应抓）---
    # 2026-10-06 **生产真缺陷**：HOLIDAYS 里多写了 "2026-10-08"，
    # 而上交所公告〔2026〕22 号明写「国庆 10/1–10/7 休市，10/8 起开市」。
    # 后果：节后**第一个交易日**整天判成休市 → 不采集、不推送、一条简报
    # 都没有 —— 而那正是最该盯盘的一天。
    # 难抓的原因：段内原有三条判据全都不成立 ——
    #   ① HOLIDAYS 非空（26 ≠ 0）② 覆盖期 451 天（> 90）
    #   ③「表内休市日被判成交易日」——探针取**表内**某天，而"多填一天"
    #      恰恰让这条**通过**（判据与被测对象同源）。
    # 修复靠**表外的独立事实**：硬编码公告复市日（golden anchor）交叉核对。
    ("holiday-table-extra-day",
     "marketlib.py",
     '    "2026-10-06", "2026-10-07",                     # 国庆',
     '    "2026-10-06", "2026-10-07", "2026-10-08",       # 国庆',
     "多填 10-08（公告复市日）→ [节假日表] 应报「该日不采集不推送」"),

    # --- [节假日表] 漏填一个休市日（该段应抓）---
    # 与「多填」方向相反、危害不同：多填 → 交易日被当休市（漏推）；
    # 漏填 → 休市日被当交易日（**空跑一整天**，若有异动还会往群里推假播报）。
    # `[实测]` 删掉 "2026-10-07" 后原自检 rc=0 全绿，而 10-07 被判成交易日
    # （长假倒数第二天，A股确定休市）。
    ("holiday-table-missing-day",
     "marketlib.py",
     '    "2026-10-06", "2026-10-07",                     # 国庆',
     '    "2026-10-06",                                   # 国庆',
     "漏填 10-07（公告休市日）→ [节假日表] 应报「该日会空跑并可能推假消息」"),

    # --- [plan.md 新鲜度] 市场位置过期 / 关键位漂移（该段应抓）---
    # plan.md 是**唯一**"人写的市场判断"进入 AI prompt 的通道，它坏了
    # 不会让任何检查报错，只是**默默按过期认知写稿**。
    # ① 数据注入：把「当前市场位置（2026-09-30）」改成更早的日期；
    # ② 数据注入：把关键位 4300 改成与 sectors.json 不一致的值。
    # 判据的独立锚：交易日间隔用 `is_trading_day`（读 HOLIDAYS，与 plan
    # 无关）、关键位真值从 `sectors.json` 读（不是从 plan.md 抄）。
    ("plan-md-stale-position",
     "__DATA_PLAN_STALE__",
     "", "",
     "plan.md 市场位置停在 10 个交易日前 → [plan.md 新鲜度] 应报 ❌"),
    ("plan-md-threshold-drift",
     "__DATA_PLAN_DRIFT__",
     "", "",
     "plan.md 关键位 4300→4200 与 sectors.json 漂移 → 该段应报 ❌"),

    # --- [阈值兜底一致性] 三源漂移（2026-10-24 新增段应抓）---
    # ⚠️ 走**特殊处理**（apply_mutant 里按 name 分支）：锚点必须**现读**
    # sectors.json 的 hstech_up 再改 TH —— 写死 "3995"/"3781" 的话，
    # 下次重校准就 0 匹配、静默跳过（本项目反复栽的"锚点未匹配却报全绿"）。
    ("fallback-th-drift",
     "marketlib.py",
     "", "",
     "TH['hstech_up'] 与 sectors.json 漂移 → [阈值兜底一致性] 应报 ❌",
     "阈值兜底已与真值漂移"),
    # --- [阈值兜底一致性] 调用点字面量与配置漂移 ---
    ("fallback-literal-drift",
     "analyze.py",
     "", "",
     "analyze 的 threshold(\"hstech_down\", N) 字面值与配置漂移 → 该段应报 ❌",
     "阈值兜底已与真值漂移"),
    # --- [nvm 版本选择] 排序退化成字典序（2026-10-24 新增段应抓）---
    ("nvm-lexicographic-sort",
     "marketlib.py",
     '        _vs.sort(key=lambda s: [int(x) for x in re.findall(r"\\d+", s)])',
     '        _vs.sort()   # 注入：字典序（v24.9.0 > v24.22.1，选到更旧的版本）',
     "[nvm 版本选择] 排序退化成字典序 → 应报「未取数值最大者」（否则下次 nvm 升级选错 node，firecrawl/agent 静默失效）",
     "未取数值最大者"),

    # --- [安全闸门] 把 prompt 批准的操作建议词加进禁词表（该段应抓）---
    # 2026-10-06 实测的文档/代码矛盾：`claude-plan.md` 曾把
    # 减仓/加仓/建仓/买入/卖出/赎回… 整列成绝对禁词，而 `analyze.py`
    # prompt 第 4 条明写「**但"减仓/加仓/建仓/观望/不追高"等操作建议词
    # 是允许的，请放心使用**」，真实推送里「减仓」已出现 25 次且全部 ok。
    # 本变异体模拟"照过时文档改坏闸门"：把「减仓」塞进 BANNED。
    ("advice-word-banned",
     "marketlib.py",
     ' "已无","没有仓","清仓","未持有","空仓","满仓","持仓",',
     ' "已无","没有仓","清仓","未持有","空仓","满仓","持仓","减仓",',
     "把 prompt 批准的操作建议词加进禁词 → [安全闸门] 应报「误拦」"),

    # --- [工作主档一致性] claude-plan.md 与代码漂移（该段应抓）---
    # claude-plan.md 是**给未来会话读的工作主档** —— 它写错，未来会话就照错的
    # 改代码。`[实测]` 2026-10-06 抓到两处长期存在的漂移（保护时段差 5 分钟、
    # 安全规则与 prompt 矛盾）。两个数据注入变异体分别覆盖两条判据。
    ("workdoc-guard-window-drift",
     "__DATA_WORKDOC_WINDOW__",
     "", "",
     "claude-plan.md 保护时段改回 14:55-15:00 → [工作主档一致性] 应报 ❌"),
    ("workdoc-advice-clause-removed",
     "__DATA_WORKDOC_ADVICE__",
     "", "",
     "claude-plan.md 删掉「操作建议词是允许的」声明 → 该段应报 ❌"),

    # --- [WebUI 应答] 面板进程在但服务不了（该段应抓）---
    # 「有进程」≠「能服务」：进程活着但卡死不响应时，launchctl 看它一切正常、
    # last exit code 是 0，而面板其实打不开。`[实测]` 2026-10-06 grep 全库：
    # selfcheck 对 `8787`/`127.0.0.1`/HTTP 探测**零命中** —— 面板可用性
    # 此前完全没有检查。
    # 注入：把 sandbox webui.py 的默认端口改成 8799 —— 回归侧把 8799
    # 显式映射到夹具的**必拒连**端口（见 run() 的 `MW_PROBE_FIXTURE`），
    # 于是这条测的是「端口没人监听」这个分支。
    # ⚠️ `must_contain` 不能省：夹具若退化（比如 8799 被映到挂死端口），
    # 报出的是 `TimeoutError` —— 变异体照样"抓住"（rc=1/❌=1），但
    # **归因分支被换掉了**，「拒连 → 查进程/端口」这条就没人测了。
    # 实测 2026-10-11 踩过：夹具用"绑而不 listen"实现必拒连端口，
    # macOS 上那会丢 SYN 挂到超时，两条变异体挤进同一分支而全绿。
    ("webui-port-dead",
     "webui.py",
     'PORT = int(os.environ.get("MW_PORT", "8787"))',
     'PORT = int(os.environ.get("MW_PORT", "8799"))',
     "面板端口指向无人监听的地址 → [WebUI 应答] 应报「无应答」",
     "ConnectionRefusedError"),

    # --- [WebUI 应答] 进程在听、但永不响应（该段应抓，且归因到「超时」）---
    # 与 webui-port-dead（拒连）是**两种不同故障**：那条是没人听（连接被
    # 立刻拒绝），这条是听了不回（死锁 / 被宿主负载压到无响应）。
    # 实测 2026-10-07 load1≈174 时生产面板就是这个形态：8 个端点全部超时，
    # 而 launchctl 看进程一切正常。旧判据把超时归因成「数据文件可能损坏」
    # —— 方向完全错（真因在宿主负载）。判据必须区分：超时 → 提示查负载；
    # 拒连 → 提示查进程/端口。fixture 见 apply_mutant。
    ("webui-hang",
     "webui.py",
     'PORT = int(os.environ.get("MW_PORT", "8787"))',
     'PORT = int(os.environ.get("MW_PORT", "8798"))',
     "面板进程在听但不响应 → [WebUI 应答] 应报「超时」且提示查宿主负载",
     "超时（8s+12s 两次）"),

    # --- 面板超时负载归因的两个方向（[面板超时归因] 应抓）---
    # 2026-10-18 新增：超时全部 + load1 ≥ 4×核数 → 判「未验证」而非 ❌。
    # 这条判据只在宿主被压到 4× 核数时成立，生产里一个月也撞不上几次 ——
    # 用构造值直接喂纯函数（`[面板超时归因]` 段）。
    # ① 阈值被删 → 任何超时都归因过载，面板真卡死也静默放行
    ("webui-overload-exempt-all",
     "selfcheck.py",
     '    return "overload" if load1 >= _WEBUI_OVERLOAD_FACTOR * ncpu else "fail"',
     '    return "overload"  # 注入：任何超时都豁免',
     "负载归因阈值被删 → [面板超时归因] 应报「全部超时 + 负载正常」判定错",
     "面板超时归因判据错"),
    # ② all() 退化成 any()：混合失败（超时 + HTTP 500）也被豁免
    ("webui-overload-mixed-allowed",
     "selfcheck.py",
     '    if not all("超时" in str(_b) for _b in bad):',
     '    if not any("超时" in str(_b) for _b in bad):  # 注入：混合失败也豁免',
     "归因判据 all→any → [面板超时归因] 应报「混合失败」判定错",
     "面板超时归因判据错"),

    # --- [执行流水新鲜度] 埋点静默停写（该段应抓）---
    # `runlog._append` 全吞异常（设计如此：埋点不能拖垮主链路）—— 磁盘满、
    # 权限变更、调用点被误删时会**静默停写**，面板逐渐空白，而此前
    # 没有任何检查项看 runs.jsonl 一眼。
    # 注入：runs.jsonl 末条改到 2 小时前，watch.log 保持新鲜（独立锚）。
    ("audit-trail-stops",
     "__DATA_AUDIT_TRAIL__",
     "", "",
     "runs.jsonl 停写而 watch.log 仍新 → [执行流水新鲜度] 应报「停写」"),

    # --- [执行流水新鲜度] 埋点写失败退化成静默（该段应抓）---
    # 真缺陷形态（2026-10-12 审查）：`runlog._append` 的失败留痕块若整体
    # 被 `except: pass` 包住，在"目录都不存在"（磁盘满/目录被删的典型）
    # 时会退化成**零痕迹** —— 返回 False 却既不写 `.appendfail` 也不上
    # stderr，流水静默丢失。本变异体把 stderr 告警那步删掉（留痕仍尝试
    # 落盘，但落不下时无声）。
    ("appendfail-silent",
     "runlog.py",
     '            print(f"[runlog] {path} 埋点写失败"\n'
     '                  f"（{type(_e_w).__name__}: {_e_w}）—— 该轮流水未落盘",\n'
     '                  file=_sys_w.stderr, flush=True)',
     '            pass  # 注入：写失败不再上 stderr',
     "[执行流水新鲜度] runlog 写失败必须留 stderr 告警（否则静默丢流水）",
     "写失败**无 stderr 告警**"),

    # --- [演练开关] 遗留的 DRY_RUN 静默吞掉全部推送（该段应抓）---
    # 全库唯一的「开关型」抑制点：`_dry_flag_active()` 命中就 return 在
    # push() 之前 —— 任务在跑、流水在写、面板在更新，只有**消息没发**。
    # 历史事故形态（记忆 [market-watch DRY_RUN 陷阱]）。
    # 注入：沙箱造一个**新鲜**的 DRY_RUN（mtime=现在）。
    ("dry-run-flag-left-on",
     "__DATA_DRY_FLAG__",
     "", "",
     "遗留的新鲜 DRY_RUN → [演练开关] 应报「推送正在被静默吞掉」"),

    # --- [调度方式] plist 指向错的脚本（该段应抓）---
    # 三种形态各有独立危害：
    #   ① 脚本不存在 → 任务反复起、反复失败（exit code 判据也能抓，但事后）
    #   ② 指向**别的**脚本 → **exit code 判据永远抓不到**：跑起来的是
    #      collect.py，它正常退出 0，而 marketwatch 该干的活一条没干
    #      （推送全停，launchctl 全绿）
    #   ③ 解释器不可执行 → 同上反复失败
    # 静态校验能在任务第一次失败**之前**报出来，且沙箱可覆盖
    # （读的是 MW_LAUNCHAGENTS 下的 plist，不碰宿主机 launchd）。
    ("sched-wrong-script",
     "__DATA_PLIST_WRONG_SCRIPT__",
     "", "",
     "marketwatch 的 plist 指向 collect.py → [调度方式] 应报「跑的是别的东西」"),

    # --- 启动竞态宽限：两个方向各一条（[启动竞态宽限] 应抓）---
    # 背景：自检 plist 2026-10-17 起 RunAtLoad=true（登录即跑一轮），而
    # collect 的 StartInterval 计时器**同样**从登录时刻起算 —— 重启后的头
    # 15 分钟里 pending.json 必然是上一交易日的。判据抽成纯函数
    # `_stale_verdict`，由 `[启动竞态宽限]` 用构造值验证（生产里这条分支
    # 沙箱不可达：沙箱的 boot_age 是真实值，跑一次就超窗）。
    # ① 宽限被整个删掉 → 每次重启都推一条"采集器可能没跑"假告警
    ("fresh-grace-removed",
     "selfcheck.py",
     '    if isinstance(boot_age, (int, float)) and boot_age < _FRESH_BOOT_GRACE:\n'
     '        return "grace"\n',
     '',
     "[启动竞态宽限] 应报「刚启动 60s → 宽限」判定错（宽限被删，重启即假告警）",
     "刚启动 60s"),
    # ② 读不到启动时长被当成"刚启动" → sysctl 一失效，所有陈旧采集静默放行
    ("fresh-grace-on-unknown",
     "selfcheck.py",
     '    if isinstance(boot_age, (int, float)) and boot_age < _FRESH_BOOT_GRACE:\n',
     '    if boot_age is None or boot_age < _FRESH_BOOT_GRACE:\n',
     "[启动竞态宽限] 应报「启动时长读不到 → 报警」判定错（fail-open 静默放行）",
     "启动时长读不到"),

    # --- [调度方式] 自检 job 的 RunAtLoad 被关掉（该段应抓）---
    # 真故障（2026-10-17 实测）：launchd 的 StartInterval 计时器**从 job
    # 加载时刻起算**，每次重启/登录都清零。自检是 86400s 的**一次性**任务，
    # 计时器被重启清零 = 当天整天不跑（runs=0、never exited、selfcheck.out
    # 停在前一天 17:42），而"自检没跑"没有任何信号。
    # 修法是 plist 加 RunAtLoad=true；本变异体把它翻回 false，验证该断言
    # 真的能抓（数据注入型：改沙箱 LaunchAgents 里的 plist，不碰宿主机）。
    ("sched-selfcheck-no-runatload",
     "__DATA_PLIST_SELFCHECK_RAL__",
     "", "",
     "selfcheck 的 plist 去掉 RunAtLoad → [调度方式] 应报「重启当天的自检会被静默吞掉」",
     "静默吞掉"),

    # --- [推送通道] webhook URL 结构错（该段应抓）---
    # `[实测]` 2026-10-06：路径少一个字母 / 整条只剩域名 / 换成 example.com
    # —— **四种全部通过**旧判据，因为它只 curl `https://{netloc}` 这个
    # **根路径**：根路径 403/200 都算"可达"，与 webhook 路径是否正确无关。
    # 危害：推送全失败（企微返回 errcode≠0 或非 JSON），而检查全绿 ——
    # 与 plist 指向错脚本同族：**"目标可达" ≠ "目标是对的"**。
    ("webhook-url-malformed",
     "__DATA_WEBHOOK_PATH__",
     "", "",
     "webhook 路径少一个字母 → [推送通道] 应报「URL 结构不对」"),

    # ============ 第四十七轮：--dry 演练轮在流水/面板里的可辨识性 ============

    # --- urgent 的 dry 分支把 status 写回 "ok"（[演练开关] 应抓）---
    # 真缺陷形态（2026-10-07 预演撞见）：`--dry` 一轮**一条消息都没发**，
    # 但流水落的是 `{"status":"ok","slot":"09:25","events":[...]}` —— 与真实
    # 推送成功轮**逐字段同形**（pushes.jsonl 里又没有记录）。面板的
    # run_status 拿到 "ok"，前端 rmap 译作「已执行」，而该槽位状态是 missed：
    # 读者看到「未推送 + 已执行」自相矛盾的一行，且无从知道是人工演练。
    ("dry-urgent-status-ok",
     "analyze.py",
     # ⚠⚠ 2026-11-13（复核，判别力修复）：原锚点把 `status` 与 `kind`
     # **两行一起**换掉，于是注入后 `kind` 也一并消失 → 除了本条要测的
     # 「status 写回 ok」，还**顺带**触发了「dry 轮 kind 不对」那条检查。
     # 结果是本条与 `dry-kind-overwritten-by-urgent` 报出**一字不差**的
     # 同一句 `dry 轮 kind='urgent'（应为 'dry'）`，两者的 must_contain
     # 也就无法区分 —— 实测把 A 的匹配串拿去套 B 的输出照样命中。
     # 这属于**变异体自己越界**：它只该改 status，不该顺手动 kind。
     # 改成只替换 status 那一行、保留 `kind="dry"`，两条从此各报各的。
     '            _r["status"]="skip_dry"',
     '            _r["status"]="ok"',
     "urgent 的 dry 分支写回 status=ok → [演练开关] 应报「与真实成功轮同形」"),

    # --- 通用 dry 分支把 status 写回 "ok"（[演练开关] 应抓）---
    ("dry-general-status-ok",
     "analyze.py",
     '        _r.setdefault("status","skip_dry")',
     '        _r.setdefault("status","ok")',
     "通用 dry 分支写回 status=ok → [演练开关] 应报「与真实成功轮同形」"),

    # --- dry 分支缺 kind="dry"（[演练开关] 应抓）---
    # 只有 skip_dry、没有 kind 时，面板的 kind 过滤（webui 读 runs 的
    # `r.get("kind")`）读不到 —— 演练轮只能靠 status 猜来源。
    ("dry-kind-missing",
     "analyze.py",
     '        _r["kind"]="dry"\n        _r["dry"]=True',
     '        _r["dry"]=True',
     "通用 dry 分支缺 kind=dry → [演练开关] 应报「缺 kind」"),

    # --- 面板不认 runs 的 skip_dry（[面板时段归因] 应抓）---
    # 真缺陷形态：`_run_dry` 恒为空集 → `--dry` 演练轮落进 missed，
    # 理由显示 run_status 原值。判据是行为测试（造 skip_dry 的 runs 行 +
    # 无 pushes 记录，断言槽位必须是 skipped 且理由含 '--dry'）。
    ("panel-ignores-run-dry",
     "webui.py",
     '    _run_dry: set[str] = {k for k, v in run_status.items() if v == "skip_dry"}',
     '    _run_dry: set[str] = set()   # 注入：不认 runs 的 skip_dry',
     "面板不认 runs 的 skip_dry → [面板时段归因] 应报「演练轮落进 missed」"),

    # --- rmap 把演练文案挂回 "ok" 下（[面板时段归因] 的 rmap 锚应抓）---
    # 历史错位形态（预演实测）：`ok:"DRY_RUN 标志生效…"` 而 skip_dry 键
    # **根本不存在** —— 前端 `rmap[reason] || reason` 于是把真 skip_dry
    # 裸显成英文枚举，而 "ok" 显示成"人工暂停"。两个键的文案互换后，
    # 契约锚（skip_dry 与 ok 文案必须不同）应报出来。
    ("rmap-dry-misplaced",
     "webui.py",
     '              not_trading_day:"非交易日",error:"执行出错",ok:"已执行",\n'
     '              skip_dry:"--dry / DRY_RUN 演练，人工暂停（非故障）",',
     '              not_trading_day:"非交易日",error:"执行出错",\n'
     '              ok:"--dry / DRY_RUN 演练，人工暂停（非故障）",\n'
     '              skip_dry:"已执行",',
     "rmap 把演练文案挂到 ok 下、skip_dry 挂成别的 → [面板时段归因] 应报"),

    # --- `?date=` 非法值退化成"不过滤"（[面板时段归因] 应抓）---
    # 真缺陷形态（2026-10-12 复查实测）：`_q_date` 校验失败返回 None，
    # 而 None 在 /api/runs、/api/pushes 里是"不过滤"信号 —— 于是
    # `?date=xyz` 返回**全部历史行**（实测 800 行），比原先"返回空结果"
    # 更糟（空结果不冒充别的日期）。本变异体把校验失败改回返回值。
    ("date-invalid-means-unfiltered",
     "webui.py",
     "        if datetime.date.fromisoformat(_d).isoformat() != _d:\n"
     "            raise ValueError(_d)\n"
     "    except Exception:\n"
     "        raise BadDate(_d)\n"
     "    return _d",
     "        if datetime.date.fromisoformat(_d).isoformat() != _d:\n"
     "            raise ValueError(_d)\n"
     "    except Exception:\n"
     "        return None  # 注入：非法日期退化成「不过滤」\n"
     "    return _d",
     "[面板时段归因] ?date= 非法值必须抛 BadDate（不得静默当\"不过滤\"返回全量）",
     "_q_date 放行了非法日期"),

    # ============ 面板板块榜 /api/boards 形状守卫（2026-10-12）============
    # --- chg 守卫去掉 bool 排除（[面板板块榜] 应抓）---
    # `isinstance(True, int)` 为真 —— 少了 `or isinstance(_ch, bool)` 这半句，
    # 布尔值会被当前端数字渲染（`true.toFixed(2)` → 崩或显示怪值），
    # 且 `True` 会当成 +1.00% 混进涨幅榜。
    ("boards-chg-bool-passes",
     "webui.py",
     "                    if not isinstance(_ch, (int, float)) or isinstance(_ch, bool):\n"
     "                        continue\n",
     "                    if not isinstance(_ch, (int, float)):\n"
     "                        continue\n",
     "[面板板块榜] chg 是 bool 必须被剔除（不得当 +1.00% 混进榜）",
     "坏输入「chg 是 bool」的 top_up 存活行"),

    # --- nan/inf 守卫去掉（[面板板块榜] 应抓）---
    # nan/inf 不是 bool、是合法 float，前一关放行；少了这半句前端
    # `toFixed` 出 "NaN%"/"Infinity%"，且 `_ch != _ch`（NaN 自比）是
    # 唯一能识别 NaN 的判据。
    ("boards-chg-nan-passes",
     "webui.py",
     '                    if _ch != _ch or _ch in (float("inf"), float("-inf")):\n'
     "                        continue\n",
     "                    if False:\n"
     "                        continue\n",
     "[面板板块榜] chg 是 nan/inf 必须被剔除（不得渲染成 NaN%/Infinity%）",
     "坏输入「chg 是 nan」的 top_up 存活行"),

    # --- name 空串守卫去掉（[面板板块榜] 应抓）---
    # --- 空数据时整段静默消失（[涨跌榜] 应抓）---
    # 2026-10-13 09:19 生产实测：hot 候选池空 → top_up/top_dn 都是 []，
    # `_chg_board` 返回 []，两段在 brief 里**静默消失**、零痕迹，推送就
    # "缺了两章"。修法是空时返回段头 + 「（本段无数据）」；本变异体把
    # 那个兜底删掉，检查项必须抓到。
    ("board-empty-marker-gone",
     "analyze.py",
     '        return [f"【{title}】{tag}", "  " * ind + "（本段无数据）"]',
     '        return []  # 注入：空数据时整段静默消失',
     "[涨跌榜] 空数据（候选池空）时两段必须带「本段无数据」标注，不许静默消失",
     "段静默消失（应渲染段头+本段无数据）"),

    # --- 形状坏测试写生产日志（[涨跌榜] 应抓）---
    # 2026-10-13 实测：[涨跌榜] ⑤ 坏形状用例喂非空非 list 时，
    # `_chg_board` 会 `_log("形状异常…")`，selfcheck 在生产目录跑 →
    # 两行假日志进了生产 watch.log。修法是 ⑤ 循环期间桩掉 analyze._log；
    # 本变异体删掉那个桩，检查项（源级锚）必须抓到。
    ("boards-prodlog-stub-gone",
     "selfcheck.py",
     '        _save_log_cb = _A_cb._log\n'
     '        _A_cb._log = lambda *a, **k: None\n',
     '        _save_log_cb = _A_cb._log\n',
     "[涨跌榜] 形状坏用例期间必须桩掉 analyze._log（否则污染生产 watch.log）",
     "形状坏用例缺 analyze._log 桩"),

    ("boards-name-empty-passes",
     "webui.py",
     "                    if not isinstance(_nm, str) or not _nm:\n"
     "                        continue\n",
     "                    if not isinstance(_nm, str):\n"
     "                        continue\n",
     "[面板板块榜] 空板块名必须被剔除（不得渲染出无名行）",
     "坏输入「name 是空串」的 top_up 存活行"),

    # --- rows 容器守卫去掉（[面板板块榜] 应抓）---
    # 去掉 `isinstance(_v, list)` 后，`_v` 是 dict/str 时 `_v[:8]` 会取到
    # 键切片或截断串、`for _r in ...` 迭代出非 dict → 整个端点抛，面板 500。
    ("boards-rows-container-guard-gone",
     "webui.py",
     "                _v = _d.get(_k)\n"
     "                if not isinstance(_v, list):\n"
     "                    continue\n",
     "                _v = _d.get(_k)\n"
     "                if _v is None:\n"
     "                    continue\n",
     "[面板板块榜] rows 是 dict/str 时不得抛（容器守卫必须逐键判 list）",
     "坏输入「top_up 是 dict（top_dn 须存活）」的 top_dn 存活行"),

    # ============ 第四十八轮：--dry 轮的**状态零写入**契约 ============

    # --- urgent 分支尾部无条件覆盖 kind（[演练开关] 行为判据应抓）---
    # 真缺陷形态（2026-10-07 实测）：`_r["kind"]="urgent"` 无条件执行，
    # 把 dry 子分支刚设的 `kind="dry"` 覆盖掉 —— 源码里两句话都在，
    # **源码正则查不出覆盖关系**，只有行为测试能抓。
    # 修复前实测：dry=True 时 run() 返回 {status: skip_dry, kind: urgent}。
    ("dry-kind-overwritten-by-urgent",
     "analyze.py",
     '        _r.setdefault("kind", "urgent")',
     '        _r["kind"]="urgent"',
     "urgent 尾部无条件覆盖 dry 的 kind → [演练开关] 行为判据应报"),

    # --- dry 轮照常认领 inflight 并落盘（[演练开关] 行为判据应抓）---
    # 真缺陷形态：`_run_after_inflight` 之前的预认领块不区分 dry，把
    # hits/urgent 的键写进 inflight 并 save_st。进程若死在"认领落盘"与
    # "dry 回滚落盘"之间，键留到 8h 后被 load_st 转正到 fired，
    # 该事件当天静默（消息从未发出）。修复前实测 save 序列 [['K'],[],[]]。
    ("dry-claims-inflight",
     "analyze.py",
     # 锚点随 2026-11-08 的「写入去重」同步：原锚点那两行之间现在夹了说明段。
     # 用「`if not dry:` + 紧跟的去重说明段首行」作锚（全库唯一），
     # 只把闸门改成 `if True:` —— 仍对应「dry 轮照常认领」这个真实缺陷。
     '    if not dry:\n        # ⚠️ 2026-11-08（复核）：**写入去重**。',
     '    if True:\n        # ⚠️ 2026-11-08（复核）：**写入去重**。',
     "dry 轮照常认领 inflight → [演练开关] 行为判据应报「向 state 落非空内容」"),

    # --- dry 轮推进真实防抖闸门（[演练开关] 行为判据应抓）---
    # 真缺陷形态：删掉 pending_idx 还原，dry 轮把 should_alert 写的
    # pending 落盘 —— 下一次真实轮次会被误判成"持续确认通过"提前推送，
    # 反向也会清掉真实轮次刚记的 pending、推迟一次本应发生的推送。
    ("dry-advances-pending",
     "analyze.py",
     '                if dry:\n                    st["pending_idx"] = _pend0',
     '                if False:\n                    st["pending_idx"] = _pend0',
     "dry 轮推进防抖闸门 pending_idx → [演练开关] 行为判据应报"),

    # --- dry 轮在 hourly 认领块写状态（[演练开关] 行为判据应抓）---
    # 真缺陷形态（2026-10-07 预演**复跑**才撞出 —— 单跑第一次没暴露，
    # 因为第一版探针没让 hourly 命中）：`_run_after_inflight` 里的
    # `if hourly:` 块不区分 dry，把 hourly_<label> 写进 inflight、
    # 写 hourly_retry 并 save_st。state.json 里留下 `hourly_retry` 与
    # `inflight_ts` 就是证据。危害同"认领落盘后进程被杀"（槽位当天静默），
    # 外加一张**本不该有**的宽限凭据。
    ("dry-hourly-claims",
     "analyze.py",
     '        if not dry:\n            st.setdefault("inflight", []).append("hourly_"+hourly)',
     '        if True:\n            st.setdefault("inflight", []).append("hourly_"+hourly)',
     "dry 轮在 hourly 认领块写状态 → [演练开关] 行为判据应报"),

    # --- dry 轮在 deadline 让位块写宽限凭据（[演练开关] 行为判据应抓）---
    # 同族第三个入口：让位时写 hourly_retry 并落盘。dry 不推送却留下凭据，
    # 后续轮次会凭它在窗口过期后补推。
    ("dry-yield-leaves-retry",
     "analyze.py",
     '            if not dry:\n                st.setdefault("hourly_retry", {})[hourly] = time.time()',
     '            if True:\n                st.setdefault("hourly_retry", {})[hourly] = time.time()',
     "dry 轮在让位块留宽限凭据 → [演练开关] 行为判据应报"),

    # ============ 第五十轮：dry 的「删除型」写入 ============
    # 第四十八轮把 dry 的"跳过认领"当成了"回滚是空操作"，于是两处 dry
    # 分支仍各写一次 `inflight=[]`。但 dry 不认领时，内存里的 inflight
    # 是 load_st() 读出的**盘上原有内容**（崩溃轮留下的去重凭据），
    # 置 [] 落盘 = 销毁别人的凭据。实测（2026-10-08，MW_DIR 沙箱，
    # 注入 inflight=['crash_k'] + 新鲜 inflight_ts 跑 --dry）：
    # state.json 的 inflight 被抹成 []，而 [演练开关] 当时全绿 ——
    # 旧判据只查"有没有新增"，测不到"减少"。

    # --- 通用 dry 分支：回滚写盘（[演练开关] 应抓）---
    # 注入：恢复第四十八轮删掉的那次 `_dry_snap` 回滚写盘。
    ("dry-writes-empty-inflight",
     "analyze.py",
     '        _r.setdefault("status","skip_dry")\n'
     '        # ⚠️ 与上面 urgent 的 dry 分支同理',
     '        _dry_snap = dict(st)\n'
     '        _dry_snap["inflight"] = []\n'
     '        if save_st(_dry_snap):\n'
     '            st["inflight"] = []\n'
     '        else:\n'
     '            _log("dry 回滚 inflight 落盘失败 —— 保留 inflight 待 finally 补救", "ERR")\n'
     '        _r.setdefault("status","skip_dry")\n'
     '        # ⚠️ 与上面 urgent 的 dry 分支同理',
     "dry 通用分支把盘上原有 inflight 抹成 [] → [演练开关] 应报「销毁去重凭据」"),

    # --- urgent dry 分支：回滚写盘（[演练开关] 应抓）---
    # 注入：恢复 urgent 子分支的那次 `_dry_snap` 回滚写盘。
    ("dry-urgent-writes-empty-inflight",
     "analyze.py",
     '        elif _push_urgent_or_unknown():',
     '            _dry_snap = dict(st)\n'
     '            _dry_snap["inflight"] = []\n'
     '            if save_st(_dry_snap):\n'
     '                st["inflight"] = []\n'
     '            else:\n'
     '                _log("dry 回滚 inflight 落盘失败 —— 保留 inflight 待 finally 补救", "ERR")\n'
     '        elif _push_urgent_or_unknown():',
     "dry urgent 分支把盘上原有 inflight 抹成 [] → [演练开关] 应报「销毁去重凭据」"),

    # --- run() 的 finally：对 dry 免疫（[演练开关] 应抓）---
    # 注入：把 finally 的 dry 守卫置为恒假，恢复"异常退出，回滚 inflight"。
    # 这是**第二次**销毁机会（两处 dry 分支是第一次）。
    # ⚠️ 必须用 `if False:` 而不是整段删除 —— 删掉 `if dry:` 会让后面的
    # `elif _converged[0]:` 悬空，整份 analyze.py 语法错误、所有段一起报 ❌
    # （实测第一版：105 个 ❌ 横跨 15 个段，"抓住"了但指向别处，零判别力）。
    # 保持语法有效，才测得到目标分支本身。
    #
    # ⚠️ `must_contain` 是「**多余落盘**」而不是「销毁凭据」—— 第五十一轮
    # 的 `_kept/_mine` 修复后，finally 即使没有 dry 守卫也**不再销毁**
    # crash_k（快照内的键被保留，`[实测]` 复刻两版逻辑：第 50 轮终态 []、
    # 第 51 轮终态 ['crash_k']）。也就是说本变异体现在只剩一个可观测后果：
    # dry 轮**多写了一次 state**（零写入契约）。锚点写「销毁凭据」会永远
    # 匹配不上，把一条真判据误判成漏网（run9 就是这么漏的）。
    # 「销毁」那一路仍有判别力 —— 由 `finally-clears-leftover`（把 `_kept`
    # 置 []）单独覆盖，两条各测一半，不能互相顶替。
    ("dry-finally-rolls-inflight",
     "analyze.py",
     '        if dry:\n            pass                    # 演练轮：一个字节都不写',
     '        if False:\n            pass                    # 注入：dry 守卫失效',
     "dry 轮的 finally 仍落盘（零写入契约）→ [演练开关] 应报「dry 轮仍写了 state」",
     "dry 轮仍写了 state"),

    # --- urgent 分支公共尾部的 save_st：对 dry 免疫（[演练开关] 应抓）---
    # 这是**第四个** dry 写入入口，也是最隐蔽的一个：它不在 `if not dry`
    # 里，而是 `if urgent:` 块的**公共尾部** —— dry 子分支只设了 `_r` 字段、
    # **没有 return**，控制流会落下来执行这次落盘。
    # 实测（2026-10-08，自检 `[演练开关]` ③ 场景，注入 inflight=['crash_k']
    # 后跑 dry）：落盘 1 次 `{inflight: ['crash_k'], pending: [], retry: {}}`。
    # 内容看着"没变"，但那是因为夹具里没有并发；真实场景下这一写会把
    # `inflight_ts` 一并刷新，让本该 8h 后转正的崩溃凭据被**无限续期**。
    # 注入：去掉 `not dry` 守卫，恢复无条件落盘。
    ("dry-urgent-tail-saves",
     "analyze.py",
     '        if not dry and not save_st(st):',
     '        if not save_st(st):',
     "dry 轮的 urgent 公共尾部仍落盘 → [演练开关] 应报「零写入契约被破坏」",
     "dry 轮仍写了 state"),

    # ============ 第五十一轮：失败回滚的范围（非 dry） ============
    # 第五十轮修的是"dry 轮不该写"；这一族是它的镜像 —— 非 dry 轮**失败时
    # 怎么回滚**。回滚的语义是"撤销本轮认领"，不是"清空 inflight"：
    # 盘上可能挂着上一轮崩溃遗留的键（消息可能已发出、_commit 未落盘），
    # 那是唯一的去重依据，清掉它 → 下一轮 detect() 的 busy 不再含该键
    # → 同一条异动重复推送。
    # 实测（2026-10-08，MW_DIR 沙箱，注入 inflight=['crash_k']）：
    # 修复前三条回滚路径（_rollback / urgent 失败分支 / run() 的 finally）
    # 的终态全是 inflight=[]。

    # --- _rollback 整体置 []（[演练开关] ⑥ 应抓）---
    ("rollback-clears-leftover",
     "analyze.py",
     '        st["inflight"] = [k for k in st.get("inflight", [])\n'
     '                          if k in pre_inflight]',
     '        st["inflight"] = []',
     "通用推送失败的回滚清空 inflight（销毁崩溃遗留键）→ [演练开关] 应报",
     "通用推送失败的回滚销毁了盘上原有 inflight"),

    # --- urgent 失败分支整体置 []（[演练开关] ⑥b 应抓）---
    ("urgent-fail-clears-leftover",
     "analyze.py",
     '            st["inflight"] = [k for k in st.get("inflight", [])\n'
     '                              if k in pre_inflight]',
     '            st["inflight"] = []',
     "urgent 失败的回滚清空 inflight（销毁崩溃遗留键）→ [演练开关] 应报",
     "urgent 推送失败的回滚销毁了盘上原有 inflight"),

    # --- run() 的 finally 整体置 []（[演练开关] ⑥c 应抓）---
    ("finally-clears-leftover",
     "analyze.py",
     '            _kept = [k for k in st["inflight"] if k in _pre_inflight]',
     '            _kept = []',
     "异常退出的回滚清空 inflight（销毁崩溃遗留键）→ [演练开关] 应报",
     "异常退出的回滚销毁了盘上原有 inflight"),

    # ============ 第四十九轮：analyze 侧的港股盘前闸门 ============

    # --- analyze.detect 去掉港股 09:30 闸门（[窗口上界自洽] 应抓）---
    # 真缺陷形态（2026-10-07 预演实测）：`hm < TH["session"][0]`（09:25）
    # 挡不住 09:26~09:29 —— 港股 09:30 才开盘，那段 f2 是昨收，
    # detect 判出 hstech_down。与 collect 侧已修的缺陷是**同一个闸门
    # 修了一半**（collect 修了、analyze 漏了，自检当时只测 collect）。
    # 注入：把闸门条件恒真，恢复到修复前行为。
    ("hk-gate-missing-analyze",
     "analyze.py",
     '        if _hk_live:',
     '        if True:',
     "[窗口上界自洽] 应报 analyze 在 09:29 判出 hstech（昨收当行情）"),

    # --- 闸门误伤：整体清零 hits（[窗口上界自洽] 反向判据应抓）---
    # 另一种改法绕开：把闸门写成"盘前直接 return []"—— 挡掉了 hstech，
    # 但也误杀了 A股 09:26~09:29 的 watch_* 有效异动。
    # 注入：在 hstech 判定前加整段早退。
    ("hk-gate-overkill-analyze",
     "analyze.py",
     '    _hk_live = M.hk_session_open(now)',
     '    _hk_live = M.hk_session_open(now)\n'
     '    if hm < 9 * 60 + 30: return []   # 注入：整体清零',
     "[窗口上界自洽] 应报「闸门把 A股板块信号也挡掉了」"),

    # --- premarket_tags 只认 09:25 槽位（[窗口上界自洽] 应抓）---
    # 真缺陷形态（2026-10-07 预演实测）：`if slot != "09:25": return "", ""`
    # 让**事件类**推送（slot=None/"event"）在 09:25~09:29 拿到空标注 ——
    # 那段港股 f3 恒为 0.0（未开盘），正文里的「恒生科技指数: 4185.63
    # (+0.00%)」被读者当成"今天港股平盘"。
    # 注入：恢复成只认 09:25 槽位。
    ("premarket-tag-slot-only",
     "analyze.py",
     '    if slot != "09:25" and _hm >= 9 * 60 + 30:\n        return "", ""',
     '    if slot != "09:25":\n        return "", ""',
     "[窗口上界自洽] 应报「事件类 09:26 无标注」"),

    # --- premarket_tags 对事件类永久带标注（[窗口上界自洽] 反向判据应抓）---
    # 另一种改法绕开：干脆把 `slot != "09:25"` 判据整条删掉 —— 事件类在
    # **盘中**也带「（盘中价）」标注，是纯噪音（且「（盘中价）」这个标注
    # 对读者毫无信息量）。
    ("premarket-tag-always",
     "analyze.py",
     '    if slot != "09:25" and _hm >= 9 * 60 + 30:\n        return "", ""',
     '    pass   # 注入：永不早退',
     "[窗口上界自洽] 应报「事件类 09:31/10:00 仍带标注」"),

    # --- premarket_tags 缺收盘侧标注（[窗口上界自洽] 应抓）---
    # 真缺陷形态（2026-10-20 实测）：A股 15:00 收盘、港股 16:00 收盘，
    # 15:00 槽位那轮采集止于 15:05 —— 推送里的恒生/恒生科技是盘中值。
    # 缺标注时 AI 写「恒生科技收 4057（-1.25%）」，真实收盘 4045.91
    # （-1.52%）；10-15 15:05「恒科收盘破位」同款。
    # 注入：收盘侧分支整条失效（恒真判据 → if False）。
    ("hk-close-tag-dropped",
     "analyze.py",
     '        _hk_open = M.hk_session_open(now)\n',
     '        _hk_open = False   # 注入：收盘侧港股标注整体失效\n',
     "[窗口上界自洽] 应报「15:12 标注」缺失 —— 港股盘中值被写成「收」",
     "15:12 标注"),

    # --- 15:00 港股「收」措辞观测被删（[结构闸门] 源级锚应抓）---
    # 2026-10-20 加：prompt 的 rule 明令港股不得写「收」，代码侧必须留痕
    # 否则规则被无视时零信号（实测 15:12 写「恒生科技收 4057」）。
    ("hk-close-wording-obs-dropped",
     "analyze.py",
     '        _log("港股措辞疑似把盘中值写成「收」（本轮港股未收盘，"\n'
     '             "rule 要求写「截至 15:0x」或标注盘中）")',
     '        pass   # 注入：15:00 港股「收」措辞不再留痕',
     "[结构闸门] produce_text 必须对 15:00 港股「收」措辞留痕"
     "（prompt 规则无代码侧观测 = 被无视时零信号）",
     "未对 15:00 港股「收」措辞留痕"),
    # ============ 第五十二轮：2026-10-08 全天推送复核的 5 项缺陷 ============

    ("snapshot-hk-open-boundary-off", "analyze.py",
     '''        if _now_min >= 9 * 60 + 30 and _snap_min < 9 * 60 + 30:
            return None''',
     '''        if False:
            return None''',
     "[快照复用] 应报「跨港股开盘的盘前快照仍被复用」"),

    ("fund-missing-rule-removed", "analyze.py",
     '''     · 板块行里**没有**「主力净」字样 = 该板块本轮没有资金数据
       （盘前最常见：主力资金要开盘后才产生）→ 只写涨跌幅
       （如 `半导体 -0.20%`），**不要**写「资金方向未提供」「主力方向
       未给出，暂不判定」这类占位语 —— 它们不是数据、不提供任何信息，
       还占篇幅（实测 2026-10-08 盘前 8 行全是占位语）。''',
     '''     · 板块行里没有资金数据时，只写涨跌幅即可。''',
     "[缺失数据的 AI 例外规则] 应报「未覆盖资金字段缺失」"),

    ("daylow-term-rule-removed", "analyze.py",
     '''      ⚠️ **位置必须用 brief 的原词**：【关键位】段给的是 **20 日**高/低，
      写距离就写「距 20 日低 +2.02%」「20 日低 4111」，**绝不能**写成
      「日内低点」「日内高点」——「日内」高低只存在于【存储芯片日内】
      那一行与「触发」段的描述里（如「自日内高点回落 1.82%」）。
      两种参照混用会让读者把"离 20 日低还有 2%"误读成"离今天最低点
      还有 2%"，据此做交易决策（实测 2026-10-08 两处写成「距日内低点」）。''',
     '''      （位置表述沿用 brief 原词即可。）''',
     "[位置术语] 应报「prompt 未写明 20 日/日内边界」"),

    ("hit-msg-subject-dropped", "analyze.py",
     '''            if v3 is not None and v3<=dr: hits.append(("hstech_drop", f"恒生科技{v3:+.2f}%"))''',
     '''            if v3 is not None and v3<=dr: hits.append(("hstech_drop", f"跌{v3:.2f}%"))''',
     "[命中消息主语] 应报 hstech_drop 消息无主语"),

    ("collect-hits-log-undeduped", "collect.py",
     '''    M._daily_log_once("collect", "hits:" + ",".join(_keys),
                      f"触发 {_keys}（本日该组合首次，相同组合不再记）")''',
     '''    _log(f"触发 {_keys}")   # 注入：去重被移除''',
     "[跨进程日志去重] 应报 collect._main 三次触发写 3 行日志"),

    ("collect-hits-callsite-bypass", "collect.py",
     '''    if ev: _log_events_once(ev)''',
     '''    if ev: _log(f"触发 {[e['k'] for e in ev]}")   # 注入：调用点绕过去重''',
     "[跨进程日志去重] 应报「调用点未走去重」"),

    # --- 死代码：把已删的零引用定义注入回去（[死代码] 应抓）---
    # 2026-10-09 删除的 5 个符号之一（marketlib.push_secids）。注入方向 =
    # 「有人把死代码加回来」—— 正是本检查项要防的回归。
    # ⚠️ 带 must_contain：否则 baseline 里任何杂散 ❌（百度风控抖动、
    # 沙箱 webui 端点时序）都会让本变异体假「抓住」。
    ("dead-code-restore-push-secids",
     "marketlib.py",
     "def index_secids():",
     'def push_secids():\n'
     '    return [_sid(_c) for _seg, x in _cfg_items("push")\n'
     '            if (_c := _cfg_id(x, "code"))]\n\n'
     "def index_secids():",
     "[死代码] 应报「push_secids 无任何引用」",
     "顶层 push_secids() 无任何引用"),

    # 同上，feeds 侧：注入回一个**无调用方**的降级函数（体内不引用已删的
    # claude_fallback，避免 NameError 掩盖判据）。代表「预设源全挂→唤起
    # 真 Claude 找源」那条危险链被重新引入。
    ("dead-code-restore-ensure-source",
     "feeds.py",
     "def health():",
     "def ensure_source(force=False):\n"
     "    # 注入：无调用方的最后一环（原实现会唤起真 Claude，无总时长上限）\n"
     "    return None\n\n"
     "def health():",
     "[死代码] 应报「ensure_source 无任何引用」",
     "顶层 ensure_source() 无任何引用"),

    # --- [死代码] 零引用的**模块级常量**（该段应抓）---
    # 2026-10-11：判据原先只扫 def/class，于是 `collect.LOG`（指向一个真实
    # 存在、但无人写的 collect.log）漏了 —— 这种"定义了却没人用"的路径常量
    # 最坑：文件真实存在，后来者会以为那是活路径，去翻一个从不被写的日志。
    # 判据已扩到模块级常量（含双向常量锚点），这条变异体守的就是那一半。
    ("dead-code-const-unreferenced",
     "collect.py",
     "OUT = p_state(\"pending.json\")\n"
     "# ⚠️ 这里原先还有一个 `LOG = p_log(\"collect.log\")`，2026-10-11 删除：\n"
     "# 全库零引用（AST 核实），而它指向的文件**真实存在**（510B，10-08 16:01 那次\n"
     "# marketlib 语法错误的留痕）—— 一个\"定义了但没人写\"的路径常量，只会让后来者\n"
     "# 以为 collect.log 是活路径。真正的日志统一走 watch.log（见 _log 的注释）。\n",
     "OUT = p_state(\"pending.json\")\n"
     "DEAD_LOG = p_log(\"collect.log\")\n"
     "# ⚠️ 这里原先还有一个 `LOG = p_log(\"collect.log\")`，2026-10-11 删除：\n"
     "# 全库零引用（AST 核实），而它指向的文件**真实存在**（510B，10-08 16:01 那次\n"
     "# marketlib 语法错误的留痕）—— 一个\"定义了但没人写\"的路径常量，只会让后来者\n"
     "# 以为 collect.log 是活路径。真正的日志统一走 watch.log（见 _log 的注释）。\n",
     "[死代码] 应报「顶层常量 DEAD_LOG 无任何引用」",
     "顶层常量 DEAD_LOG 无任何引用"),

    # --- [AI 通道] AI 元说明块泄漏（该段应抓）---
    # 真缺陷形态：AI 在正文后附「写作说明（不属于推送正文）」+ 会话环境自述，
    # 整段进群。`[实测]` 2026-10-09 13:23:42 事件推送的**生产原文**就是这个
    # 形态（prompt 规则 1/2 明禁，模型仍违约）。判据两处：_strip_meta 的
    # 用例 + ai_analyze 的接线断言。下面两个变异体分别打这两处。
    ("ai-meta-block-not-stripped",
     "analyze.py",
     # ⚠️ 2026-10-27：锚点跟着新加的非 str 守卫改（拆成两条 if）。
     "    if not any(_mk in t for _mk in _META_MARKS):\n"
     "        return t",
     "    return t  # 注入：不再剥元说明块",
     "[AI 通道] 元说明块应被截除",
     "元说明块处理错误"),
    # ⚠️ 2026-10-13 改：strip 三层调用挪进 ai_analyze 内嵌的 `_invoke`
    # （备用模型链），锚点跟到新位置 —— 判据不变（AST 接线断言）。
    ("ai-meta-strip-not-wired",
     "analyze.py",
     '            _strip_meta(_strip_think(_strip_fence((_so or "").strip()))))',
     '            _strip_think(_strip_fence((_so or "").strip())))  # 注入：忘了接 _strip_meta',
     "[AI 通道] ai_analyze 未接 _strip_meta 应被接线断言抓到",
     "ai_analyze 未调用"),
    # 2026-10-12 实测真缺陷（自查二轮新修复时发现）：`_FENCE_RE` 原写
    # `^\s*(...)\s*`，`\s` 含换行且贪婪 —— 标记行连同**下一行正文（往往是
    # 标题）**一起被剥。自检此前对围栏零覆盖，该缺陷由人工围栏包裹复现
    # 发现；现补 [AI 通道] 用例 + 本变异体（正则退回 `\s*` 旧版）双重锁住。
    ("fence-eats-next-line",
     "analyze.py",
     '_FENCE_RE = re.compile(r"^[ \\t]*(```+|~~~+)[ \\t]*[^\\n]*$", re.M)',
     '_FENCE_RE = re.compile(r"^\\s*(```+|~~~+)\\s*[^\\n]*$", re.M)  # 注入：\\s 吃掉下一行',
     "[AI 通道] 围栏标记行不得连带剥掉下一行正文（包裹型标题必须存活）",
     "围栏处理错误"),
    # 与 `ai-meta-strip-not-wired` 同一条接线断言、打另一个名字：
    # 断言清单加了 `_strip_fence` 后，必须有一条变异体证明**那一项**是活的
    # （否则又是"用例与实现同源"——清单里列了却没有任何变异体验证它会报）。
    ("ai-fence-strip-not-wired",
     "analyze.py",
     '            _strip_meta(_strip_think(_strip_fence((_so or "").strip()))))',
     '            _strip_meta(_strip_think((_so or "").strip())))  # 注入：忘了接 _strip_fence',
     "[AI 通道] ai_analyze 未接 _strip_fence 应被接线断言抓到",
     "ai_analyze 未调用"),

    # --- 涨跌榜盘前不标注（[涨跌榜] 应抓）---
    # 真缺陷形态（2026-10-12 自查发现）：clist 的 f3 在盘前就是**上一交易日**
    # 涨跌幅，而涨跌榜段头写「今日涨幅 TOP3」却不带标注 —— 读者会把昨收
    # 当成"今天涨这么多"。与 [[market-watch-premarket-tag-coverage]] 同型。
    ("board-no-premarket-tag",
     "analyze.py",
     '    return [f"【{title}】{tag}"] + _rows',
     '    return [f"【{title}】"] + _rows  # 注入：涨跌榜丢掉盘前标注',
     "[涨跌榜] 盘前涨跌榜段头必须带「（盘前参考价）」标注",
     "盘前涨跌榜段头缺标注"),

    # ============ 涨跌榜 TOP3（2026-10-12 加）============
    # --- 涨跌榜段被塞进 `if _strong:` 块内（[涨跌榜] 应抓）---
    # 真缺陷形态（2026-10-12 沙箱自检抓到）：第一版把涨跌榜渲染写在
    # `if _strong:` 块**内**，于是 strong 为空（去重/过滤后无候选）时
    # 涨跌榜跟着消失 —— 两者本是独立数据源（strong=算法打分榜，
    # top_up/top_dn=纯涨跌幅榜）。生产上"碰巧"没暴露只因当天 strong 非空。
    ("board-inside-strong-guard",
     "analyze.py",
     '        if isinstance(hot.get("top_up"), list) or isinstance(hot.get("top_dn"), list):\n'
     '            _bu = _chg_board(hot, "top_up", "今日涨幅 TOP3", _a_tag)',
     '        if False:\n'
     '            _bu = _chg_board(hot, "top_up", "今日涨幅 TOP3", _a_tag)',
     "[涨跌榜] strong 为空时涨跌榜仍须渲染（两榜是独立数据源）",
     "涨跌榜段缺失"),

    # --- 涨跌榜排序方向反了（[涨跌榜] 应抓）---
    # 读者会以为"涨幅第一名"是涨得最少的。判据是 [涨跌榜] 的方向断言。
    ("board-sort-reversed",
     "hot.py",
     "    top_up = list(reversed(_by_chg[-3:]))",
     "    top_up = _by_chg[:3]  # 注入：涨幅榜取最小的三个",
     "[涨跌榜] 涨幅榜必须降序（涨幅最大排第一）",
     "缺涨跌榜排序式"),

    # --- 兜底路径丢涨跌榜段（[涨跌榜] 应抓）---
    # 真缺陷形态：只给 AI 路径加段、忘了兜底路径 —— AI 一超时读者就少一段
    # （同 memory market-watch-fallback-path-parity 的教训）。
    ("board-fallback-missing",
     "analyze.py",
     '            _bl = _chg_board(hot, _k2, f"今日{_t2} TOP3", _pre_tag)\n'
     '            if _bl:',
     '            _bl = []\n'
     '            if _bl:',
     "[涨跌榜] 兜底模板必须也带涨跌榜段（AI 超时那轮）",
     "兜底模板缺涨跌榜段"),

    # --- 热点缓存 schema 守卫漏查 top_up/top_dn（[涨跌榜] 应抓）---
    # 旧版本 hot_cache 没有这两个键，命中后 build_brief 取到 None → 涨跌榜
    # 静默消失（且旧缓存 30 分钟才过期）。与 "strong" 缺键同型。
    ("hot-cache-schema-guard-narrow",
     "hot.py",
     '                    and "top_up" in c["data"] and "top_dn" in c["data"] \\\n',
     '',
     "[涨跌榜] hot 缓存命中须校验 top_up/top_dn 键（缺键视为未命中）",
     "旧 schema 缓存"),

    # --- AI 漏写「热点」段无留痕（[涨跌榜] 应抓的可观测性面）---
    # 实测 2026-10-12 13:30 那轮：brief 有【市场热点】8 条，AI 只写了
    # 关注板块、整段漏掉热点（正文仅 269 字，非字数不够）。判据：
    # produce_text 必须留下一条日志，否则"段消失"与"AI 精简"无法区分。
    ("hot-section-missing-silent",
     "analyze.py",
     '    _has_hot_sec = any(_l.lstrip().startswith("**热点")\n'
     '                       for _l in txt.split("\\n"))\n'
     '    if "【市场热点 TOP" in brief and not _has_hot_sec:\n'
     '        _log(f"AI 输出缺「热点」段（brief 含市场热点数据）slot={slot or \'event\'}")',
     '    pass  # 注入：热点段缺失不再留痕',
     "[涨跌榜] AI 漏写热点段必须留日志（否则与'AI 精简掉了'无法区分）",
     "未对「热点段缺失」留痕"),

    # --- [兜底模板建议段] 风险段重复触发原文（该段应抓）---
    # 真缺陷形态：板块全在阈值内时，风险段回退去抄 hits 里的语义事件原文，
    # 而触发段是 hits 全量 join —— 同一句「恒生科技4101.32≤4300」在
    # 标题/触发/风险三处逐字出现。`[实测]` 2026-10-09 10:00:57 生产推送。
    # 判据是 [兜底模板建议段] 的断言 ⑥（自己造「有价无资金 + 语义事件 hits」
    # 的触发条件，再断言风险段不含该原文）。
    ("risk-dup-trigger-text",
     "analyze.py",
     "            if str(k) in _trg_keys:\n"
     "                continue",
     "                # 注入：不再按键去重，风险段原样抄触发段已呈现的原文",
     "[兜底模板建议段] 应报「风险段重复触发原文」",
     "风险段重复了触发段已呈现的语义事件原文"),

    # --- 兜底标题与【触发】段指向不同板块（2026-10-23 加）---
    #
    # 真缺陷形态：`_judge` 的「领跌」只从 `_rows_f3`（**配置的 8 个关注板块**）
    # 里选，而【触发】行报的是 `hits`（**全市场扫描**）。两者是不同集合，
    # 于是标题可能说 A、触发行说 B。
    # `[实测]` 2026-10-23 09:31:47 真实推送：
    #   标题「触发提醒 · 印制电路板领跌」   ← 印制电路板 -3.18%（关注板块最弱）
    #   触发「贵金属-4.15%」               ← 贵金属是全场最弱（不在关注板块里）
    # 回查 history/pushes.jsonl：代码拼的标题里 **13/20 条**有这种错位。
    #
    # 变异体把「对齐触发段」那段整块短路，退回只看关注板块的旧行为。
    ("fallback-title-misaligned-trigger",
     "analyze.py",
     "    _judge_t = _judge\n"
     "    if hits and _rows_f3:\n",
     "    _judge_t = _judge\n"
     "    if False:   # 注入：标题不再对齐触发段，退回只看关注板块\n",
     "[兜底模板建议段] 标题的「领跌/领涨」板块必须与【触发】段一致"
     "（不一致时读者会以为数据自相矛盾）",
     # ⚠️ must_contain 必须与 selfcheck.py **实际报出的那句话**逐字对齐
     # （2026-10-23 实测，第二次踩同型）。本变异体短路的是整个 `if hits
     # and _rows_f3` 块，于是标题退回 `risk[0]` 拼的「半导体资金流出居前」
     # —— 那**不是**「X领跌/领涨」形态，所以走的是断言⑦ 的**第二支**，
     # 实际文案是「既不是「X领跌/领涨」形态、也不在【触发】段…」。
     # （第一支「标题说「X领跌」…」只有当标题确实是领跌/领涨形态时才走到。
     #  凭"这条测的是领跌"去猜文案，就会猜错 —— 必须抄实际输出。）
     "既不是「X领跌/领涨」"),

    # --- 触发段有内容但标题仍用无关板块（同上缺陷的另一半）---
    # 上一版只处理了"触发段里出现关注板块"这一支；这一条把**回退到触发原文**
    # 的那一支短路 —— 10-23 09:31 的真实场景正是走这一支
    # （贵金属不在关注板块里，必须直接引用触发段）。
    ("fallback-title-ignores-trigger-text",
     "analyze.py",
     '            _t0 = ""\n'
     '            for _sg in _trg_txt.split("；"):\n',
     '            _t0 = ""   # 注入：不引用触发原文，保留无关板块标题\n'
     '            for _sg in ():\n',
     "[兜底模板建议段] 触发段里的板块不在关注板块时，标题必须引用触发原文"
     "（否则标题指向一个与触发无关的板块）",
     # 同上：这条走的是断言⑦ 的第二支，报错文案是「标题「…」既不是「X领跌/领涨」…」
     "既不是「X领跌/领涨」"),

    # --- HTTPS 端点候选里混入永久封锁主机（2026-10-23 加）---
    #
    # `[实测]` 2026-10-23 09:20:10 生产日志「跳过已永久封锁的主机…push2delay」
    # —— clist / trend 每次都先撞一次 push2delay。`cj()` 认得永久封锁、
    # 零发包返回 None，但调用方照常走下一轮，于是每次多一层判定 + 一条噪音日志。
    # 这个 bug 在 feeds.health() 修过一次，但三处是另一份硬编码副本，漏改了。
    ("https-hosts-include-permadead",
     "marketlib.py",
     "    return [h for h in _ordered_em_hosts()\n"
     "            if h.startswith(\"https://\") and _host_of(h) not in PERMANENTLY_DEAD]",
     "    return [EM, EM_TEST]   # 注入：退回硬编码，不过滤永久封锁",
     "[行情主机排序] HTTPS 端点候选不得包含 PERMANENTLY_DEAD 主机"
     "（否则每次 clist/trend 都白撞一次 + 打噪音日志）",
     "HTTPS 端点候选里含永久封锁主机"),

    # ============ 第五十三轮：launchd 日志轮转 ============
    #
    # 真缺陷（2026-10-09 查明）：launchd 在 exec **之前**按 StandardOutPath
    # 打开文件并把 fd 交给子进程（探针实测 flags = `O_RDWR|O_APPEND`）。
    # 用 `os.replace` 轮转**完全无效** —— rename 只改目录项，fd 仍指向原
    # inode：`.out` 路径整个消失、空间一点没腾、且**零信号**。
    # 原先三处实现（marketlib / webui / selfcheck）里有两处用的就是
    # `os.replace`，且当时**没有任何检查项**能发现（[定时任务] 只对账
    # "名单里有没有这个文件名"，不看轮转算法对不对）。
    #
    # 判据是 [日志轮转] 的**行为**测试：保持一个 O_APPEND fd 打开、
    # 轮转、再用老 fd 追加，然后看 base / `.1` 的内容分布。
    # 两种写法在源码里形状一样，只有跑一遍才分得出来。
    ("rotate-revert-os-replace",
     "marketlib.py",
     "            shutil.copyfile(p, p + \".1\")\n"
     "            _harden(p + \".1\")          # copyfile 按 umask 建 → 得自己收紧\n"
     "            with open(p, \"r+b\") as f:\n"
     "                f.truncate(0)",
     "            os.replace(p, p + \".1\")\n"
     "            _harden(p + \".1\")",
     "[日志轮转] 应报「base 消失 / .1 混入新内容」",
     "轮转后 base 文件**不存在**"),

    # ⚠️ 轮转逻辑在本项目里有**三个独立副本**（marketlib / webui /
    # selfcheck），上面那条只打 marketlib 那份。[日志轮转] 必须**每份都测**
    # —— 否则某一份单独退化时它照样全绿（"同形兄弟只装一半闸门"，
    # 本项目反复栽的那类）。下面两条分别打另两份。
    ("rotate-webui-os-replace",
     "webui.py",
     "            _sh.copyfile(p, p + \".1\")\n"
     "            _harden_logs(p + \".1\")          # copyfile 按 umask 建 → 得自己收紧\n"
     "            with open(p, \"r+b\") as f:\n"
     "                f.truncate(0)",
     "            os.replace(p, p + \".1\")\n"
     "            _harden_logs(p + \".1\")",
     "[日志轮转] 应报 webui._rotate_out 的 base 消失",
     "webui._rotate_out: 轮转后 base 文件"),

    ("rotate-selfcheck-os-replace",
     "selfcheck.py",
     "            with open(p, \"rb\") as src, open(p + \".1\", \"wb\") as dst:\n"
     "                while True:\n"
     "                    _b = src.read(1 << 16)\n"
     "                    if not _b:\n"
     "                        break\n"
     "                    dst.write(_b)",
     "            os.replace(p, p + \".1\")",
     "[日志轮转] 应报 selfcheck._rotate_out 的 base 消失",
     "selfcheck._rotate_out: 轮转后 base 文件"),

    # 顺序错：先截断后 copy → `.1` 拷到的是**已清空**的文件，历史全丢。
    # 与上面那个方向不同（那个是"路径消失"，这个是"内容丢失"），
    # 危害也不同：前者还能从 `.1` 里找回日志，后者**永久丢**。
    ("rotate-truncate-before-copy",
     "marketlib.py",
     "            shutil.copyfile(p, p + \".1\")\n"
     "            _harden(p + \".1\")          # copyfile 按 umask 建 → 得自己收紧\n"
     "            with open(p, \"r+b\") as f:\n"
     "                f.truncate(0)",
     "            with open(p, \"r+b\") as f:\n"
     "                f.truncate(0)\n"
     "            shutil.copyfile(p, p + \".1\")\n"
     "            _harden(p + \".1\")",
     "[日志轮转] 应报「.1 里没有旧内容」",
     "`.1` 里没有旧内容"),

    # 漏一个调用点：collect 的 rotate 调用被删（harden 还在）。
    # [调度方式] 3c) 的名单对账必须抓到 —— 这是"半吊子加固"的典型形态：
    # 加了 harden 忘了加 rotate，文件仍是 600 但**无界增长**。
    ("rotate-drop-collect-call",
     "collect.py",
     "    M.rotate_logs(\"collect.out\", \"collect.err\")   # 单代轮转（copy+截断，见 marketlib）\n",
     "",
     "[调度方式] 应报「collect.out 未列进 rotate_logs 实参」",
     "找不到 rotate_logs(...) 调用"),

    # ============ 第五十四轮：自指假红（selfcheck 判自己） ============
    #
    # 真缺陷（2026-10-10 查明）：`[定时任务]` 的 last-exit-code 判据只看
    # `rc != 0 and not _alive`，而 **selfcheck 自己**的退出码**就是它的结论**
    # （0=正常 / 1=发现异常 / 2=自检自身坏了），且它是一次性任务、
    # `launchctl list` 里永远是 `-`。于是它每发现一次异常，**下一次**自检
    # 就把它报成「任务在反复起、反复失败（plist 可能坏了）」——
    # 自指假红，且**锁死**：rc=1 → 下轮 ❌ → rc 仍 1 → 永远报下去，
    # 即使最初那个缺陷早修好了。生产 2026-10-09 的 rc=1 就是这种
    # （当时因 baidu 源不可用置位，是**正确**的结论）。
    #
    # ⚠️ 这个变异体**不需要造夹具** —— 沙箱的 `[定时任务]` 读的是**真实
    # launchd 状态**（`launchctl print gui/<uid>/...`），而生产
    # `marketselfcheck` 的 last exit code 长期就是 1（它每次发现异常都会
    # 置 1，那是设计内行为）。所以把一次性任务的分支整体删掉，沙箱里
    # 立刻复现"报成反复起、反复失败"的假红。
    ("sched-selfcheck-rc1-not-alarm",
     "selfcheck.py",
     "            if label in _ONESHOT:\n"
     "                if _rc in _ONESHOT_OK_RC:",
     "            if False:\n"
     "                if _rc in _ONESHOT_OK_RC:",
     "[定时任务] 应把 selfcheck 的 rc=1 认作设计内结论（不该报反复起）",
     "反复起、反复失败"),

    # -------------------- 2026-10-16：marketagent 的 rc 分类 --------------------
    #
    # 2026-10-13 上线第 5 个 job（marketagent）时只补了 `_jobs` 名单，rc 分类
    # 表没同步 → 它落进"普通常驻"默认分支，rc=1 + 无进程被报成
    # 「反复起、反复失败（plist 坏了）」。生产 2026-10-15 连续两轮真误报
    # （真因是 `create_sdk_mcp_server` 启动崩溃，全在进程内，plist 好好的）。
    # 修法：给 agent 单列一支 —— rc=1 **恒报**（措辞指向 agent.err，不指 plist）。
    # 本变异体（数据注入型）把夹具里 agent 的 rc 从 0 改成 1，验证该支可达且
    # 报出的措辞是"本轮调研失败"而非"反复起、反复失败"。
    ("sched-agent-rc1-alarm",
     "__DATA_LC_AGENT_RC1__",
     "", "",
     "[定时任务] marketagent rc=1 应报「本轮调研失败」（指向自身日志，非 plist）",
     "本轮调研失败"),

    # -------------------- 2026-10-16：marketagent 的 rc 分类 --------------------
    # 分支，被误报成「反复起、反复失败（plist 坏了）」。**双重注入**：夹具
    # 先造 rc=1 触发条件（否则 rc=0 下打断分支也不报，是空操作 —— 记忆
    # 「变异体夹具要自己造触发条件」），再改源码。
    ("sched-agent-rc1-misclassified",
     "__DATA_LC_AGENT_RC1_MISCLASS__",
     "", "",
     "[定时任务] agent 若未按调研任务分类，rc=1 会被误报为「反复起、反复失败」（复现旧缺陷）",
     "反复起、反复失败"),

    # -------------------- 2026-10-16：盘面行的指数补齐 --------------------
    #
    # 用户实测反馈：「盘面…我最初要求的各大指数都要显示的，现在只有两个
    # 指数」（2026-10-16 13:30 那轮 AI 卡死走兜底模板，其盘面行硬编码只有
    # 上证 + 港股科技）。两条路径都改成按 sectors.json 的 index 清单列全，
    # `[兜底模板与结构闸门]` 的断言⑥分别验证两条路径。
    # ① AI 路径：`_norm_index_line` 的补齐被去掉（模型漏列的指数不再补）
    ("index-line-fill-dropped",
     "analyze.py",
     '    _lines[_li] = _ln2 + "；" + "、".join(_parts) + "。"',
     "    _lines[_li] = _ln2",
     "[兜底模板与结构闸门] AI 路径盘面行漏列指数时必须机械补齐",
     "未补齐指数"),
    # ② 兜底路径：盘面行退回"只列前两个"（旧硬编码形态）
    ("index-line-fallback-truncated",
     "analyze.py",
     '        _pans = "**盘面** " + "、".join(_pans_items) + "。"',
     '        _pans = "**盘面** " + "、".join(_pans_items[:2]) + "。"',
     "[兜底模板与结构闸门] 兜底模板盘面行必须列全配置指数（不能只两个）",
     "兜底模板盘面行缺指数"),

    # -------------------- 关键位行补齐（2026-10-19 用户反馈） --------------------
    # 用户：「我发现关键位这一块，每次都是只报恒生科技的点位，没有其他指数的」
    # 13 条含关键位段的推送恒科 11 次、恒生指数 1 次 —— prompt 规则④曾
    # 授权「只挑 2~4 个」，plan.md 又以恒科为主角，免写权全用在恒科之外。
    # 修法与盘面行（index-line-fill-dropped）同款：`_norm_levels_line`
    # 代码层机械补齐全部配置指数，prompt 规则④同步改为「指数全列、
    # 板块挑 2~4」。变异点把**拼接那一步**改成空操作（`_lv_parts` 照旧
    # 算出来、日志照旧报「补齐 N 个」，只是不写回正文）——
    # 于是 `_miss` 里的指数在关键位行上整段消失，断言⑥b 必报缺。
    # ⚠️ 只把 RHS 换掉、保留 `if _lv_parts:` 骨架：`_lv_parts` 必须仍然
    # 被求值，否则"算而不拼"这个真实缺陷形态（首版实现就是如此）
    # 与"整段删掉"会在测试里同形。
    # 港股休市的**措辞**（2026-10-19 事故的另一半）：盘面行里 AI 写的
    # 「恒生指数 数据缺失」必须被改成「港股休市」—— 前者把读者引向"查系统"，
    # 后者说明"今天不开市"。改的是措辞不是数字。
    # 措辞修正被 early-return 丢掉（2026-10-19 14:40/14:48 两条推送实测）：
    # 把「改完的文本」重新退回原始 `t`。
    ("hkclosed-wording-lost-earlyreturn",
     "analyze.py",
     '        return "\\n".join(_lines) if _rewrote else t',
     '        return t',
     "[兜底模板与结构闸门] 港股休市日盘面行的「数据缺失」必须改成「港股休市」",
     "未被修正（early-return 丢改动）"),
    ("hkclosed-wording-kept",
     "analyze.py",
     '                    _new_r = _new_r.replace(_bad, f"{_a_r} 港股休市")',
     '                    pass',
     "[兜底模板与结构闸门] 港股休市日盘面行的「数据缺失」必须改成「港股休市」",
     "盘面行仍写「数据缺失」"),
    # 港股休市日不得用昨收判 hstech 阈值（detect / collect 两处，
    # 原先只判「09:30 之后」）。判据是**构造值**：钉在已知港股假期
    # （重阳翌日）喂一份"行情看起来正常"的 q，看 detect 会不会误报。
    ("hk-closed-detect-ignores-calendar",
     "analyze.py",
     "        if _hk_live:",
     "        if hm >= 9 * 60 + 30:   # 注入：只判开盘，不判休市/半日市收盘",
     "[兜底模板与结构闸门] 港股休市日不得用昨收判 hstech 阈值",
     "在港股**休市日**"),
    # 港股日历（2026-10-19 重阳节翌日事故）：
    #   ① 表被清空 → 所有港股休市日都会被当成交易日，推送写「数据缺失」
    #   ② 判定退化成 A 股日历 → 重阳/佛诞/受难节这些"港股独有"的休市日
    #      全被判成交易日（本项同时证明两张表必须独立）
    ("hk-halfday-coverage-alarm-blind",
     "marketlib.py",
     # ⚠️ 2026-11-05（P2）：锚点原先钉在常量**完整赋值**（含 "2027-12-31"）上 ——
     # 将来更新覆盖期时锚点会静默失配（记「锚点未匹配，跳过」），这条变异体
     # 从此空转。现只钉**赋值前缀**（与值无关），注入值用 `或` 短路保留
     # 原字面量：`X = "2000-01-01" or "2027-12-31"` 求值为前者 ——
     # 行为与原注入（常量改成过去日期）完全一致，且任何年份更新都不用动它。
     'HK_HALF_DAYS_COVERED_THROUGH = ',
     'HK_HALF_DAYS_COVERED_THROUGH = "2000-01-01" or ',
     "[节假日表] 半日市表覆盖期过期必须报 ❌（2026-11-04 新增判据）—— "
     "否则 2028 起半日市下午的行情会被新鲜度闸门整批判「数据缺失」，"
     "而自检仍报绿",
     "❌ 半日市日历覆盖至"),
    ("hk-calendar-emptied",
     "marketlib.py",
     "    \"2026-10-19\",                                   # 重陽節翌日  ← 本轮事故当天\n"
     "    \"2026-12-25\", \"2026-12-26\",                     # 聖誕節 / 後第一個周日\n",
     "",
     "[节假日表] 港股休市日（重阳翌日）必须被判休市",
     # ⚠️ 2026-11-17：改锚到**计数摘要行**。原先与下一条共用
     # "港股休市日 2026-10-19"，两条的命中段实测完全相同 —— 判别力重叠，
     # 无法区分"到底验到哪一条"。删一行只动 1 天，换表动 31 个工作日，
     # 计数天然可区分。
     "港股已知休市日共 1/"),
    ("hk-calendar-uses-a-share",
     "marketlib.py",
     "    return d.weekday() < 5 and d.isoformat() not in HK_HOLIDAYS",
     "    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS",
     "[节假日表] 港股日历必须与 A 股日历相互独立",
     # 同上：换表会让**多个**已知休市日同时翻转，计数必然 >1
     "港股已知休市日共 5/"),
    # ⚠️ 锚点 2026-10-19 随 `_levels_line_body` 抽取更新：原锚点落在
    #    `_norm_levels_line` 里内联的 `_lv_parts` 拼接语句上，抽取后那几行
    #    已搬进渲染器（一处渲染、两处调用），旧锚点匹配 0 次 → 该变异体
    #    会变成空操作、报「漏网」（[[market-watch-duplicated-truth-breaks-anchors]]）。
    #    新锚点打在两处调用都必经的渲染器返回值上。
    ("levels-line-fill-dropped",
     "analyze.py",
     '    _body = "；".join(_parts)',
     '    _body = ""  # 注入：补齐内容被丢弃',
     "[兜底模板与结构闸门] AI 路径关键位行漏列指数时必须机械补齐",
     "关键位行未补齐指数"),

    # 整段缺失时**造段**这一支（2026-10-19：deadline 2/2、10:00 13/13、
    # 11:30 12/12、summary 12/12 条推送整段没有关键位）。
    # 把造段分支改成 return t → 有行的情形仍能补漏项（上面那条变异体不管
    # 这一支），整段消失的情形重新回到静默。
    ("levels-section-missing-not-created",
     "analyze.py",
     '        _mk = _levels_line_body(_lv_idx, q, now)\n'
     '        if not _mk[0]:\n'
     '            return t                 # 一项日K都没有 —— 不造空段',
     '        _mk = _levels_line_body(_lv_idx, q, now)\n'
     '        if True:\n'
     '            return t                 # 注入：整段缺失时不造段',
     "[兜底模板与结构闸门] 关键位整段缺失时必须按配置补出该段",
     "关键位整段缺失时未补出该段"),

    # 补出的段必须**排在盘面之后**且前后有空行 —— 追加到末尾是初版
    # 实现（2026-10-19 自查发现与 AI 路径的段落顺序不一致）。
    # 这条变异体把插入退化成"追加到全文末尾"，自检应报"排序/空行"。
    ("levels-section-appended-at-end",
     "analyze.py",
     '        _pi = next((_i for _i, _l in enumerate(_lines)\n'
     '                    if _l.lstrip().startswith("**盘面")), None)\n'
     '        if _pi is None:\n'
     '            _lines += ["", _new]            # 没有盘面段：退化为追加\n'
     '        else:\n'
     '            _pj = _pi + 1\n'
     '            while (_pj < len(_lines) and _lines[_pj].strip()\n'
     '                   and not _lines[_pj].lstrip().startswith("**")):\n'
     '                _pj += 1\n'
     '            _lines[_pj:_pj] = ["", _new]',
     '        _lines += ["", _new]  # 注入：一律追加到末尾',
     "[兜底模板与结构闸门] 补出的关键位段应紧随盘面段、前后带空行",
     "补出的关键位段"),

    # 结构闸门按 slot 要求关键位这一支。把名单清空 → deadline 缺段重新
    # 静默过闸（2/2 实测就是这种形态）。
    ("struct-lv-slot-list-emptied",
     "analyze.py",
     '_STRUCT_LV_SLOTS = ("09:25", "14:30", "deadline")',
     '_STRUCT_LV_SLOTS = ()  # 注入：不再按 slot 要求关键位',
     "[兜底模板与结构闸门] deadline 的 rule 要求写关键位，缺段必须被结构闸门判出",
     "deadline 缺关键位段未被结构闸门判出"),

    # --- 关键位**段名**判据退回裸前缀（2026-10-26 实测后补）---
    # 09:25 的 rule 逐字要求「②**今日关键位**」，AI 照写 `**今日关键位**`
    # 时，写死 `startswith("**关键位")` 的判据判它"缺段" → 白跑一轮完整
    # 重写。`[实测]` 10-26 09:38:50 生产日志正是这条（重写又耗 134s）。
    # 本变异体把 `_STRUCT_LV_HEAD_RE.match(...)` 退回旧写法。
    ("struct-lv-head-name-too-narrow",
     "analyze.py",
     '                    _l.lstrip().startswith("**关键位")\n'
     '                    or _STRUCT_LV_HEAD_RE.match(_l.lstrip())',
     '                    _l.lstrip().startswith("**关键位")',
     "[兜底模板与结构闸门] 段名「今日关键位」（prompt rule 原文如此）"
     "不得被判缺段（写死前缀会让照 prompt 写的合规输出白跑一次重写）",
     "段名「今日关键位」"),

    # --- 关键位段头判据退回**裸子串**（反方向锁）---
    # 放宽段名时最容易滑向"行里出现『关键位』就算有"。那样
    # `**触发** 未触发关键位与资金异动阈值` 会被当成关键位段，
    # 真缺段的那轮悄悄过闸 —— 判据必须锚**段头形态**（`**…关键位…**`）。
    ("struct-lv-head-substring-too-loose",
     "analyze.py",
     '                    _l.lstrip().startswith("**关键位")\n'
     '                    or _STRUCT_LV_HEAD_RE.match(_l.lstrip())',
     '                    "关键位" in _l',
     "[兜底模板与结构闸门] 「关键位」只出现在别的段正文里时，"
     "不得被当成关键位段（裸子串判据会漏放真缺段）",
     "「关键位」只出现在别的段正文里"),

    # --- `元`/`块` 后的金额后缀判据被退回（2026-10-26 生产 P0）---
    # 生产连续 5 轮推送失败：`_mask()` 把「今日跌幅 TOP3」的 `3` 与下一行
    # 板块名「元件」拼成 `3元件`，被 `\d+元` 分支判成金额泄漏。
    # `[实测]` 13:41/13:52/14:02 三轮把 13:30 槽位彻底作废，14:25 连
    # 事件类推送也被拦 —— 用户当天下午完全没收到播报。
    # 本变异体把后缀判据摘掉，退回「`\d+元` 无条件命中」的旧形态。
    ("amt-yuan-suffix-guard-dropped",
     "marketlib.py",
     '    r"|(?:\\d[\\d,]*(?:\\.\\d+)?\\s*(?:元|块))(?![" + _AMT_COMPOUND + r"])"\n',
     '    r"|(?:\\d[\\d,]*(?:\\.\\d+)?\\s*(?:元|块|千元|千))"\n',
     "[安全闸门] `元`/`块` 后跟板块名汉字（元件/元宇宙）不得判成金额"
     "（实测连续 5 轮推送失败，13:30 槽位整天作废）",
     # ⚠️ must_contain 必须与自检**实际报出的首条 ❌** 逐字对齐：
     # ALLOW 用例失败时输出的是 `❌ 放行: <文本>`（不是 BLOCK 的
     # `拦截[标签]` 形态），故锚文本本身而不是标签。
     "❌ 放行: **今日跌幅 TOP3**"),

    # --- 中文数字分支的 `元/块` 后缀判据被退回（2026-10-26）---
    # 与 `amt-yuan-suffix-guard-dropped` **同一根因的第二个实例**：
    # `_mask` 把「排名第三」+ 换行 + 「元件」拼成 `三元`。
    # 阿拉伯数字那条修完后，中文数字分支原样留着同样的洞。
    ("amt-cn-yuan-suffix-guard-dropped",
     "marketlib.py",
     # 锚点随 2026-11-08 的修复同步：中文数字与单位之间新增了可选的约数助词类。
     '    r"|(?:[" + _CN_NUM + r"]{1,6}[多来余几把数]?\\s*(?:元|块))(?![" + _AMT_COMPOUND + r"])"\n',
     '    r"|(?:[" + _CN_NUM + r"]{1,6}\\s*(?:万元|万|元|块|千元|千))"\n',
     "[安全闸门] 中文数字 + 板块名（排名第三+元件 / 第一元件）不得判成金额",
     "❌ 放行: 排名第三"),

    # --- 后缀判据放宽过头：真金额被放行（反方向锁）---
    # 上一条变异体守"不能误拦板块名"，这一条守"不能把真金额一起放过" ——
    # 金额泄漏是不可观测的一侧（发出去收不回）。
    ("amt-yuan-suffix-guard-overbroad",
     "marketlib.py",
     # ⚠️ 锚点必须带上前一行（阿拉伯数字那条 `元|块` 分支）才唯一 ——
     # 2026-10-26 修完中文数字分支的同款洞之后，单行后缀守卫在 marketlib.py
     # 里出现 **2 次**，`apply_mutant` 的「非唯一锚点拒绝替换」把它判成
     # 「锚点未匹配，跳过」，R14 全量因此差这一条没绿、基线没能回写。
     # ⚠️ 2026-11-06：现在有**三条**行为一致的金额分支
     #   ① `\d[\d,]*\s*(?:元|块)(?!compound)`  → ② `4 位以上硬分支` → ③ 中文数字
     # 所以锚点必须连①一起改掉，否则②会接住（`花了3000元以上`
     # 即 4 位数），该变异体形同未注入 —— 实测距离提交前就这么漏了一次。
     '    r"|(?:\\d[\\d,]*(?:\\.\\d+)?\\s*(?:元|块))(?![" + _AMT_COMPOUND + r"])"\n'
     '    r"|(?:(?:\\d{1,3}(?:,\\d{3})+|\\d{4,})(?:\\.\\d+)?\\s*(?:元|块|万|千))(?!亿)"\n',
     # 替换串必须仍是**合法的字符串参数**（锚点在
     # `re.compile(...)` 的参数序列里，`pass` 会把语法搞坏）。
     # `(?!)` 是「永不匹配」的零宽断言：分支被彻底取消，
     # 而 `re.compile` 仍能正常构造—— 与原注入意图等价。
     '    r"|(?!)"  # 注入：硬分支一拆掉\n'
     '    r"|(?!)"  # 注入：硬分支二拆掉\n',
     "[安全闸门] 放宽 `元` 判据后，真金额（以上/左右/余额/整/斜杠）必须照拦",
     "❌ 拦截[金额(元+后缀 以上)]"),

    # --- 内容失效复查整段被删（[内容失效复查] 应抓）---
    # `[实测]` 2026-10-26 09:25 槽位：09:19:12 认领（窗口 [09:17,09:33)，
    # 合法），AI 跑 1312s（21.9 分钟），**09:41:04** 才推送 —— 而该槽位
    # 的内容失效时刻是 **09:35**，晚了 6 分钟。推出去的正是 `SLOT_VALID_UNTIL`
    # 注释举反例的那种「把盘中价标成昨收」的简报（brief 是 09:19 抓的）。
    # 最坏情况：09:32 认领 + AI 22 分钟 = 09:54 推送，过期 19 分钟。
    # 本变异体把整块复查短路掉 —— 自检必须报「09:41 已过期却仍推送」。
    ('slot-expired-gate-removed',
     'analyze.py',
     '    if not dry and _chk_slot:\n        _vu = SLOT_VALID_UNTIL.get(_chk_slot)\n        if _vu is not None:\n            _hn = datetime.datetime.now()',
     '    if False:   # 注入：不再复查内容失效\n        _vu = SLOT_VALID_UNTIL.get(_chk_slot)\n        if _vu is not None:\n            _hn = datetime.datetime.now()',
     '[内容失效复查] 过期槽位必须整条不发（09:41 那轮已实证：晚 6 分钟推了「把昨收标成当日」的盘前简报）',
     '09:41 已过期却仍推送'),

    # --- 过期后不清 hourly_retry（会以 hourly 身份补推同样的过期内容）---
    ('slot-expired-retry-kept',
     'analyze.py',
     '                st.get("hourly_retry", {}).pop(_chk_slot, None)',
     '                pass  # 注入：过期后仍留补推凭据',
     '[内容失效复查] 过期后必须清掉 hourly_retry（留着会让 _in_grace() 以 hourly 身份再试一次同样的过期内容）',
     'hourly_retry 仍留着'),

    # --- 过期判据用 now（轮次起点）而不是当前时刻（等于没查）---
    # `now` 是**轮次起点**，比推送早整整一个 AI 时长（今天 22 分钟）。
    # 拿它复查 = 查的还是认领时的值 —— 与本项要防的偏差完全同源。
    ("slot-expired-uses-round-start",
     "analyze.py",
     '            _hn = datetime.datetime.now()\n'
     '            _hdiff = _hn.hour * 60 + _hn.minute - _vu',
     '            _hn = now\n'
     '            _hdiff = _hn.hour * 60 + _hn.minute - _vu',
     "[内容失效复查] 必须用**当前时刻**判断（用轮次起点 `now` 查的是"
     "22 分钟前的值，闸门永远不命中）",
     "09:41 已过期却仍推送"),

    # --- [内容失效复查] 行为测试漏桩 save_st → 夹具 state 落盘生产 ---
    # `[实测]` 2026-10-27 栈回溯坐实（selfcheck.py:9880 -> analyze.py:5497
    # -> _save_st_ck -> save_st -> STATE）：本检查项直接调
    # `_run_after_inflight`，原先只桩了 produce_text/push，`save_st` 走的
    # 是真实实现 —— **每次生产自检都把夹具 state 写进生产 state.json**
    # （`{"fired": [], …, "count": 1, "date": …}`，date 在最后、缺
    # inflight_ts），洗掉当天 fired 去重凭据：2026-10-26 14:19 那次自检后
    # 14:30 槽位重复推送两次；15:45:42 / 22:40:59 两次落盘也都是它。
    # 注入：删掉 save_st 桩 → main() 的「生产 state 写入守卫」必须当场报 ❌
    # （守卫拦截落盘 + 按调用点记账，收尾打印）。
    ("content-validity-save-not-stubbed",
     "selfcheck.py",
     '            _A23.save_st = lambda *a, **k: True\n',
     '',
     "[内容失效复查] 行为测试必须桩 save_st —— 否则夹具 state 落盘生产 "
     "state.json、洗掉当天 fired（2026-10-26 14:30 重复推送的根因）",
     "把夹具 state 写进了生产 state.json"),

    # 空段判据只认「（本段无数据）」、漏掉「（日K暂不可用）」——
    # 后者是 build_brief 在日K全挂时的真实标注。漏掉它就等于在
    # **补不出这段**的日子里反复要求必带，每轮白跑一次重写。
    ("struct-lv-emptymark-brief-only",
     "analyze.py",
     '            if not ("（本段无数据）" in _vsec\n'
     '                    or "（日K暂不可用）" in _vsec) and not any(',
     '            if not ("（本段无数据）" in _vsec) and not any(',
     "[兜底模板与结构闸门] 日K全挂（（日K暂不可用））时不得要求必带关键位",
     "日K全挂"),

    # --- 港股日历闸门的另两处（2026-10-20 自查）---
    # 2026-10-19 只修了 analyze.detect 与 collect 的 hstech 支；IDX3S 循环
    # 与 alert.check_index 同样拿"昨收 f3"判 3σ，休市日会误报（alert 那条
    # 还会推紧急消息）。
    ("hk-gate-missing-collect-idx3s",
     "collect.py",
     # ⚠️ 2026-11-06：港股/A 股两个闸门合并成一个分支后，
     # 原锚点已不存在。病态不变：把港股分支整体短路。
     '            if _is_hk_row:\n'
     '                if not _hk_live_now:\n'
     '                    continue',
     '            if _is_hk_row:\n'
     '                pass  # 注入：不再按港股日历跳过 IDX3S\n',
     "[港股休市] collect.evaluate 港股休市日不得用港股 f3 判 IDX3S 3σ",
     "collect.evaluate 在港股休市日仍用港股 f3"),
    ('hk-gate-missing-alert-index',
     'alert.py',
     '        _is_hk_idx = str(sid).startswith(("100.", "124."))\n        if _is_hk_idx and not _hk_live:\n            continue',
     '        _is_hk_idx = str(sid).startswith(("100.", "124."))',
     '[港股休市] alert.check_index 港股休市日不得发紧急（用昨收判 3σ）',
     'alert.check_index 港股休市日仍发港股紧急'),
    # --- 半日市下午：三处闸门退化回"今天开不开市"（2026-11-05，P1-13）---
    # 真缺陷形态：`is_hk_trading_day` 在半日市当天恒为 True —— 12:00 收盘后
    # 行情定格在当日收盘价，三个闸门整个下午仍拿它判"盘中异动"。
    # 注入：把统一判据改回只判日期（三处各自独立注入，确保每处都被覆盖 ——
    # 只改一处时另两处仍会拦住行为探针，那样抓不住单处退化）。
    ("hk-halfday-detect-ignores-close",
     "analyze.py",
     "    _hk_live = M.hk_session_open(now)",
     "    _hk_live = M.is_hk_trading_day(now.date())",
     "[兜底模板与结构闸门] 半日市 12:00 后不得判 hstech（定格收盘价）",
     "半日市下午 14:00 判出"),
    ("hk-halfday-collect-ignores-close",
     "collect.py",
     "    _hk_live_now = M.hk_session_open(now)",
     "    _hk_live_now = M.is_hk_trading_day(now.date())",
     "[港股休市] collect.evaluate 半日市 12:00 后不得判 IDX3S/hstech",
     "半日市下午仍用定格 f3"),
    ("hk-halfday-alert-ignores-close",
     "alert.py",
     "    _hk_live = M.hk_session_open(now)",
     "    _hk_live = M.is_hk_trading_day(now.date())",
     "[港股休市] alert.check_index 半日市 12:00 后不得发港股紧急",
     "半日市下午仍发港股紧急"),
    # --- 紧急播报信息量（2026-11-05，用户反馈「播报的信息太少」）---
    # 原先紧急播报只有一行涨跌幅。扩充的点位 / 盘面 / 快照 / 摘要
    # 各配一条注入，防止一次重构悄悄退回"一行涨跌幅"。
    ("urgent-index-point-dropped",
     "analyze.py",
     '            _last_i = e.get("last")',
     '            _last_i = None   # 注入：不显示点位',
     "[紧急播报信息量] 指数异动必须带点位（f2）",
     "缺少点位"),
    ("urgent-board-context-dropped",
     "analyze.py",
     '            _bd = e.get("board") or []\n'
     '            if _bd:',
     '            _bd = []   # 注入：不输出同轮盘面\n'
     '            if _bd:',
     "[紧急播报信息量] 必须输出其余指数同轮盘面",
     "缺少其余指数盘面"),
    ("urgent-news-summary-dropped",
     "analyze.py",
     '                if _sm and _safe(_sm)[0]:',
     '                if False:   # 注入：不输出快讯摘要',
     "[紧急播报信息量] 快讯必须带摘要（标题常跑题）",
     "缺少快讯摘要"),
    ("urgent-board-unwired",
     "alert.py",
     '    for _h in hits:\n'
     '        _h["board"] = [(n2, c2) for n2, c2 in _board\n'
     '                       if n2 != _h.get("name")]',
     '    pass   # 注入：盘面上下文不挂到事件上',
     "[紧急播报信息量] check_index 必须把盘面上下文挂到事件上",
     "事件缺少其余指数盘面"),

    # 反方向：整段补齐不看槽位（原先就是无条件造段）→ 10:00 那种
    # 「只报异动事实」的消息被塞进 6 个指数的 20 日高低。
    ("levels-section-created-for-all-slots",
     "analyze.py",
     '        if slot not in _STRUCT_LV_SLOTS:\n'
     '            return t\n'
     '        _mk = _levels_line_body(_lv_idx, q, now)',
     '        _mk = _levels_line_body(_lv_idx, q, now)',
     "[兜底模板与结构闸门] 不要求关键位的槽位（如 10:00）不得被造出该段",
     "10:00 的 rule 不要求关键位，却被造出了该段"),

    # -------------------- 第五十六轮：launchd 夹具（MW_LAUNCHCTL） --------------------
    #
    # 上面那条 `sched-selfcheck-rc1-not-alarm` 曾因**宿主机状态**而失效：
    # 2026-10-10 08:36 重启把 `marketselfcheck` 的 `last exit code` 清零成
    # `(never exited)`，rc 判定分支整段不可达 → 该变异体**永久漏网**（129/1）。
    # 现由 `MW_LAUNCHCTL` 夹具供给恒定状态（见 make_sandbox），三支判定恒可达。
    #
    # ① 一次性任务的 rc=1 是**结论**、rc=2 是**自检自身坏了**（必须报）——
    #    这两条此前一条漏网、一条从未被行为测过。
    ("sched-selfcheck-rc2-alarm",
     "__DATA_LC_RC2__",
     "", "",
     "[定时任务] 应报「自身异常退出（rc=2）」—— 自检自己坏了那次的结论无效",
     "自身异常退出"),

    # ② 常驻任务的 `rc=3`（webui 的自重启）是**设计内**，但只在**进程活着**
    #    （或正处于被拉起前的空窗）时成立；一旦进程死了，rc=3 就是真故障。
    #    夹具默认把 webui 设成「rc=3 且无进程」= 空窗态 —— 于是把
    #    `_RESIDENT_OK_RC` 查表打断，就会落到"非零 + 无进程"的真故障分支。
    ("sched-resident-rc-forgotten",
     "selfcheck.py",
     "_RESIDENT_OK_RC.get(label, ())",
     '_RESIDENT_OK_RC.get(label + "_x", ())',
     "[定时任务] 常驻任务 rc=3 且无进程应报「反复起、反复失败」",
     "反复起、反复失败"),

    # ============ 第五十五轮：`a/b` 的日期误伤 ============
    #
    # 真缺陷（2026-10-10 查明）：`STATE_RE` 的 `a/b` 那一支无条件命中，
    # 而 `M/D` 形式的日期是行情播报高频写法（「自 9/30 至 10/7」）。
    # 后果不只是少推一条：`produce_text` 命中后会**再发起一次完整 AI
    # 调用**去重写（还把「至 10/7」当敏感词写进重写 prompt），
    # 重写仍不过就退化成兜底模板 —— 一条正常播报因写了日期丢掉 AI 分析。
    #
    # 修法：`至`/`到` 从 `a/b` 那一支的限定词里摘掉（它们正是日期区间的
    # 介词，歧义全由它们引入），`减/降/占/约/留` 等无歧义词保留。
    # 判据是 [安全闸门] 的 ALLOW 列表（5 条日期串必须放行）。
    ("gate-fraction-date-falsepos",
     "marketlib.py",
     '        if _n < _d and _d <= 12:\n'
     '            return _m.group(0)',
     '        return _m.group(0)   # 注入：不再做数值过滤',
     "[安全闸门] `a/b` 分数必须按「分子<分母 且 分母≤12」过滤日期"
     "（摘掉后「近 10/8」「为 10/23」被误拦 → 每次白付一轮 AI 重写）",
     "误拦 近 10/8"),

    # 反方向：**不能**为了修日期误伤而把分数一起放过 ——
    # 把 `a/b` 那一支整个弄成永不匹配，断言 BLOCK 里的分数用例报出来。
    # ⚠️ 锚点 2026-10-26 随 `_STATE_AB_RE` 拆出而更新（原先锚在 STATE_RE
    # ① 支的两行 `r"|..."`，该支已摘出成独立正则 + 数值过滤函数）。
    # ⚠️ 2026-10-26 二次更新：`_STATE_AB_RE` 扩成两支四组（动作仓词 /
    # 仓位动词+至到），锚点改为 `_ST_AMBIG` 那一行 ——【由上到下】匹配这一段。
    ("gate-fraction-branch-dead",
     "marketlib.py",
     '    r"(?:(?:" + _ST_AMBIG + r"|" + _ST_ACTION + r")" + _ST_SEP\n'
     '    + r"([0-9０-９]{1,2})" + _ST_SEP + r"[/／]" + _ST_SEP\n'
     '    + r"([0-9０-９]{1,2})"',
     '    r"(?:(?:NEVER_MATCH_XYZ|" + _ST_ACTION + r")" + _ST_SEP\n'
     '    + r"([0-9０-９]{1,2})" + _ST_SEP + r"[/／]" + _ST_SEP\n'
     '    + r"([0-9０-９]{1,2})"',
     "[安全闸门] 应报「减 1/3」等仓位分数被放行",
     "❌ 拦截[仓位分数(减 2/5)]"),

    # --- a/b 分数的分隔符容忍被退回（[安全闸门] 应抓加粗/全角绕过）---
    # 2026-10-25 加：`减**1/3**`、`减 1／3` 原先**直接放行**（存量绕过面）。
    # 根因两层：`_mask` 把 `/` 剥掉 → 规范化文本里 a/b 分支恒不可达；
    # 原文侧 `\s*` 又不吃 `*`。修法是把两侧空白放宽成 `_ST_SEP`
    # （markdown 强调符 + 词内分隔符）并吃全角斜杠。
    # 本变异体把该容忍退回 `\s*` + 半角斜杠 —— 若闸门没被自检守住，
    # 那四条 BLOCK 用例会全部「漏网!」。
    # ⚠️ 锚点 2026-10-26 随 `_STATE_AB_RE` 拆出而更新；同日二次更新为
    # 两支四组结构，锚点改成 `_ST_SEP` 那三行（第一支 + 第二支同时退）。
    ("gate-fraction-sep-tolerance-dropped",
     "marketlib.py",
     '    r"(?:(?:" + _ST_AMBIG + r"|" + _ST_ACTION + r")" + _ST_SEP\n'
     '    + r"([0-9０-９]{1,2})" + _ST_SEP + r"[/／]" + _ST_SEP\n'
     '    + r"([0-9０-９]{1,2})"\n'
     '    r"|" + _ST_VERB_TO + _ST_SEP + r"([0-9０-９]{1,2})" + _ST_SEP\n'
     '    + r"[/／]" + _ST_SEP + r"([0-9０-９]{1,2}))")',
     '    r"(?:(?:" + _ST_AMBIG + r"|" + _ST_ACTION + r")" + r"\\s*"\n'
     '    + r"([0-9０-９]{1,2})" + r"\\s*" + r"[/／]" + r"\\s*"\n'
     '    + r"([0-9０-９]{1,2})"\n'
     '    r"|" + _ST_VERB_TO + r"\\s*" + r"([0-9０-９]{1,2})" + r"\\s*"\n'
     '    + r"[/／]" + r"\\s*" + r"([0-9０-９]{1,2}))")',
     "[安全闸门] 仓位分数加粗/全角斜杠仍须拦住"
     "（`_mask` 剥斜杠致规范化不可达，原文侧再不吃 `*` 就是一键绕过）",
     "❌ 拦截[仓位分数(加粗拆断)]"),

    # ⚠️ 2026-10-26 新增：**动作仓词 / 仓位动词+至到** 的分支被摘掉。
    # 实测漏网（只锚 `_ST_AMBIG` 单字时限定词）：`减仓 1/3`、`建仓 1/4`、
    # `加仓 1/2`、`港股科技减仓 1/3`、`调仓至 1/2` **全部放行** ——
    # 单字「减」与分数之间隔了个「仓」字，`(?:减)\s*1` 接不上。
    # 这两类后面跟 a/b 分数必然是仓位（「加仓 1/2」不可能是日期），
    # 纯收紧、零歧义。把 `_ST_ACTION` 与 `_ST_VERB_TO` 清空即回到漏网态。
    ("gate-fraction-verb-fraction-dropped",
     "marketlib.py",
     '_ST_ACTION = r"减仓|加仓|建仓|清仓|调仓|补仓|平仓|空仓|满仓|重仓"',
     '_ST_ACTION = r"XXX_NO_ACTION_XXX"',
     "[安全闸门] 动作仓词后的 a/b 分数仍须拦住"
     "（`减仓 1/3` 里「仓」隔开单字「减」，单字限定词接不上）",
     "❌ 拦截[仓位分数(动作仓词)]"),

    ("gate-fraction-verb-to-dropped",
     "marketlib.py",
     '_ST_VERB_TO = (r"(?:减|降|留|剩|用|投|加|建|清|调|补|平)仓?"\n'
     '               r"\\s*[至到]\\s*")',
     '_ST_VERB_TO = r"(?:NEVER_MATCH_XYZ)"',
     "[安全闸门] 仓位动词 + 至/到 + a/b 分数仍须拦住（`调仓至 1/2`）",
     "❌ 拦截[仓位分数(仓位动词+至)]"),

    # 2026-10-26 反方向：`_ST_VERB_TO` 的动词表**收窄到仓位动词**，
    # 「涨至 1/5」「收在 1/5」是行情描述、必须放行。把它放宽成
    # 「任意单字 + 至/到」即回到 2026-10-10 的假阳性态。
    ("gate-fraction-verb-to-overbroad",
     "marketlib.py",
     '_ST_VERB_TO = (r"(?:减|降|留|剩|用|投|加|建|清|调|补|平)仓?"\n'
     '               r"\\s*[至到]\\s*")',
     '_ST_VERB_TO = r"." + r"\\s*[至到]\\s*"',
     "[安全闸门] 行情动词 + 到/至 + a/b 不得误拦"
     "（放宽成任意单字即误伤「涨至 1/5」，每条行情播报白付一轮 AI 重写）",
     "误拦 涨至 1/5"),

    # ═══ 2026-10-14 09:19 实测（盘前简报「涨跌家数约两成」被拦）后补的两条 ═══
    # STATE_RE 的成/分之/位数三支改为仓语境共现（(?=[子句]*[仓敞]) + 任意
    # 非标点前缀）。「涨跌家数约两成」这类市场约数放行，「底仓约五成」仍拦。

    # --- 仓语境前瞻被删（[安全闸门] ALLOW 应抓）---
    # 把前瞻改成恒真 lookahead（空断言），三支退回无条件命中 →
    # 「涨跌家数约两成」等 4 条市场约数被误拦 → ALLOW 用例红。
    ("state-ctx-lookahead-removed",
     "marketlib.py",
     '_STATE_CTX = r"(?=[^，。；！？]*[仓敞])"',
     '_STATE_CTX = r"()"  # 注入：仓语境前瞻失效',
     "[安全闸门] 市场约数（涨跌家数约两成等）被仓位分数正则误拦",
     "误拦 涨跌家数约两成"),

    # --- 占比支被删（[安全闸门] BLOCK 应抓）---
    # 「占比约六成，计划降到四成」无仓字，全靠独立的占比支拦。
    # 删掉该支 → 这条 memory 实录泄漏形态漏网 → BLOCK 用例红。
    ("state-zhanbi-branch-removed",
     "marketlib.py",
     '    r"|占比\\s*(?:约|近|超)?\\s*" + _ST_NUM + r"{1,4}\\s*(?:成|分之)"',
     "    # 注入：占比支已删",
     "[安全闸门] 「占比约六成」无仓字泄漏形态被放行",
     "❌ 拦截[仓位分数(占比约六成)]"),

    # ═══ 2026-10-13 11:35 实测（156 字「上午小结」过闸）后补的四条 ═══
    # 现场：AI 只写触发+盘面（156 字），缺关注板块/建议/热点全部段。
    # 旧闸门「<60 字 或 <2 标签」恰好放行（标签=2）。修法是 `_struct_reason`
    # 加必带段判据 + 带缺段提示重写一次。下面四条各自覆盖一个环节。

    # --- 必带段判据被删（[兜底模板与结构闸门] 应抓）---
    # 删掉「缺必带段」这一支：`_struct_reason` 退回只看字数与标签数，
    # 11:35 那种薄输出会**再次过闸**（标签=2、156 字 >60）。
    ("struct-must-have-boards-gone",
     "analyze.py",
     '            return "缺必带段「关注板块」（brief 给了板块数据）"',
     '            return None  # 注入：不再校验必带段',
     "[兜底模板与结构闸门] 薄输出缺「关注板块」时必须判结构不完整（11:35 实测）",
     "薄输出（缺关注板块）未被 _struct_reason 判出"),

    # --- 不合格时不再重写、直接退兜底（[兜底模板与结构闸门] 应抓）---
    # 把重写调用换成 pass：薄输出 → 直接兜底。断言「恰好一次重写」会落空。
    # ⚠️ 锚点 2026-10-19 更新：`_struct_reason` 加了 `slot` 参数（关键位
    #    必带按槽位生效），旧锚点 `_struct_reason(txt, brief)` 匹配 0 次 →
    #    该变异体变成空操作、报「跳过」。
    ("struct-rewrite-skipped",
     "analyze.py",
     '    if ok and txt:\n        _rs = _struct_reason(txt, brief, slot)\n        if _rs:',
     '    if ok and txt:\n        _rs = _struct_reason(txt, brief, slot)\n        if False and _rs:',
     "[兜底模板与结构闸门] 结构不合格时应先带缺段提示重写一次，而非直接兜底",
     "薄输出应触发恰好一次重写"),

    # --- brief 空段时**也**重写（[兜底模板与结构闸门] 应抓）---
    # 反方向：把「（本段无数据）」的豁免去掉 —— brief 没给板块数据时
    # 仍要求必带段，于是盘前/接口全挂那轮会被无谓重写一次（浪费一次调用）。
    ("struct-empty-section-still-required",
     "analyze.py",
     '        if "（本段无数据）" not in _bsec and not any(\n'
     '                _l.lstrip().startswith("**关注板块")\n'
     '                for _l in txt.split("\\n")):',
     '        if not any(\n'
     '                _l.lstrip().startswith("**关注板块")\n'
     '                for _l in txt.split("\\n")):',
     "[兜底模板与结构闸门] brief 空段（本段无数据）时不该要求必带段、更不该重写",
     "brief 空段时不该重写"),

    # --- 兜底模板缺热点段（[兜底模板与结构闸门] 应抓）---
    # 把兜底侧新增的热点段整块删掉 → AI 超时那轮读者看不到热点段，
    # 且尾部观测会把模板缺失归因成 AI 缺失（09:59/10:15 实测）。
    ("fallback-hot-section-gone",
     "analyze.py",
     '        if _hl:\n'
     '            _hot_txt = ("**热点**（算法选出）\\n" + "\\n".join(_hl) + "\\n\\n")',
     '        if _hl:\n'
     '            _hot_txt = ""  # 注入：兜底模板不再渲染热点段',
     "[兜底模板与结构闸门] 兜底模板必须也有热点段（AI 超时那轮读者看不到）",
     "兜底模板缺热点段"),

    # --- digest 不再声明「不豁免必带段」（[兜底模板与结构闸门] 应抓）---
    # 11:35 缺段的疑似诱因：五条已推摘要让 AI 把「避免重复」读成「可以不写」。
    # 源级锚断言这句说明必须在。
    ("digest-exempt-note-gone",
     "analyze.py",
     '                + "（「避免重复」只豁免措辞与已说过的结论，**不豁免必带段**：\\n"',
     '                + ""',
     "[兜底模板与结构闸门] digest 必须声明「避免重复不豁免必带段」（11:35 缺段疑似诱因）",
     "digest 未声明"),

    # --- 前言剥除（[AI 输出净化] 应抓）---
    # `[实测]` 2026-10-19 10:23:27 把
    # 「根据规则手动计数（约 310 字），稍作精简后给出正文：」推进了群。
    # 把剥除函数改成恒等 → 前言重新泄漏。
    ("preamble-strip-noop",
     "analyze.py",
     '    if _i >= len(lines) or _is_head(lines[_i]):',
     '    return t  # 注入：不再剥前言\n'
     '    if _i >= len(lines) or _is_head(lines[_i]):',
     "[AI 输出净化] AI 正文前的元叙述（计数前言/粘连标点）必须被剥除",
     "前言行处理错误"),

    # --- 前言剥除没接线（[AI 输出净化] 应抓）---
    # 函数本身没坏，但 ai_analyze 忘了调用 —— "修了函数没接线"。
    # AST 接线判据必须抓住。
    ("preamble-strip-unwired",
     "analyze.py",
     '        _out = _strip_preamble(\n'
     '            _strip_meta(_strip_think(_strip_fence((_so or "").strip()))))',
     '        _out = _strip_meta(_strip_think(_strip_fence((_so or "").strip())))',
     "[AI 输出净化] ai_analyze 必须真的调用 _strip_preamble（AST 接线判据）",
     "未调用"),

    # --- 启动榜被塞回 `if _strong:` 块内（[涨跌榜] 应抓）---
    # 与涨跌榜那次同款：strong 为空时启动榜跟着消失（两个独立数据源）。
    # 变异体把启动榜的渲染重新嵌回 `if _sl:` 块内。
    ("start-board-tied-to-strong",
     "analyze.py",
     '        _start = hot.get("start")',
     '        _start = [] if not _sl else hot.get("start")  # 注入：strong 空则启动榜一起丢',
     "[涨跌榜] strong 为空时启动榜仍须渲染（两榜是独立数据源）",
     "strong 为空时启动榜"),

    # --- 热点段行被误报「串行」（[输出格式规范化] 应抓）---
    # 兜底热点段落地后，热点行（60日x%/量比/占比，天然多 %）每轮被
    # `_norm_sectors` 的「≥2 个 %」判据误报 3~5 条假日志。修复是给热点段
    # 加豁免；本变异体把豁免删掉（无条件报串行），检查项必须抓到。
    ("hot-block-serial-log-back",
     "analyze.py",
     '        if _pct_outside_paren(_s) >= 2:\n'
     '            if _in_hot_block:\n'
     '                out.append(ln); continue\n'
     '            _log(f"板块行疑似串行（含多个%），规范化跳过: {_s[:50]!r}")',
     '        if _s.count("%") >= 2:\n'
     '            _log(f"板块行疑似串行（含多个%），规范化跳过: {_s[:50]!r}")',
     "[输出格式规范化] 热点段行不得被误报「板块行疑似串行」（每轮 3~5 条假日志）",
     "热点段行被误报"),

    # --- 启动板块段不被豁免（[输出格式规范化] 应抓）---
    # 2026-10-20 加：启动板块行（60日/量比/主力占比，天然 3~4 个 %）与
    # 热点行同形，豁免名单只写「热点」时每轮 3~5 条假日志
    # （10-20 实测 31 条里 24 条）。变异体把「启动板块」从豁免里删掉。
    ("start-block-serial-log-back",
     "analyze.py",
     '            _in_hot_block = ("热点" in _s or "启动板块" in _s)',
     '            _in_hot_block = ("热点" in _s)',
     "[输出格式规范化] 启动板块段行不得被误报「板块行疑似串行」"
     "（10-20 实测 31 条假日志里 24 条是启动板块行）",
     "启动板块行被误报"),

    # --- 括号内 % 计数回退成整行计数（[输出格式规范化] 应抓）---
    # 2026-10-20 加：`存储芯片 +1.34%（-37.6 亿，自日内高点 -1.93%）` 是
    # **格式正确**的一行，括号里的 % 不是串行。变异体把判据改回
    # `_s.count("%")`，正确行重新被记假日志。
    ("paren-pct-count-whole-line",
     "analyze.py",
     "        if _pct_outside_paren(_s) >= 2:",
     '        if _s.count("%") >= 2:',
     "[输出格式规范化] 括号内注记 % 不得算进「串行」判据"
     "（正确格式的板块行被记假日志，10-20 实测 31 条里 6 条）",
     "括号内含 % 的正确板块行被误报"),

    # --- _pct_outside_paren 整个失效（[输出格式规范化] 应抓）---
    # 变异体让助手函数恒返回 0：真串行（两个板块挤一行）从此不报，
    # "规范化失效可见性"整条静默消失。
    ("pct-outside-paren-always-zero",
     "analyze.py",
     '    return re.sub(r"[（(][^）)]*[）)]", "", s).count("%")',
     '    return 0   # 注入：串行判据恒 False',
     "[输出格式规范化] 关注板块真串行必须仍被记日志（判据函数被改坏）",
     "串行"),
    # ===== agent 侧（2026-10-17，用户「要」）：目标 agent/src/mw_agent/__main__.py =====
    # 探针：_run_agent_probe —— 沙箱自检新增 [agent 摘要链路] 段，import 沙箱里
    # 的 agent 模块（补桩 claude_agent_sdk —— 沙箱 venv 里没有 SDK）、
    # 用假文本**调用真推送路径** _push_summary（webhook 置空挡在 curl 之前，
    # 纯函数推导、不进行为，变异天然打不掉）。新增段 → [检查项可失败性]
    # 报孤儿标记；段内 ❌ → 行粒度判据与 must_contain 双闸。
    ("agent-gate-weekend-run",
     "agent/src/mw_agent/__main__.py",
     "        if not _MLTD.is_trading_day(_d, warn=False):\n            return _GATE_HOLIDAY",
     "        if _d.isoweekday() > 4:\n            return _GATE_HOLIDAY",
     "[agent 摘要链路] 节假日闸门改看自己的 isoweekday → 周末有判、"
     "HOLIDAYS 表内休市日放行（交易日判定必须走 marketlib 单一真值）",
     "国庆休市日"),
    ("agent-think-strip-broken",
     "agent/src/mw_agent/__main__.py",
     "        text = _AN._strip_think(text)\n        text = _AN._strip_meta(text)",
     "        pass",
     "_push_summary 的推理块/元说明剥除被摘 → think 块原样进群"
     "（2026-10-16 16:19 生产实测那条泄漏的回归）",
     "推理块未被剥离"),
    ("agent-meta-strip-broken",
     "agent/src/mw_agent/__main__.py",
     "        text = _AN._strip_meta(text)",
     "        pass",
     "_strip_meta 调用被摘 → 正文后的「写作说明」附加块进群"
     "（[[market-watch-ai-output-leak-20261009]] 的 agent 侧）",
     "元说明块未被剥离"),
    ("agent-fmt-noop",
     "agent/src/mw_agent/__main__.py",
     "    _body = []\n    for _p in _paras:\n        _p = _FMT_META_LEAD.sub(\"\", _p).strip()",
     "    return t   # 注入：排版兜底整段失效\n"
     "    _body = []\n    for _p in _paras:\n        _p = _FMT_META_LEAD.sub(\"\", _p).strip()",
     "_fmt_summary 直接放行 → prompt 说不听时代码侧无排版兜底，"
     "整段单块照发",
     "分段兜底失效"),
    # ⚠️ 2026-10-26：_fmt_summary 重写为「间隔行 + 段首加粗 + 元标签剥除」，
    # 原锚点 `if "\n\n" in t or len(t) < 200:` 已随重写消失（锚点漂移即静默
    # 跳过，见 [[market-watch-mutation-anchor-drift]]），换新锚点并加三条
    # 排版专项变异体。
    ("agent-fmt-gap-dropped",
     "agent/src/mw_agent/__main__.py",
     "    return (\"\\n\" + _FMT_GAP + \"\\n\").join(_body)",
     "    return \"\\n\\n\".join(_body)",
     "段间间隔行退回裸 \\n\\n → 企微折叠连续空行，① ② ③ 在群里挤成一坨"
     "（2026-10-26 用户反馈的原始缺陷）",
     "段间缺全角空格间隔行"),
    ("agent-fmt-bold-lead-dropped",
     "agent/src/mw_agent/__main__.py",
     "    if not p or p.startswith((\"**\", \">\", \"#\", \"-\", \"*\", \"|\")):",
     "    if True:",
     "_fmt_bold_lead 被整体去能 → 段首标签不再加粗，整段等权重灰字",
     "段首标签未加粗"),
    ("agent-fmt-meta-lead-kept",
     "agent/src/mw_agent/__main__.py",
     "        _p = _FMT_META_LEAD.sub(\"\", _p).strip()",
     "        _p = _p.strip()   # 注入：不剥元标签",
     "元标签剥离被摘 → 模型写在正文开头的 `summary.` 原样进群"
     "（2026-10-26 16:24 生产实测）",
     "元标签未被剥离"),
    # ===== 调研播报排版（2026-11-05 用户「优化调研播报排版」）=====
    # 生产实证：模型把 ①③④ 接在上一句句号后、写在同一行 —— 切不开。
    ("agent-fmt-inline-enum-unsplit",
     "agent/src/mw_agent/__main__.py",
     "    _paras = [s for p in _paras for s in _fmt_split_enum(p)]",
     "    pass",
     "行内枚举不再切段 → 「…记忆已写入。① 盘面…③…④…」挤成一坨"
     "（2026-11-05 16:21 生产推送用户截图的那种排版）",
     "行内枚举未切段"),
    ("agent-fmt-nested-enum-split",
     "agent/src/mw_agent/__main__.py",
     "        if _o is None or _o <= _max:",
     "        if _o is None:",
     "只认递增标记 → ④ 段内部重起序号的列举（①恒科守…②上证…）被切成独立段",
     "嵌套列举被误切"),
    ("agent-fmt-sentence-break-dropped",
     "agent/src/mw_agent/__main__.py",
     "    if len(p) < _FMT_LONGLINE:",
     "    if True:",
     "长段不再按句换行 → >120 字的段仍是一整行，移动端一屏读不完",
     "长段未按句换行"),
    ("agent-quality-gate-removed",
     "agent/src/mw_agent/__main__.py",
     "    _iss = []\n    t = (text or \"\").strip()\n    if len(t) < 80:",
     "    _iss = []\n    t = (text or \"\").strip()\n    if False:",
     "_summary_quality 判据被去能 → 过短/无数字的退化摘要照推",
     "过短（<80 字）"),
    ("agent-quality-nodigit-removed",
     "agent/src/mw_agent/__main__.py",
     "    if not re.search(r\"\\d\", t):",
     "    if False:  # 注入：无数字判据失效",
     "_summary_quality 的「无数字」判据失效 → 零事实（纯定性词）播报照推",
     "无数字（零事实）"),
    ("agent-quality-warn-unwired",
     "agent/src/mw_agent/__main__.py",
     "        _warn = _summary_warn(_body, dropped=_LAST_SAFE_DROPPED)",
     "        _warn = []  # 注入：质量告警不再产生",
     "[调研 Agent] 摘要质量告警被摘 → 缺归因证据/缺昨日验证的退化报告"
     "不再留痕（2026-11-04 用户反馈「报告不专业」的观测缺口）",
     "摘要质量告警未接到 _prepare"),
    ("agent-pick-think-candidate",
     "agent/src/mw_agent/__main__.py",
     "    for _blk in reversed(blocks):",
     "    for _blk in blocks and [blocks[-1]]:",
     "倒序选块退化成只看末块 → 末块是纯推理块时剥完为空、"
     "触发「AI 无任何文本产出」误回滚当日标记",
     "纯推理末块被当成摘要"),
    # ⚠️ 2026-10-20：Agent 的收盘数字曾整段取自**东财网页缓存快照**
    # （抓到 10-13 的收盘价，写出「PCB +6.08%、沪电 +5.73%」）。修法是
    # 新增 MCP 工具 get_snapshot（读主系统 collect 落的 pending.json）。
    # 这条变异体测「get_snapshot 不再校验日期」→ 拿到非当天的快照也照答，
    # Agent 会把过期收盘值当今天用（与网页缓存同一类错）。
    ("agent-snapshot-date-check-dropped",
     "agent/src/mw_agent/__main__.py",
     '    if not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", want):\n'
     '        return {"content": [{"type": "text", "text": "(非法 date，需 YYYY-MM-DD)"}]}\n',
     '    if not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", want):\n'
     '        want = time.strftime("%Y-%m-%d")\n',
     "[agent 摘要链路] get_snapshot 放弃日期校验 → 非当天的快照也返回，"
     "过期收盘值会被当成今日写入 plan/摘要（2026-10-20 网页缓存事故同型）",
     "非法日期未拒答"),

    # --- 快照港股行不再标「盘中值」（[agent 摘要链路] 应抓）---
    # 2026-10-20 加：pending.json 采集止于 15:05、港股 16:00 才收盘 ——
    # 港股行永远是盘中值。不标注时 Agent 把 15:03 的恒生科技当收盘值
    # 写进摘要（当天真实收盘 4045.91 vs 快照 4057）。
    ("agent-snapshot-hk-intraday-tag-dropped",
     "agent/src/mw_agent/__main__.py",
     '            if str(code).startswith(("100.", "116.", "124.")):\n'
     '                extra += "（港股盘中值，非收盘）"\n',
     '',
     "[agent 摘要链路] 快照港股行不标「盘中值」→ 15:05 的盘中价被当收盘价"
     "写进摘要（2026-10-20 实测恒科 4057 vs 真实收盘 4045.91）",
     "港股盘中值"),

    ("agent-syspath-root-missing",
     "agent/src/mw_agent/__main__.py",
     "if _MW_SRC not in sys.path:\n    sys.path.append(_MW_SRC)\n",
     "",
     "[agent 摘要链路] 摘掉主根入 sys.path → 生产 sys.path 不含主根，"
     "marketlib/analyze 全部 ModuleNotFoundError，三处 try/except 静默退化："
     "交易日门放行（周末/节假日照跑照推）、_mask 退回原文匹配、"
     "_strip_meta 退回只剥 think。2026-10-17 周六 16:47 生产真跑并推群的根因",
     "休市闸门失效：周六未拦"),

    # -------------------- 2026-10-25：agent 侧三处 P1 --------------------
    # 共同背景：agent 是**第二个推送出口 + 第二个配置写入方**，原先这三处
    # 都比主系统弱一层，或用了"看起来成功"的假判据。

    # ① plan.md 关键位同步对**全文** count=1 替换 —— `## 当前市场位置`
    #    段排在前面且叙述里会写「上方 **N**」，替换改错行**且返回成功**。
    #    ⚠️ 注入必须**精确还原旧行为**（`_body=_old` + 段界退化为全文），
    #    只换 `_body` 一行会把全文拼进段切片、文件被复制三倍 —— 首个 ❌
    #    落在旧断言「改动了正文其它内容」上，判据文案对不上（实测假漏网）。
    ("agent-plan-sync-wholefile",
     "agent/src/mw_agent/__main__.py",
     '    _span = _plan_section_span(_old, "关键位")\n'
     '    if _span is None:\n'
     '        return "plan.md 里**找不到「## 关键位」段**（标题被改过）—— 需人工同步"\n'
     '    _lines = _old.split("\\n")\n'
     '    _s, _e = _span\n'
     '    _body = "\\n".join(_lines[_s:_e])',
     '    _lines = _old.split("\\n")\n'
     '    _s, _e = 0, len(_lines)   # 注入：段界退化为全文\n'
     '    _body = _old',
     "[agent 摘要链路] 关键位同步退回全文范围 → 改到「当前市场位置」段的"
     "叙述行却返回成功，配置与 plan 继续漂移（2026-10-25 审查 P1）",
     "关键位同步改到了「当前市场位置」段里的叙述行"),

    # ② 摘要闸门退回本地词表（不调主系统 safe()） —— 漏掉五个结构正则、
    #    _mask 规范化与代理码点。⚠️ 只换 `return _ML.safe(s)` 那一行：
    #    把整个 if 块换成 `if False:` 会留下无体的 if（IndentationError，
    #    实测探针 rc=1、报错指向语法而非行为）。
    ("agent-summary-gate-local-only",
     "agent/src/mw_agent/__main__.py",
     # ⚠️ 2026-11-06：`_chk()` 从「直接 return 主闸门结果」改成
     # 「主闸门不过就返回，过了继续查本地词表」后，原锚点已不存在。
     # 病态不变：把主闸门整体短路，只剩本地词表。
     "                if not _ok:\n"
     "                    return False, _h",
     "                if False:   # 注入：摘掉主系统闸门\n"
     "                    return False, _h",
     "[agent 摘要链路] 摘要闸门退回本地词表 → 窗口措辞/个人金额/拆字绕过"
     "全部漏网（agent 是第二个推送出口，守卫不该弱于主系统）",
     "窗口措辞未被拦"),

    # ③ DRY_RUN 不挡 agent 推送 —— 人工暂停开关只关了半扇门。
    ("agent-dry-flag-ignored",
     "agent/src/mw_agent/__main__.py",
     "    if _dry_flag_active():",
     "    if False:  # 注入：不查 DRY_RUN",
     "[agent 摘要链路] agent 推送不查 DRY_RUN → 人工暂停期间仍往群里发"
     "（2026-10-25 审查 P1；用户铁律：只有生产可推）",
     "DRY_RUN 生效时 _push_summary 仍报告推送成功"),

    # -------------------- 2026-11-02：Read 路径白名单（P2，实测） --------------------
    # 背景：`_read_guard_paths` 原先放行整个 conf/ 目录 → config.json
    # （真实 webhook key）与 private-notes.md（个人持仓）可被 Read 读出。
    # 探针 [_run_agent_probe] 已加双向断言（plan/sectors 放行、config.json
    # 与 /etc/passwd 拒绝）。这两条锁住"白名单被放宽"与"守卫被去能"。
    ("agent-read-guard-conf-whitelisted",
     "agent/src/mw_agent/__main__.py",
     '        os.path.join(MW_DIR, "state"),\n'
     '        os.path.join(MW_DIR, "agent"),\n',
     '        os.path.join(MW_DIR, "conf"),\n'
     '        os.path.join(MW_DIR, "state"),\n'
     '        os.path.join(MW_DIR, "agent"),\n',
     "[agent 摘要链路] Read 白名单放宽到整个 conf/ → config.json（真实 "
     "webhook key）可被注入页面诱导读出外带，与本钩子的目的直接矛盾",
     "Read 白名单放行了 conf/config.json"),

    ("agent-read-guard-always-allow",
     "agent/src/mw_agent/__main__.py",
     '        print(f"⛔ Read 守卫拦截越界路径: {_raw!r}", file=sys.stderr)\n'
     '        return {"hookSpecificOutput": {\n'
     '            "hookEventName": "PreToolUse",\n'
     '            "permissionDecision": "deny",',
     '        print(f"⛔ Read 守卫拦截越界路径: {_raw!r}", file=sys.stderr)\n'
     '        return {"hookSpecificOutput": {\n'
     '            "hookEventName": "PreToolUse",\n'
     '            "permissionDecision": "allow",',
     "[agent 摘要链路] Read 守卫恒 allow（越界路径照放） → 白名单形同虚设，"
     "任意路径（/etc/passwd、~/.claude 凭据）都可被读",
     "Read 白名单放行了 /etc/passwd"),

    # -------------------- 2026-11-02：MCP 工具 schema / 注解规范化 --------------------
    # 背景：九个 mw MCP 工具原先用短式 dict schema（`{"query": str, ...}`），
    # 而 SDK 的 `_build_input_schema` 会把**每个键**塞进 required —— 带默认值
    # 的可选参数变成必填，且没有 description / 取值约束 / additionalProperties。
    # 2026-11-02 改为完整 JSON Schema + MCP 注解（readOnly/destructive/
    # idempotent/openWorld）+ 服务器 version。探针里已加源码层形状断言
    # （@tool 数、additionalProperties 数、required 形态、enum、注解数、
    # version）。这两条锁住"退回短式"与"注解丢失"两个方向。
    ("agent-mcp-schema-optional-required",
     "agent/src/mw_agent/__main__.py",
     '     "required": ["query"],\n     "additionalProperties": False},\n'
     '    annotations=_ANN_READ_WEB,\n)\nasync def web_search(args):',
     '     "required": ["query", "max_results"],\n'
     '     "additionalProperties": False},\n'
     '    annotations=_ANN_READ_WEB,\n)\nasync def web_search(args):',
     "[agent 摘要链路] web_search 的 schema 把可选参数 max_results 列进 "
     "required → 模型必须传它，短式 dict schema 的老毛病回来了",
     # ⚠️ must_contain 必须取自**探针实际报出的那句话**（本项目的死法：
     # 锚在源码片段上 → 输出里根本不含它 → 判为漏网）。探针的失败文案是
     # 「web_search 的 required 不含 max_results 的形式 —— 可选参数被当必填…」。
     "可选参数被当必填"),

    ("agent-mcp-annotation-dropped",
     "agent/src/mw_agent/__main__.py",
     '     "required": ["key", "value", "reason"],\n'
     '     "additionalProperties": False},\n'
     '    annotations=_ANN_WRITE,\n)\nasync def update_threshold(args):',
     '     "required": ["key", "value", "reason"],\n'
     '     "additionalProperties": False},\n'
     '    annotations=None,\n)\nasync def update_threshold(args):',
     "[agent 摘要链路] update_threshold 丢了 MCP 注解 → 客户端读不到"
     "\"这是写操作\"，只读/写入语义对上层不可见（拒绝/确认策略无从判断）",
     "个 @tool 带 MCP 注解"),

    # -------------------- 2026-10-26 夜：慢轮吞调度 / 状态写坏 --------------------
    # 三处修复各自的守护。背景（当日生产实证）：
    #   · 11 次触发被 launchd 丢弃（占盘内有效轮次 20%），09:25 那轮
    #     AI 跑 1171s、整轮 1313s，期间 4 次触发丢失；
    #   · 15:45:42 state.json 被写成缺 inflight_ts 的形态、fired 被清空，
    #     同日 14:30 槽位重复推送两次。
    ("ai-round-budget-ignored",
     "analyze.py",
     "            total_limit=_budget,\n",
     "",
     "[AI 调用看门狗] 单轮 AI 不再有总时长上界 → 慢轮无限期吞掉后续"
     "调度轮次（2026-10-26 实测一天吞 11 次）",
     "没传 total_limit"),
    ("ai-budget-exhausted-trips-breaker",
     "analyze.py",
     # ⚠️ 2026-11-20 锚点同步：该分支中间插入了 `_stall_record`（归因落盘）。
     # 注入体必须**整段**替换 —— 只改 `if` 那一行的话，`_stall_record`
     # 还照常执行，归因被记成 budget-skip，测不出"误判成通道故障"。
     '            if "[budget-exhausted " in (_se or ""):\n'
     '                # 预算在**调用之前**就耗尽了（取数吃光了全轮预算）。\n'
     '                # 与「调用跑到一半被掐」（budget）分开记：前者是\n'
     '                # **连一次 AI 都没发起**，后者是发起过没跑完。\n'
     '                _stall_record(_se, _to, "budget-skip")\n'
     '                return ""',
     '            if False:  # 注入：预算耗尽也走熔断\n                return ""',
     "[AI 通道] 预算耗尽被误判成通道故障 → 一次正常慢轮让后续 10 分钟"
     "全退模板，把省下的时间又赔回去",
     "预算耗尽被误判成通道故障"),
    ("ai-empty-retry-removed",
     "analyze.py",
     # ⚠️ 2026-11-04 锚点同步：重试条件加了"不限制模式直接放行"的
     # `AI_ROUND_BUDGET is None` 分支（用户要求取消 AI 预算），
     # 条件折成两行。锚点必须按源码逐字节照抄。
     "                and (AI_ROUND_BUDGET is None\n"
     "                     or _used_now + 60 <= AI_ROUND_BUDGET)):",
     "                and False):  # 注入：上游断流不再重试",
     "[AI 通道] 上游断流（rc=0 空输出 + stderr 干净）不再立即重试 → "
     "一次瞬时断流就熔断 10 分钟、退兜底模板（10-23 断流率 46%）",
     "❌ 上游断流的典型形态"),
    # ⚠️ 2026-11-04 新语义：AI 预算默认不限制（用户要求）。
    # 本条守护「不限制 ≠ 不调用」—— 注入把 None 当 0s 处理（典型回归
    # 形态：有人重算预算时忘了 None 分支），AI 会再次每轮被"预算耗尽"
    # 跳过。判据是 [AI 调用看门狗] 段的两态探针（不限制模式必须照常调用）。
    ("ai-unlimited-budget-collapsed",
     "analyze.py",
     "        if AI_ROUND_BUDGET is not None:\n"
     "            # 有限预算模式（可恢复）：剩余预算 ≤ 0 时跳过本次调用。\n"
     "            _budget = max(0.0, AI_ROUND_BUDGET - _used)",
     "        _budget = max(0.0, (AI_ROUND_BUDGET or 0) - _used)  "
     "# 注入：不限制按 0s 处理\n"
     "        if True:",
     "[AI 调用看门狗] 不限制模式下已用时间极大时也必须照常调用 —— "
     "注入后 AI 每轮都被「预算耗尽」跳过，永远拿不到完整的思考时间"
     "（2026-11-03/11-04 生产 5 轮零产出退模板的修复正是为它）",
     "不限制模式（AI_ROUND_BUDGET=None）下已用时间极大时"),
    # 总时长闸门掐停 ≠ 通道故障（2026-10-27 生产实证：一天 3 次误熔断）。
    ("ai-total-limit-trips-breaker",
     "analyze.py",
     '            if "[total-limit-gate " in (_se or ""):\n'
     '                _log("总时长闸门掐停且无产出 —— 是我方预算上限，"',
     '            if False:  # 注入：总时长闸门掐停也走熔断\n'
     '                _log("总时长闸门掐停且无产出 —— 是我方预算上限，"',
     "[AI 通道] 总时长闸门掐停被误写成 AI 熔断 → 一次正常慢轮让后续 "
     "10 分钟全退模板（2026-10-27 生产 09:23/10:14/11:39 三次）",
     # ⚠️ 期望串必须是**自检 ❌ 行**里的原文，不是 analyze 的日志文案
     # （2026-10-27：写成 `_log` 的句子 → 检查项明明抓住了却判漏网）。
     "被误写成 AI 熔断"),
    # 流式中断时正文只在 text_delta 里，抽取链必须兜底（否则「已保留部分
    # 产出」是空话，还会把零产出误判成通道故障）。
    ("ai-stream-delta-fallback-dropped",
     "marketlib.py",
     '        elif _ty == "stream_event":',
     '        elif _ty == "stream_event__disabled":  # 注入：不再收 delta 分片',
     "[AI 通道] 流式中断（只有 text_delta、无完整 assistant 块）时抓不到"
     "正文 → 部分产出被丢弃 + 误判零产出写熔断（2026-10-27 生产 3 次）",
     "流式 delta 兜底失效"),
    # 重写失败丢弃首版正文（2026-10-27 生产两次退模板）。
    ("rewrite-fail-discards-first",
     "analyze.py",
     '        if not ok and _first_txt:',
     '        if False:  # 注入：重写失败直接丢首版',
     "[AI 通道] 重写返回空时首版正文被整条丢弃 → 只差一句软词的可用正文"
     "退成兜底模板（2026-10-27 09:23/13:34 两次）",
     "首版正文被丢弃"),
    # PLAN_TENSE_RE 跨句读符误拦市场语言（2026-10-27 生产 13:31）。
    ("plan-tense-crosses-comma",
     "marketlib.py",
     '    r"[^。；，、！？\\n]{0,12}"     # ⚠️ 2026-10-27 修：不跨句读符 —— 防止「已破 20 日低，逢反弹分批减仓」这类市场判断被误拦（2026-10-27 生产三次误熔断根因之一）',
     '    r"[^。；\\n]{0,12}"  # 注入：窗口重新跨逗号',
     "[安全闸门] 计划时态窗口跨逗号 → 「已破20日低，逢反弹分批减仓」"
     "这类市场判断被误拦、白付一轮重写（2026-10-27 生产 13:31）",
     "误拦 已破20日低，逢反弹分批减仓"),
    # 「万亿」负向前瞻（2026-10-27）。
    # ⚠️ 修法演进：先试的是往 `_AMT_CTX` 里加「融资余额/两融/全市场/
    # 缩量/放量」五个词，但那会扩大「同子句有市场词就放行个人金额」的
    # 既有缺口（实测「放量后另留 5万」被放行）。改成在 `万` 分支加
    # `(?!亿)` —— `万` 后紧跟 `亿` 即「万亿」= 市场量级，判据更精确、
    # 且不碰语境表。
    ("amt-wanyi-lookahead-dropped",
     "marketlib.py",
     '    r"|(?:\\d[\\d,]*(?:\\.\\d+)?\\s*(?:万元|万|千元|千))(?!亿)(?![" + _AMT_COMPOUND + r"])"',
     '    r"|(?:\\d[\\d,]*(?:\\.\\d+)?\\s*(?:万元|万|千元|千))"',
     "[安全闸门] 金额闸门缺「万亿」负向前瞻 → 盘面播报的「融资余额 1.8 "
     "万亿」被判成个人金额、白付一轮重写（2026-10-27）",
     # ⚠️ 2026-11-09：安全闸门的首条✗ 实际是「全市场 1.2 万亿」（案例表第一条），
     # 不是注释里写的「融资余额 1.8 万亿」。写错期望文案会让
     # 真正被抓住的突变体被判成漏网（假阳）。
     "误拦 1.2 万"),
    # 代理码点落盘后读取侧不可编码（2026-10-27）。
    ("runlog-surrogate-readback-broken",
     "runlog.py",
     '                        _line2 = line.encode("utf-8", "replace").decode(\n'
     '                            "utf-8", "replace")\n'
     '                        _lb = _line2.encode("utf-8")',
     '                        _lb = line.encode("utf-8", "backslashreplace")',
     "[面板] 代理码点用 backslashreplace 落盘 → json.loads 读回真 Cs 字符 "
     "→ /api/pushes 编码失败退化成空 JSON 且 HTTP 200（审计流水永久不可见）",
     "代理码点仍被还原成真 Cs 字符"),
    # 快讯源故障折叠成空列表（2026-10-27）。
    # ---------- 2026-11-10 审查批次新增代码的变异体 ----------
    # ❌ 原因：这批修复当时**一条变异体都没加**，
    #     导致“心跳/配额预警/代理分账/协议分账/守卡”这些新守卡被拆掉时
    #     测试全绿。每条都对应一个**可实测的真实故障**。
    ("fund-amount-missing-as-zero",
     "analyze.py",
     '        f62 = _num(x.get("f62"))',
     '        f62 = _num(x.get("f62"), 0.0)',
     "[推送] f62 缺失默认成 0 → 推出「主力≈0(占+9.0%)」，"
     "把“金额未知”断言成“资金没动”",
     "缺失 f62 应显示「金额未知」"),
    ("quota-warn-written-outside-lock",
     "marketlib.py",
     '                _wok = _atomic_write_json(self.RATE_FILE, _rec)',
     '                _atomic_write_json(self.RATE_FILE, _rec)\n'
     '                fcntl.flock(lk, fcntl.LOCK_UN)\n'
     '                _rec2 = _load_json(self.RATE_FILE, {}) or {}\n'
     '                _atomic_write_json(self.RATE_FILE, _rec2)',
     "[计数] 预警在解锁后另写一次 → 并发预留时计数被旧快照覆盖",
     # ⚠ 2026-11-12（全量回归）：原匹配串是「一次预留只落盘 1 次 rate.json」，
     # 而自检**从来没有**这条判据 —— 它靠的是 ④「与盘上 rate.json 对账」：
     # 解锁后那次额外写用**旧快照**覆盖盘上 count，于是内存与盘上分叉。
     # `[实测]` 该变异体实际 ❌ 是
     # 「count 与盘上 rate.json 分叉：内存=5 盘上=4（_roll 的跨进程同步已坏）」。
     # 判据本身是准的（分叉正是这个缺陷的危害：计数被旧快照覆盖），
     # 只是期望串写成了"另一种实现方式"，从来没对上过。
     "与盘上 rate.json 分叉"),
    ("locked-update-swallows-write-result",
     "marketlib.py",
     'return bool(_atomic_write_json(path, d, indent=indent))',
     '_atomic_write_json(path, d, indent=indent)\n                return True',
     "[持久化] 写入失败仍返 True → 磁盘满时静默失效",
     "写入失败应返 False"),
    ("news-bad-structure-cached-as-empty",
     "news.py",
     '    if not isinstance(_lst, list):\n'
     '        _NEWS_MEM.update({"ts": now, "limit": limit, "data": None,\n'
     '                          "unavailable": True})\n'
     '        return None',
     '    if not isinstance(_lst, list):\n        _lst = []',
     "[新闻] 主源结构损坏被归一成空列表 → 当成“今天没新闻”缓存",
     "结构损坏应返 None"),
    ("lunch-stale-bypass-no-date",
     "marketlib.py",
     "    if (_lunch[0] <= now.hour * 60 + now.minute < _lunch[1]\n            and t.date() == now.date()):",
     "    if _lunch[0] <= now.hour * 60 + now.minute < _lunch[1]:",
     "[FEED] 午休豁免不限定当天 → 11:45 把**昨日收盘价**当现价放行，"
     "而 11:30 槽位窗口 11:30~11:50 正在盲区内",
     "午休豁免没限定当天"),
    ("ov-fx-date-only-gate-dead",
     "marketlib.py",
     "                if _dtp is None:\n                    _d_only = str(_dt_s).strip()[:10]",
     "                if False:\n                    _d_only = str(_dt_s).strip()[:10]",
     "[FEED] 新浪汇率末列是纯日期，`_ts_parse` 返 None → 整段陈旧判定是死代码",
     "回包日期在 40 天前时应丢弃"),
    ("orphan-counts-all-scripts",
     "webui.py",
     '        _az = [r for r in runs if r.get("script") == "analyze"]',
     '        _az = runs',
     "[面板] collect 只写 end，数全量则 start>end 永不成立 → "
     "被 kill 的轮次告知「launchd 没触发」",
     "归因应指向「进程未正常结束」"),
    ("stall-alert-ignores-running",
     "marketlib.py",
     "    if running:\n        return False\n    try:\n        return float(age_s) > STALL_ALERT_MIN_AGE",
     "    if False:\n        return False\n    try:\n        return float(age_s) > STALL_ALERT_MIN_AGE",
     "[告警] 不看锁 → 实测最长轮次 24.3 分钟 > 阈值 15 分钟，"
     "跑着的时候往企业微信喊「停摆」，且按日去重掩盖当天真故障",
     "持锁时不应告警"),
    ("webui-arrived-judged-missed",
     "webui.py",
     '            status = "pending"',
     '            status = "missed"  # 注入：到点就判漏推',
     "[面板] 到点即判 missed → 10:05 时 10:00 那格（还有 35 分钟窗口）已显示「漏推」",
     "不再产生这些状态词"),
    ("beat-file-perm-relaxed",
     "runlog.py",
     'os.chmod(tmp, 0o600)',
     'os.chmod(tmp, 0o644)  # 注入：心跳文件权限放宽',
     "[状态/日志/锁文件权限] beat.json 权限回退到 644",
     "beat.json 应为 0600"),
    ("proxy-cooldown-no-count-while-cooling",
     "marketlib.py",
     '    if time.time() < _proxy_fail_get(_hk):\n        _proxy_fail_bump(_hk)\n        return None',
     '    if time.time() < _proxy_fail_get(_hk):\n        return None  # 注入：冷却期内不计失败',
     "[网络降级链] 冷却期内不计失败 → 冷却到期计数归零，黑洞主机每 600s 重付一轮",
     "冷却期内也要计"),
    ("members-partial-cached-as-complete",
     "hot.py",
     'if _decl is not None and len(_merged) < _decl:',
     'if False:  # 注入：残缺名单当完整缓存',
     "[热点] 成分股残缺名单被当完整缓存 7 天",
     "残缺名单会被当完整缓存 7 天"),
    ("advice-section-gate-removed",
     "analyze.py",
     'if not (_has_adv or (_has_r and _has_o)):',
     'if False:  # 注入：建议段闸门失效',
     "[推送正文] 「建议/风险/机会」闸门被拆 → 最关键的一段无硬约束",
     "建议段"),
    ("host-key-loses-scheme",
     "marketlib.py",
     'return ("http://" if url.startswith("http://") else "https://") + h',
     'return h  # 注入：健康键丢掉协议',
     "[数据源] HTTP 失败会把 HTTPS 候选一起判死 30 分钟",
     "HTTP/HTTPS 共用同一健康键"),
    ("runlog-short-write-silent",
     "runlog.py",
     '                        _wtot += _n',
     '                        break  # 注入：短写静默截断',
     "[流水] os.write 短写被当成写完 → 半截 JSON 落盘仍返回 True",
     "写入短缺"),
    ("live-stale-never-true",
     "runlog.py",
     '        return (time.time() - float(ts)) > max_age_s',
     '        return False  # 注入：停摆判定永不成立',
     "[告警] 心跳判定永不成立 → 主链路停摆完全没有信号"
     "（2026-11-08 20:23 那 45 分钟零推送零通知正是这种形态）",
     "心跳超过 15 分钟仍判存活"),
    ("live-never-beat-not-stale",
     "runlog.py",
     '        if not isinstance(ts, (int, float)):\n            return True',
     '        if not isinstance(ts, (int, float)):\n            return False  # 注入：从未心跳当正常',
     "[告警] 「从来没跑起来」被当成正常 → launchd 加载失败无声无息",
     "从未有心跳时未判停摆"),
    ("news-source-failure-looks-empty",
     "news.py",
     # ⚠ 必须带上前一行注释作锚：2026-11-10 之后 `data: None` 那一行出现了
     # **两次**（东财两级失败 / 缺 fastNewsList 降级后仍失败），只用它单行
     # 作锚会让 count=2、锚点校验不过，且注入可能落在别的那一处上。
     '        # 降级也拿不到 → 与“两级源都失败”同语义：返回 None（不缓存）\n'
     '        _NEWS_MEM.update({"ts": now, "limit": limit, "data": None})\n'
     '        return None',
     '        # 降级也拿不到 → 与“两级源都失败”同语义：返回 None（不缓存）\n'
     '        _NEWS_MEM.update({"ts": now, "limit": limit, "data": []})\n'
     '        return []',
     "[快讯] 两级源都失败时返回 [] 而不是 None → alert 侧「源不可用」"
     "分支不可达，日志永远写「源可用但空」，排查方向被带偏",
     "也不可分辨"),
    # 面板顶层形状守卫（2026-10-27）：`read_json_obj` 去掉 isinstance 兜底，
    # 回到「`read_json(X, {}) or {}`」的旧形态 —— 顶层是 list 时原样返回，
    # 下游 `.get()` 抛 AttributeError，`/api/slots` 500 + 前端 Promise.all
    # 七路并发 → 整页只剩「载入失败」。
    ("webui-read-json-obj-guard-dropped",
     "webui.py",
     '    d = read_json(path, None)\n    return d if isinstance(d, dict) else {}',
     '    return read_json(path, {}) or {}  # 注入：不再校验顶层形状',
     "[面板] read_json_obj 不校验顶层形状 → state/pending/sectors 顶层写成 "
     "list 时 `.get()` 抛 AttributeError、/api/slots 500，前端 Promise.all "
     "七路并发整页白（2026-10-27 修复前的形态）",
     "read_json_obj"),
    # 总时长闸门文案写死 240s（2026-10-27）。
    ("stall-desc-budget-seconds-hardcoded",
     "analyze.py",
     '        _sec_tl = float(_m_tl.group(1)) if _m_tl else None',
     '        _sec_tl = None  # 注入：不读 stderr 里的真实秒数',
     "[AI 输出] 预算文案写死 240s → 「预算（240s）耗尽，耗时 47s」"
     "自相矛盾，排查者按 240s 推算会差 4 倍（2026-10-27 生产 09:23）",
     "不含 '47s'"),
    ("save-st-shape-check-dropped",
     "analyze.py",
     "    if not isinstance(s, dict) or any(k not in s for k in _ST_REQUIRED):",
     "    if False:  # 注入：不校验形态",
     "[状态文件] save_st 不再校验 state 形态 → 外来写入者能把生产 state "
     "覆盖成缺键形态，去重凭据丢失（2026-10-26 14:30 重复推送的入口）",
     "save_st 写入门失效"),
    ("save-st-fired-shrink-allowed",
     "analyze.py",
     # ⚠️ 2026-10-27：锚点缩进从 8 空格改成 12 —— save_st 的读-改-写
     # 被收进 `try:`（加 flock）后整体缩进一级。锚点必须按源码逐字节照抄。
     "            if _lost:\n                _log(f\"save_st 检测到 fired 丢失 {len(_lost)} 条\"",
     "            if False:  # 注入：允许当天 fired 缩短\n                _log(f\"save_st 检测到 fired 丢失 {len(_lost)} 条\"",
     "[状态文件] save_st 允许当天 fired 缩短 → 去重凭据被清空，同一槽位"
     "重复推送（2026-10-26 14:30 事故形态）",
     "save_st 写入门失效"),

    # -------------------- 2026-10-20：自检推送白名单 --------------------
    #
    # 2026-10-20 01:39 真实事故：我带着一个**根本不存在的** `MW_SKIP_PUSH=1`
    # 跑验证（变量名记错了，grep 零命中），守卫当时是**黑名单**
    # （只列 MW_MUT_SANDBOX / MW_SKIP_AI_PROBE），两道全不命中 → rc=1 把
    # 「自检异常」推进入群，并把 cache/selfcheck_alert_date.json 写成
    # {"sig":"1"}，连带让当天真正的生产告警被 24h 去重吞掉。
    # 判据改白名单后抽成 `_push_context(environ)`，下面三条分别打它的三面。
    ("push-guard-launchd-gate-dropped",
     "selfcheck.py",
     '    if env.get("XPC_SERVICE_NAME") != "com.mison.marketselfcheck":\n'
     '        return False, "非 launchd 的 selfcheck 语境"\n',
     "",
     "[推送守卫] 去掉 launchd 语境判据 → 默认放行，"
     "终端手工跑自检时 rc≠0 又把告警推进真实群（01:39 事故原样复发）",
     "终端手工跑（01:39 事故语境）"),

    ("push-guard-mut-sandbox-ignored",
     "selfcheck.py",
     '    if env.get("MW_MUT_SANDBOX") == "1":\n'
     '        return False, "变异回归沙箱"\n',
     "",
     "[推送守卫] 忽略 MW_MUT_SANDBOX → 变异回归一旦让沙箱进程 rc≠0，"
     "整轮回归每抓住一条都把告警推进真实群（2026-10-14 事故形态）",
     "变异回归沙箱"),

    ("sandbox-carries-real-webhook",
     "selfcheck.py",
     "",
     "",
     "[沙箱 webhook 隔离] 沙箱 config.json 带着**生产 webhook** 跑"
     "（2026-10-20 09:49 事故形态）→ 任何让自检 rc≠0 的变异体都会把告警"
     "推进真实群；检查项必须当场报红",
     "非生产语境不得携带真实 key"),

    ("hot-ttl-exceeds-interval",
     "hot.py",
     "HOT_TTL = 300\n",
     "HOT_TTL = 600\n",
     "[热股] HOT_TTL 超过调度间隔 → 缓存跨轮复用，"
     "同一条消息里 TOP3/hot 比其它数字旧至多 300s（2026-10-20 改回 600 的原形态）",
     "HOT_TTL=600 > 调度间隔"),

    ("check-index-nondict-crash",
     "alert.py",
     "    if not isinstance(q, dict):\n"
     '        _log(f"check_index 注入的行情不是 dict（{type(q).__name__}），"\n'
     '             f"本轮不判指数异动", "ALERT")\n'
     "        return []\n",
     "",
     "[指数突变] 去掉 check_index 的非 dict 守卫 → quotes_fn 换实现时抛 "
     "AttributeError，一整轮指数异动判定全丢且无留痕",
     "check_index 非 dict 行情直接抛异常"),

    ("preamble-strip-nonstr-crash",
     "analyze.py",
     # ⚠️ 2026-10-27：非 str 守卫现在有 6 个同形副本（本轮给同族纯函数
     # 补齐），锚点必须带上**后续一行**才唯一（`_is_head` 是 preamble 独有）。
     "    if not t or not isinstance(t, str):\n"
     "        return t\n"
     "    lines = t.split(\"\\n\")\n"
     "\n"
     "    def _is_head(s):",
     # ⚠️ 替换串必须**保留** `lines = …` 与 `def _is_head`，只删守卫两行 ——
     # 否则 `_is_head` 的函数体成了孤儿（语法错误，注入被拒 → 跳过）。
     "    lines = t.split(\"\\n\")\n"
     "\n"
     "    def _is_head(s):",
     "[AI 输出泄漏] 去掉 _strip_preamble 的非 str 守卫 → 复用该纯函数时传 "
     "None/数字直接抛，与 _strip_meta/_strip_think 的行为不一致",
     # ⚠️ 期望文案必须指向**本段**的失败行。守卫被拿掉后 `t.split` 直接
     # 抛 TypeError，落到本段的 except → 打印「前言行剥除检查异常（本项未生效）」，
     # 那句里**没有**用例标签（`非 str 原样返回` 只在正常比对失败时才出现）。
     # 原先写用例标签 → 明明 rc=1 却被判「漏网」（文案不匹配 ≠ 没抓住）。
     "前言行剥除检查异常"),

    # ⚠️ 2026-10-22：原 `backup-firstbyte-not-shorter`（备用链首字节上限
    # 必须短于主链）随"取消备用模型机制"一并删除 —— 它锚定的
    # `AI_BACKUP_FIRST_BYTE_LIMIT` 已废弃，判据本身也被我替换掉了。
    # 删掉而不是留着：一个变异体若锚点还在但**判据已不存在**，它会变成
    # 恒漏网（测不到任何东西），那种"绿"比没有更糟。

    ("firstbyte-param-ignored",
     "marketlib.py",
     "                                and time.monotonic() - _t0 > first_byte_limit)",
     "                                and time.monotonic() - _t0 > AI_FIRST_BYTE_LIMIT)",
     "[AI 调用看门狗] 首字节闸门忽略传入的 first_byte_limit → 备用链"
     "仍按主链 150s 等（备用本该 120s），参数形同虚设",
     "first_byte_limit 参数未被采纳"),

    ("stall-desc-hardcoded-idle",
     "analyze.py",
     '    if "[first-byte-gate " in (se or ""):\n'
     '        return f"首字节闸门（{M.AI_FIRST_BYTE_LIMIT}s 零输出）"\n',
     "",
     "[AI 输出] 终止原因文案不看 stderr 来源标记 → 首字节闸门杀的也报成"
     "「空闲闸门 600s」，排查者按 600s 推算被吞轮数会差 4 分钟"
     "（2026-10-20 10:07 生产日志）",
     "标记优先于 timed_out"),

    # --- 确定性失败闸门被删（[AI 调用看门狗] 应抓）---
    # 2026-10-21 加：stderr 出现 CLI 自报的 unrecognized_model 后必须立即
    # 终止 —— 该调用第一秒已注定失败，等 idle 纯属白等（10-20 13:55 备用链
    # 实测 605s；stderr 有字节，首字节闸门天然不触发）。
    ("fatal-model-gate-dropped",
     "marketlib.py",
     '        elif _rt is not None:\n'
     '            _fb_ref = max(_t0, _rt)\n'
     '            _first_byte_dead = (first_byte_limit\n'
     '                                and not any(bufs[1])\n'
     '                                and time.monotonic() - _fb_ref > first_byte_limit)\n'
     '        else:\n'
     '            _first_byte_dead = (first_byte_limit\n'
     '                                and not any(bufs[1]) and not any(bufs[2])\n'
     '                                and _t0\n'
     '                                and time.monotonic() - _t0 > first_byte_limit)',
     '        else:\n'
     '            _first_byte_dead = (first_byte_limit\n'
     '                                and not any(bufs[1]) and not any(bufs[2])\n'
     '                                and _t0 and time.monotonic() - _t0 > first_byte_limit)',
     "[AI 调用看门狗] stderr 出现 unrecognized_model 后必须收回 stderr 的"
     "活跃特权、改按 stdout 计首字节（否则挂起形态白等 idle 600s，"
     "10-20 13:55 实测 605s）",
     "模型名被拒后挂起未被提前终止"),

    # --- 文案不认 fatal 标记（[AI 输出] 应抓）---
    # 闸门还在、_stall_desc 的分支被删 → 杀是杀了，日志却报成
    # 「空闲闸门 600s」，排查者按"产出断了"去查通道，而真因是模型名被拒。
    ("fatal-desc-branch-dropped",
     "analyze.py",
     '    if "[first-byte-gate-after-model-reject " in (se or ""):',
     '    if False and "[first-byte-gate-after-model-reject " in (se or ""):',
     "[AI 输出] 终止原因文案不认确定性失败标记 → 网关拒模型名被报成"
     "「空闲闸门 600s」（排查方向：通道退化；真因：模型名无效）",
     "模型名被网关拒绝"),

    ("push-guard-inlined-back-in-main",
     "selfcheck.py",
     "        _prod_ctx, _why = _push_context()\n",
     "        _prod_ctx = True\n",
     "[推送守卫] __main__ 绕开 _push_context 恒判生产 → 纯函数那 8 条用例"
     "全绿而生产照旧误推（守卫写在函数里、接线断掉的形态）",
     # ⚠️ 期望文案必须指向**接线断言**那条：该变异体把调用换成 `_prod_ctx = True`，
     # 于是 `_push_context` 的调用点只剩测试循环那 1 处 → 报「守卫没接进
     # __main__」。原先写的是反向断言（"__main__ 块里还有…"）的文案，而
     # 反向断言只查字面量、这条变异体并不触发它 → 明明 rc=1 却被判"漏网"
     # （2026-10-20 实测，文案不匹配 ≠ 没抓住）。
     "守卫没接进 __main__"),

    # --- 去重键退回裸 rc（[推送守卫] 应抓）---
    # 2026-10-21 加：裸 rc 只有 1/2，同一天先推过 rc=1 后，另一种 rc=1
    # 缺陷会被当成"同一事件"吞掉（10-20 01:39 手工跑写 {sig:"1"}，
    # 当天 15:34 生产自检的「慢轮吞槽位」❌ 差点被吞）。
    ("alert-sig-rc-only",
     "selfcheck.py",
     '    _sig = f"{rc}"\n'
     '    try:\n'
     '        import hashlib as _hl\n'
     '        import re as _re_hl\n'
     '        _p = out_path or p_log("selfcheck.out")',
     '    _sig = f"{rc}"\n'
     '    return _sig   # 注入：去重键退回裸 rc（同 rc 不同缺陷互相吞）\n'
     '    try:\n'
     '        import hashlib as _hl\n'
     '        _p = out_path or p_log("selfcheck.out")',
     "[推送守卫] 去重键必须区分缺陷（rc+首条 ❌ 指纹）—— 裸 rc 会让同一天"
     "第二种缺陷被当成重复吞掉",
     "推送守卫错: 去重键不区分缺陷"),

    # --- 告警正文不带原因（[推送守卫] 应抓，2026-11-05）---
    # 实测：用户收到「自检异常（rc=1）」但看不出是什么事，必须登录机器
    # 才能知道。首个 ❌ 行必须直写进告警正文。
    ("alert-msg-no-reason",
     "selfcheck.py",
     '    _mid = f"\\n首个失败：{_f[0][:120]}" if _f else ""',
     '    _mid = ""   # 注入：告警正文不带原因',
     "[推送守卫] 告警正文不带首个 ❌ → 群里只看到「自检异常」不知道是什么事"
     "（2026-11-05 用户反馈）",
     "告警正文没带首个 ❌ 行"),

    # --- 未验证计数点没有可定位文案（2026-11-06 加）---
    # 生产 11-05 那轮：结论写「另有 2 项本轮未验证」，读者 grep「未验证」
    # 只命中 1 行 —— 第二项（AI 冒烟卡死）打的是 ❌ 文案。
    # 判据 = 每个 `_unverified[0] += 1` 的最小 suite 里必须有
    # 「未验证」或「❌」字样，否则那笔计数在输出里定位不到。
    ("unverified-counter-unlocatable",
     "selfcheck.py",
     '                print("  ⚠️  找不到 agent 源码，接线断言**本轮未验证**")',
     '                print("  ⚠️  找不到 agent 源码，接线断言跳过")',
     "[检查项可失败性] 未验证计数点没有可定位文案 → 结论行的 "
     "grep 指引命中数 < N（生产 11-05：说 2 项、只 grep 到 1 行）",
     "既没打「未验证」也没打「❌」"),

    # ---------- 资讯来源链接（2026-10-22） ----------
    # 真实缺陷形态：链接被静默丢掉（用户看到的是"没来源的快讯"，
    # 而系统自认为做了）—— 不报错、不告警，只是消息里少了一样东西。
    ("news-link-dropped",
     "analyze.py",
     '    _t = title.replace("[", "【").replace("]", "】")\n',
     '    _t = title\n'
     '    return title   # 注入：链接整个丢掉\n',
     "[资讯来源链接] 快讯应渲染成 [标题](url) —— 丢掉后群里没有来源网址",
     "正常链接未按"),

    # 真实缺陷形态：URL 形态校验被摘掉 —— 含 `)` 的 URL 会提前闭合
    # markdown 链接语法，后半截变成裸文本，且**看起来仍是个链接**。
    ("news-link-bad-url-accepted",
     "analyze.py",
     '    if any(ch in _u for ch in (" ", "(", ")", "\\n", "\\r", "\\t", "<", ">")):\n'
     '        return title\n',
     '    if False:   # 注入：不再校验 URL 形态\n'
     '        return title\n',
     "[资讯来源链接] 含右括号的 URL 必须降级为纯标题（否则截断链接语法）",
     "时仍产出了链接"),

    # ---------- 2026-10-28 优化轮：把本轮修的缺陷逐条钉住 ----------
    #
    # ① 结构重写丢首版（P0）：与 10-27 修过的禁用词重写同型 —— 那一半
    # 修了，这一半没修。重写返回空时首版（已过 `_safe`、只缺一个必带段）
    # 被整条丢弃，退兜底模板。判据在 `[重写回退与流水编码]` ①b。
    # --- [未绑定全局名] 的判别力（2026-10-29 P0 事故：_t0 NameError）---
    # --- [调研 Agent] 的判定顺序（2026-10-29：第一版在交易日恒判 pending）---
    # --- [调研 Agent] 的判定顺序（2026-10-29：第一版在交易日恒判 pending）---
    # 精确复刻第一版的 bug：把「上一个交易日」这条分支短路掉，于是
    # 自检的日常时刻（交易日 14:2x）恒判 pending、永远看不到昨天的结果。
    ("agent-verdict-always-pending",
     "selfcheck.py",
     "    if not prev_td:\n"
     "        return \"pending\"",
     "    if True:   # 注入：回退到第一版（交易日恒判 pending）\n"
     "        return \"pending\"",
     "[调研 Agent] 的构造值自测应报「交易日 14:2x、昨日失败」等用例不符"
     " —— 第一版就是这样在**每个工作日**恒判「还没到点」，"
     "agent 连跑几天失败也只会打这一句"),

    # --- watch.log 的 _rotate_log 行为（此前零覆盖，2026-10-29 补）---
    ("watchlog-rotate-os-replace-broken",
     "marketlib.py",
     "                if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:\n"
     "                    os.replace(LOG, LOG + \".1\")",
     "                if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:\n"
     "                    os.remove(LOG)   # 注入：直接删，不留 .1",
     "[日志轮转] 应报「marketlib._rotate_log: 没有生成 `.1`」"
     " —— watch.log 出问题后无法回溯上一代"),

    # --- AI 计时口径（2026-10-29 P0：墙钟 − 单调钟 = 1.79e9 秒）---
    ("round-ai-ms-clock-mixed",
     "analyze.py",
     "            _ROUND_AI_MS.append(int((time.monotonic() - _t) * 1000))",
     "            _ROUND_AI_MS.append(int((time.time() - _t) * 1000))"
     "   # 注入：与 _t 的 monotonic 口径不一致",
     "[AI 调用看门狗] 或预算相关段应报「AI 已用时间异常」——"
     "墙钟减单调钟 ≈ 1.79e9 秒，会让 `_budget = max(0, 240 - 1.79e9) = 0`，"
     "AI **一次都不调用**、每轮直接退兜底模板（2026-10-29 的真实形态）"),
    # ⚠️ 锚点 2026-10-29 改：这段计算抽成了纯函数 `_elapsed_ms`（原先在
    # `run()` 与 `_run_after_inflight()` 里各写一遍）。抽出来的**直接理由**
    # 就是这条变异体的上一版**漏网** —— 负数被下游 `> 0` 判据吃掉，
    # 纯行为断言看不见，只有对返回值做量级单测才咬得住。
    ("round-data-ms-clock-mixed",
     "analyze.py",
     "    return int((time.time() - t0) * 1000)",
     "    return int((time.monotonic() - t0) * 1000)   # 注入：与 t0 的墙钟口径不一致",
     "[AI 调用看门狗] 的计时口径断言应报「`_elapsed_ms(墙钟起点)` 量级异常」"
     " —— 混用会得约 -1.79e9，而下游 `_ROUND_DATA_MS > 0` 会把它当"
     "「未设置」按 0 处理，取数耗时永远不计入 AI 预算（静默失效）"),
    # ⚠️ 锚点 2026-10-29 改：这段判定被抽进了纯函数 `_elapsed_ms`（原先是
    # `_run_after_inflight()` 里的内联 if）。bug 形态完全一样 ——
    # `_t0` 只是别的函数的局部名，本函数里按全局规则查找 → 必然 NameError。
    ("cross-scope-name-ref",
     "analyze.py",
     "    if not isinstance(t0, (int, float)):\n"
     "        return None",
     "    if not isinstance(_t0, (int, float)):   # 注入：引用外层函数的局部名\n"
     "        return None",
     "[未绑定全局名] 应报「_run_after_inflight() 引用了未绑定的全局名 _t0」"
     " —— 这正是 2026-10-29 让当天 09:25 盘前简报整轮失败的那个 P0"),

    ("struct-rewrite-discards-first",
     "analyze.py",
     '                if _rs2 == "输出不可用" and _first_txt and _first_min_ok:',
     '                if False:   # 注入：结构重写不再回退首版',
     "[重写回退与流水编码] 结构重写返回空时必须回退首版"
     "（首版只缺一段，比兜底模板有信息量）",
     "结构重写返回空时"),
    # ⚠️ 回退必须**同时**要求首版过形态下限（2026-10-28 加，P0 回归）。
    # 只看「重写空产出」会把首版是空壳的轮次也回退 —— 而空壳正是结构闸门
    # 存在的理由。`[实测]` 沙箱：首版 = DSML 占位串（19 字）→ 回退 → 这句
    # 占位串原样进了推送正文，与 `_invoke` 写死的不变式直接冲突。
    ("struct-rewrite-fallback-ignores-shell",
     "analyze.py",
     '                _first_min_ok = (len(_first_txt) >= 60 and _n_tags_first >= 2)',
     '                _first_min_ok = True   # 注入：回退不再要求首版过形态下限',
     "[重写回退与流水编码] 首版是空壳/占位串时不得借回退路径出线",
     "回退把**空壳正文**推了出去"),
    # ⚠️ 港股半日市豁免必须限定「时间戳就是当天」（2026-10-28 加，P1 回归）。
    # 原判据只看「今天是半日市 + 现在 ≥12:00」，半日市整个下午的港股
    # 新鲜度闸门整体失效 —— 包括它存在的唯一理由：源回退到上一交易日收盘价。
    ("hk-halfday-exempt-any-date",
     "marketlib.py",
     '            and now.hour * 60 + now.minute >= 12 * 60\n'
     '            and t.date() == now.date()):',
     '            and now.hour * 60 + now.minute >= 12 * 60):   # 注入：不判日期',
     "[新鲜度字段透传] 港股半日市豁免必须限定时间戳是当天"
     "（否则昨日收盘被当现价）",
     # ⚠️ must_contain 不能写死具体日期（2026-11-07）：自检取的是
     # `sorted(HK_HALF_DAYS)[0]`，配置里新增 2026-02-16（春节前夜）后排序
     # 第一位变了，写死的 "2026-12-24" 就永远对不上 —— 变异体**真的被
     # 抓住了**，却被判成漏网。只留不随日期变化的框架词。
     # （注意这里是**子串包含**判断，不是正则，不能写成 {a|b}）
     "下午把**上一交易日（"),

    # ② `_stall_desc` 秒数正则只认整数（P0）：生产 marker 的秒数是
    # float（`240.0 - used`），`(\d+)s` 对 `196.0s` 恒不命中 → 秒数分支
    # 成死代码，日志永远退回硬编码「预算（240s）耗尽」（生产
    # `grep -c 本次调用上限` = 0 为证）。判据在 `[AI 输出]` 的文案用例。
    ("stall-desc-int-only-seconds",
     "analyze.py",
     '        _m_tl = re.search(r"\\[total-limit-gate (\\d+(?:\\.\\d+)?)s", se or "")',
     '        _m_tl = re.search(r"\\[total-limit-gate (\\d+)s", se or "")',
     "[AI 输出] 预算秒数正则必须认 float（生产 marker 是 196.0s）",
     "196s"),

    # --- 板块日K 剔除不可能存在的交易日（2026-11-21，P0）---
    # push2test 实测给出「2026-11-21（周六）」的 K 线，会污染 20 日高/低。
    ("kline-impossible-date-kept",
     "marketlib.py",
     '                if _d2 > _today2 or not is_trading_day(_d2, warn=False):',
     '                if False:   # 注入：不过滤日期',
     "[日K日期合理性] 不过滤 → 周六/未来日的合成 K 线入库，"
     "抬高板块 20 日高、让「距 20 日高」偏乐观（偏差不报警）",
     "未来日（2027-12-26）的 K 线没被剔除"),

    # --- 实时行情禁用测试源（2026-11-21，P0 数据正确性）---
    # EM 被封只剩 push2test 通时，quotes() 返回伪造行情（3701.71 vs 真值 3533.79），
    # 三大指数全错。`_valid_quotes()` 与新鲜度闸门都拦不住。
    ("quote-source-trust-emtest",
     "marketlib.py",
     '    em_hosts = [h for h in _ordered_em_hosts()\n'
     '                if _host_of(h) != _host_of(EM_TEST)]',
     '    em_hosts = _ordered_em_hosts()   # 注入：恢复旧行为',
     "[行情源可信度] 实时行情候选池又含 push2test → EM 真主机被封时"
     "会把测试环境的模拟行情当成真行情推给用户",
     "采用了 push2test 的伪造行情"),

    # --- 兜底链超时钳制（2026-11-21，P1）---
    # 11:42 直连超时后白烧 firecrawl 90s，整轮 300s、AI 一分钱没拿到。
    ("data-fallback-timeout-unclamped",
     "marketlib.py",
     '        if _lft < _t_want:',
     '        if False:   # 注入：不钳',
     "[兜底钳制] 兜底超时不按剩余预算收紧 → 代理 25s + firecrawl 90s "
     "照样越过上限（11:42 白烧 90s 的形态）",
     "剩 4s 时兜底超时仍为"),

    ("data-fallback-timeout-zero",
     "marketlib.py",
     '            timeout = max(1, int(_lft))',
     '            timeout = int(_lft)   # 注入：去掉下限',
     "[兜底钳制] 去掉 max(1,..) → int(0.5)=0，curl --max-time 0 语义是"
     "「不限制」，比不钳更糟",
     "int() 截成 0"),

    # --- AI 调用留痕（2026-11-21，P1 可观测性）---
    # `AI_MODEL=None` 走 CLI 默认模型，那个 id 只在上游，日志里从来没记过。
    # 11-20 排查 AI 变慢时只能手工跑 CLI 才知道是 claude-fable-5。
    ("ai-observability-log-removed",
     "analyze.py",
     '        M.log(f"AI 调用：模型 {_mdl or (model or M.AI_MODEL or \'CLI 默认\')} "\n              f"· 耗时 {_dt_ai:.0f}s · 原始 {_nch} 字符 · rc={_rc} "\n              f"· 预算 {(_budget if _budget is not None else -1):.0f}s"\n              f"{\'（被掐停）\' if _to else \'\'}"\n              # ⚠️ `dry` **不是**本函数的可见名（它是 main() 的局部量）——\n              # 第一版直接写了 `if dry`，NameError 被 `_invoke` 外层那句\n              # `except Exception: pass` **静默吞掉**，日志一条都不出，\n              # 而我以为是"没走到这里"。用模块级判定函数 `_dry_flag_active()`\n              # 才是对的（同名函数本就在，见 768 行）。\n              f"{\'（dry）\' if _dry_flag_active() else \'\'}", "AI")',
     "        pass  # 注入：删掉 AI 留痕",
     "[AI 调用留痕] 留痕日志被删 → 「今天用哪个模型」又变成查不到，"
     "「变慢了」分不清是换模型还是同模型变慢",
     "没有「AI 调用：模型」日志"),

    ("ai-observability-model-blind",
     "marketlib.py",
     '        if _o.get("type") == "system" and _o.get("model"):',
     '        if False and _o.get("type") == "system" and _o.get("model"):',
     "[AI 调用留痕] 模型抽取恒空 → 日志只会打「CLI 默认」，"
     "上游真实模型名仍然不可见（记录了半天等于没记）",
     "ai_extract_model(init 事件)"),

    # --- 取数入口零发包（2026-11-21，P1）---
    # `data_cap` 只管 clist/members/量比；quotes（行情/港股/备用源）原先
    # 完全不受约束 —— 11:42 那轮 143s 就花在这，AI 拿到 0s 预算。
    ("data-entry-gate-noop",
     "marketlib.py",
     '    return _l is not None and _l <= 0',
     '    return False   # 注入：data_expired 恒假',
     "[取数入口闸] data_expired 恒假 → 预算耗尽仍照常取数，"
     "最坏再拖 175s，AI 拿不到预算只能推模板",
     "预算耗尽时 quotes 仍发"),

    ("data-cj-retry-unbounded",
     "marketlib.py",
     '        if i and data_expired():',
     '        if False:   # 注入：不拦重试',
     "[取数入口闸] cj 重试链不拦 → 预算耗尽后仍跑满 3 次重试 + 代理/firecrawl"
     "（最坏再 175s），11:42 走到 300s 就是这么来的",
     "预算耗尽时 cj 应只放行首次"),

    # --- 慢取数留痕（2026-11-20，P1 可观测性）---
    # 11:42 那轮前 143s 全程无日志，只能靠后面 clist 的「本轮已用 143s」
    # 反推，无法归因到具体哪一步（当时先误判成 news）。
    ("data-timing-quotes-silent",
     "analyze.py",
     '        M.log(f"取数耗时：行情 {\'快照复用\' if _from_snap else \'实时取数\'} "\n              f"{_dq:.1f}s（标的 {len(ids)} 个）—— 超过 5s 才记，"\n              f"慢轮据此归因", "FEED")',
     "        pass  # 注入：行情慢取数不留痕",
     "[慢取数留痕] 行情段耗时日志被删 → 300s 那种慢轮又变成静默黑盒，"
     "只能事后靠别的日志反推是哪一步慢",
     "缺慢取数耗时日志"),

    ("data-timing-trend-silent",
     "analyze.py",
     '                M.log(f"取数耗时：分时补取 {_ds:.1f}s —— 慢轮据此归因", "FEED")',
     '                pass  # 注入：分时慢取数不留痕',
     "[慢取数留痕] 分时补取耗时日志被删 → 那 143s 静默段再次无法归因",
     "缺慢取数耗时日志"),

    # --- 阈值兜底漂移 + 自检百分比误报（2026-11-20）---
    # 今日配置从 3630 重校准到 3482.43，TH 与 3 处字面默认值**又漏同步** ——
    # marketlib 上方注释自己写着"重校准后必须手工同步"，说明靠人记得不可靠。
    # 同一轮还查出 [plan.md 新鲜度] 把「下方 3.4%」的百分比当阈值。
    ("th-fallback-drift-hstech-down",
     "marketlib.py",
     '    "hstech_down": 3482.43,',
     '    "hstech_down": 3630,',
     "[阈值兜底] TH 与配置漂移 → sectors.json 缺键/写坏时触发线静默退回旧数"
     "（旧线 3630 高于现价会退化成每日常中）",
     # ⚠️ 期望串不能写裸 `hstech_down` —— 那只键名在**干净基线**的 ✅ 行里
     # 也存在，判据对它恒为真、判别力为零（工具的 must_contain 恒真闸当场点出）。
     # 取只在缺陷触发时才出现的整句，并带 ❌ 前缀区分同文案的 ✅ 行。
     "❌ 1 处阈值兜底已与真值漂移"),

    ("planmd-pct-read-as-threshold",
     "selfcheck.py",
     '            _pairs = [(r"上方\\s*\\*{0,2}(\\d+(?:\\.\\d+)?)(?![\\d.%])(?!\\s*%)", "hstech_up"),' "\n" '                      (r"下方\\s*\\*{0,2}(\\d+(?:\\.\\d+)?)(?![\\d.%])(?!\\s*%)", "hstech_down")]',
     '            _pairs = [(r"上方\\s*\\*{0,2}(\\d+(?:\\.\\d+)?)", "hstech_up"),' "\n" '                      (r"下方\\s*\\*{0,2}(\\d+(?:\\.\\d+)?)", "hstech_down")]',
     "[plan.md 新鲜度] 正则退回无「非百分比」断言 → 「下方 3.4%」被当成"
     " hstech_down，连报两条假 ❌ 并把自检 rc 抬成 1（变异基线变红、回归被阻塞）",
     "plan.md 写 3.4"),

    # --- 占位剔除余段头（2026-11-20，实推缺陷）---
    # 生产 5 条推送里出现过光秃秃的 `**热点**` 段头、下面空无一字。
    # 判据在 selfcheck 的 [占位剔除余段头]（删得掉 + 不误伤 两头都测）。
    ("placeholder-orphan-header-kept",
     "analyze.py",
     '            if re.fullmatch(r"\\*\\*[^*]+\\*\\*(（[^）]*）)?", _prev):',
     "            if False:   # 注入：不查上一行是不是段头",
     "[占位剔除] 只删占位行、不查上一行 → 留下光秃秃的 `**热点**` 段头，"
     "读者看到标题下空无一字（2026-11-17/19/20 共 5 条生产推送的形态）",
     "占位删了但段头留着"),

    ("placeholder-header-overdeleted",
     "analyze.py",
     '                if not _rest or _rest[0].strip().startswith("**"):',
     "                if True:   # 注入：无条件连带删段头",
     "[占位剔除] 无条件连带删段头 → 把「段头 + `> ` 引用正文」的建议段削掉"
     "（`> ` 是正文格式，不是新段头）",
     "段头被误删"),

    # --- 取数编排层闸门（2026-11-20，P1）---
    # `[实测]` 11:42 那轮 300s：138s quotes/trend + 13s clist(已钳) + 149s news。
    # news 跑满让 AI 拿到 0s → 406 字残稿（热点段空）+ 饿死 1 次触发。
    # 判据在 tests/test_data_budget.py（AST 级别：条件真的求值预算、
    # body 真的跳过、else 真的仍取数）。
    ("data-gate-trend-noop",
     "analyze.py",
     "        if _dl is not None and _dl <= 0:",
     "        if False:   # 注入：分时闸门失效",
     "[取数编排闸] trend 闸条件变常量 False → 预算耗尽仍去补取分时，"
     "138s 那种静默段照旧拖满整轮",
     # ⚠️ 期望串逐字取自 selfcheck 真正打出的 ❌ 行首段，
     # 不是 tests/ 的断言措辞（该工具只认「本轮新增失败行」，
     # 写错 = 明明抓住却报漏网，见 push-split 那两条的同类教训）。
     "trend 闸的条件没求值预算"),

    ("data-gate-news-noop",
     "analyze.py",
     "            ns = []",
     "            pass   # 注入：只打日志不置空",
     "[取数编排闸] news 闸只打日志、不置空 ns → 分支等于没写，"
     "快讯取数照旧跑满（那是 11:42 那轮 300s 里的 149s）",
     "news 闸 body 没把 ns 置空"),

    # --- 推送排版拆行阈值（2026-11-20，用户「优化一下排版」）---
    # 盘面段 129 字距旧阈值 132 只差 3 字 → 不触发，手机上折 8 行无缩进。
    # 判据在 tests/test_push_layout.py（引用 A._SPLIT_LIMIT，不抄数字）。
    ("push-split-limit-too-wide",
     "analyze.py",
     "_SPLIT_LIMIT = 88",
     "_SPLIT_LIMIT = 132",
     "[推送排版] 拆行阈值偷偷调回 132（6 行屏宽）→ 盘面/关键位那些 100~130 "
     "字的段不再触发拆行，手机上一折就是一堵墙且续行无缩进"
     "（2026-11-20 用户截图反馈的形态）",
     # ⚠️ 期望串必须**逐字**取自 selfcheck 真正打出的 ❌ 行首段。
     # 该工具只认「本轮新增失败行」，不认全文出现 —— 写错就是假漏网。
     # `[实测]` 第一版写 `_SPLIT_LIMIT=132 太宽`（那是 tests/ 的口径），
     # 自检实际打的是「拆行阈值 132 太宽」，于是明明抓住却报漏网。
     "拆行阈值 132 太宽"),

    ("push-split-head-ignores-head-width",
     "analyze.py",
     "        _first_left = _LIMIT - len(_head)      # 首行正文可用宽度",
     "        _first_left = _LIMIT - 2      # 注入：首行也用续行宽",
     "[推送排版] 首行改回按 `LIMIT-2` 切 → 带段头的首行溢出 len(head)，"
     "2026-11-20 13:40 生产推送里 **盘面**92 / **资金**92 / **热点**91"
     " 正是这个形态（手机上仍多折一行）",
     # 逐字取自自检实际打出的 ❌ 行首段（只认新增失败行）
     "带段头的首行溢出"),

    ("push-split-ignores-const",
     "analyze.py",
     "    _LIMIT = _SPLIT_LIMIT",
     "    _LIMIT = 132",
     "[推送排版] 函数里另写一个数字、不取模块常量 → 调阈值时生产不变，"
     "测试引用的常量却变了，两者静默分叉",
     # 同上：取自检真实 ❌ 文案（「没引用 `_SPLIT_LIMIT`」），
     # 不是 tests/ 里的断言措辞。
     "没引用"),

    # --- 取数全局上限（2026-11-20，P1）---
    # `AI_ROUND_BUDGET` 是「240s − 取数耗时」。取数阶段原先没有总数，
    # 三个子预算（60+40+80）能叠到 180s ⇒ AI 只剩 60s 而 P90 要 140s ⇒
    # 慢轮必退模板。判据在 tests/test_data_budget.py。
    ("data-budget-cap-ignored",
     "marketlib.py",
     '    _left = data_left()\n    if _left is None:\n        return own_s',
     '    _left = None  # 注入：无视全局上限\n    if _left is None:\n        return own_s',
     "[取数全局上限] data_cap 无视全局剩余 → 三个子预算照旧叠加到 180s，"
     "AI 只剩 60s 而 P90 要 140s，慢轮必退模板（2026-11-18 14:54 实测形态）",
     "全局 30s 更紧时应取 30"),

    ("data-budget-vr-unwired",
     "hot.py",
     'vr_deadline = time.time() + M.data_cap(VR_BUDGET, "量比")',
     'vr_deadline = time.time() + VR_BUDGET',
     "[取数全局上限] 量比自算没被全局上限钳 → 冷启动那轮它独自能吃 80s，"
     "取数总耗时照样能到 172s（AI 上限掉到 68s）",
     # 逐字取自自检实际 ❌ 行首段（只认新增失败行）
     "hot.py 只有"),

    ("data-budget-not-set-per-round",
     "analyze.py",
     '        M.set_data_deadline((_t0 if isinstance(_t0, (int, float))\n'
     '                             else time.time()) + M.DATA_BUDGET)',
     '        M.set_data_deadline(None)',
     "[取数全局上限] run() 没按本轮起点设截止（或设成 None）→ 全局上限"
     "永不生效，等于没做这个修复",
     "set_data_deadline 必须以 `_t0 + DATA_BUDGET` 调用"),

    ("data-budget-cap-silent",
     "marketlib.py",
     '    if own_s is not None and _cap < own_s:',
     '    if False and own_s is not None and _cap < own_s:',
     "[取数全局上限] 钳制不留痕 → 该源本轮取不全（量比全按 1.0、热点榜变"
     "子集）而推送里看不出来，事后只能靠「这轮怪怪的」去猜",
     "钳制日志应恰好 1 条"),

    # --- AI 终止归因落盘（2026-11-20，P1）---
    # `ai_fallback` 只说"退了模板"，不说"为什么"。2026-11-19 自检因此把
    # 4 轮「我方预算掐的」+1 轮「通道挂起」合并报成「AI 通道实质不可用 →
    # 查网关」，把排查方向引错了。判据在 tests/test_ai_stall_attribution.py。
    ("ai-stall-record-last-wins",
     "analyze.py",
     '    if _ROUND_AI_STALL:\n        return',
     '    if False:  # 注入：改为后记覆盖\n        return',
     "[AI 兜底归因] 本轮 AI 终止原因被后一次调用覆盖 → 读者看到的模板是"
     "第一次失败导致的，归因却指向重写那次（真因被替换成结果）",
     "必须首记优先"),

    ("ai-stall-not-persisted",
     "runlog.py",
     '    if ai_stall:\n        rec["ai_stall"] = str(ai_stall)[:20]',
     '    pass  # 注入：不落 ai_stall',
     "[AI 兜底归因] ai_stall 不落流水 → 自检仍只能看到 ai_fallback=True，"
     "把预算掐停报成通道故障（2026-11-19 实测的误诊形态）",
     # ⚠️ 期望串取自自检 ❌ 行原文「ai_stall 未落流水（…）」，不是本注释措辞
     "ai_stall 未落流水"),

    ("ai-stall-kind-model-reject-order",
     "analyze.py",
     '    if "[first-byte-gate-after-model-reject " in _s:\n        return "model-reject"',
     '    if False:  # 注入：漏掉 model-reject 分支\n        return "model-reject"',
     "[AI 兜底归因] 模型名被拒的变体没单独判 → 归因错成「零输出」"
     "（排查方向从'查模型配置'变成'查网关断流'）",
     "model-reject"),

    ("ai-stall-split-accepts-str",
     "selfcheck.py",
     '    if isinstance(stalls, (str, bytes)) or stalls is None:\n        return 0, 0',
     '    if stalls is None:\n        return 0, 0',
     "[AI 兜底归因] 拿去分类的 stalls 传入字符串时被逐字符计数 → "
     "7 个字符各算一次兜底，'预算为主'的判定被脏输入带偏",
     "0, 0"),

    # ③ load_st 把「读不动」折叠成「不存在」（P1）：坏 JSON/EACCES 时
    # 静默返回空状态，当天 fired 去重记录清零 → 剩余时段重复推送，
    # 且一条 ERR 都没有。判据在 `[状态文件]` 的三态行为断言。
    ("loadst-unreadable-looks-missing",
     "analyze.py",
     '    except FileNotFoundError:\n        _why = "missing"            # 全新安装，静默走空状态',
     '    except Exception:\n        _why = "missing"            # 注入：读不动也当缺失',
     "[状态文件] state.json「读不动」必须留 ERR（不能静默当全新安装）",
     # ⚠️ must_contain 必须照抄**检查项实际打印的**字样（2026-10-28）：
     # 首版猜的是 "无法解析"（那是 analyze 的日志措辞），而自检报的是
     # `❌ state 读取三态失效: …` —— 猜错就会把「已抓住」误判成漏网
     # （[[market-watch-must-contain-guess-wrong]] 同型）。
     "state 读取三态失效"),

    # ④ 港股半日市被判成"数据缺失"（P1）：12-24/12-31 的 13:00 之后，
    # 港股时间戳必然滞后 > 20 分钟，新鲜度闸门整批丢弃港股行，推送写
    # 「恒生指数 数据缺失」—— 与 10-19 重阳节事故逐字同型。
    ("hk-halfday-exemption-dropped",
     "marketlib.py",
     '    if (market == "hk" and now.date().isoformat() in HK_HALF_DAYS',
     '    if (False and now.date().isoformat() in HK_HALF_DAYS',
     "[新鲜度字段透传] 港股半日市下午不得判陈旧（否则推送写「数据缺失」）",
     # 同上：日期不写死。
     "对 12:00 的时间戳仍判陈旧"),

    # ⑤ 东财主路径只判跨日被改成"什么都不判"（P2）：`_fetch` 注入的 f124
    # 若走 20 分钟阈值，集合竞价期间（f124 仍是昨日收盘）会把盘前那一轮的
    # A 股行情整批误杀；反之若完全不判，则"限流回上一交易日点位"抓不到。
    ("em-crossday-gate-off",
     "marketlib.py",
     '        if now.hour * 60 + now.minute < 9 * 60 + 35:\n'
     '            return False\n'
     '        return t.date() != now.date()',
     '        return False   # 注入：东财链路不再判跨日',
     "[新鲜度字段透传] 东财 f124 链路必须判跨日"
     "（否则限流回上一交易日点位抓不到）",
     "cross_day_only 不判跨日"),

    # ⑥ 「主备双败」文案（P1）：备用链 2026-10-22 已删，日志却仍写
    # 「两条链都挂了」→ 排查者去查网络，而实际只有一条链。
    # 判据是 `[AI 熔断]` 段的行为断言（日志文案含"主链"不含"主备"）。
    ("breaker-log-claims-dual-chain",
     "analyze.py",
     '            _log(f"AI 通道熔断中（上一轮主链零产出，还有 {_left//60} 分 {_left%60} 秒）"',
     '            _log(f"AI 通道熔断中（主备双败，还有 {_left//60} 分 {_left%60} 秒）"',
     "[AI 熔断] 熔断日志不得声称「主备双败」（备用链已删，指向不存在的第二条链）",
     "主备"),

    # ⑦ 收尾事件被当成正文（P2）：`_AI_CONTENT_MARKS` 含 `"type":"result"`
    # 时，CLI 先吐 error result 再挂住会让首字节闸门**永久解除** ——
    # 真挂起被记成"跑得慢"，且只能等满 idle 600s 才杀。判据在
    # `[AI 调用看门狗]` ⑨（result-only 挂起必须被首字节闸门杀）。
    ("content-mark-includes-result",
     "marketlib.py",
     '_AI_CONTENT_MARKS = (b\'"type":"assistant"\', b"content_block_delta")',
     '_AI_CONTENT_MARKS = (b\'"type":"assistant"\', b"content_block_delta",\n'
     '                     b\'"type":"result"\')',
     "[AI 调用看门狗] 收尾事件 result 不算正文（否则首字节闸门被永久解除、"
     "真挂起要等满 idle）",
     "只吐 result 再挂住的进程未被终止"),

    # ⑧ 港股午休窗口与 A 股共用并集（P3）：并集 [11:30,13:00) 会让港股在
    # 11:30–12:00（**仍在交易**）的陈旧时间戳被当现价放行。判据在
    # `[新鲜度字段透传]` 的港股份支。
    ("hk-lunch-union-window",
     "marketlib.py",
     '    _lunch = ((12 * 60, 13 * 60) if market == "hk" else _LUNCH)',
     '    _lunch = _LUNCH   # 注入：港股也用并集窗（11:30–12:00 误豁免）',
     "[新鲜度字段透传] 港股午休窗必须与 A 股分开（11:30–12:00 港股仍在交易）",
     "午休"),

    # ⑨ slot_expired 分支漏摘 hits 键（P0，2026-10-28 修）：与 urgent
    # 分支修过的"同一缺陷的第二个实例"同型，这是第三个 —— 槽位过期提前
    # return 时只摘了 `hourly_<label>`，同轮认领的 hits 键留在 inflight，
    # 那些事件一条都没推、却整天不再触发，8h 后还会被转正成"已完成"。
    ("slot-expired-keeps-hits",
     "analyze.py",
     '                _hk_drop = {_hk} | {h[0] for h in hits}',
     '                _hk_drop = {_hk}   # 注入：不再摘 hits 键',
     "[内容失效复查] 槽位过期时同轮认领的 hits 键也必须摘掉"
     "（否则事件整天静默、面板还显示已完成）",
     "同轮认领的 hits 键"),

    # ⑩ 本地启动失败被当成通道故障（P2，2026-10-28 修）：`Popen` 抛的
    # E2BIG/ENOENT/EACCES 全是本机问题，重试与熔断都不会自愈；原先走
    # 通用分支被判「网关/上游断流」并写 10 分钟熔断，排查方向被引向网络。
    ("popen-fail-trips-breaker",
     "analyze.py",
     '            if (_se or "").startswith("Popen 失败"):',
     '            if False:   # 注入：本地启动失败也当通道故障',
     "[AI 熔断] 本地启动失败（Popen 抛异常）不得写熔断"
     "（真因在本机：权限/参数长度/磁盘）",
     "本地启动失败"),

    # ===== 结果性判据（2026-10-30 加）=====
    #
    # ⚠️ 这一组守的是 2026-10-29 那场「13 轮 error、09:25 简报永久丢失、
    # 而自检零信号」暴露的**结构性缺口**：本文件此前只有过程性判据。
    # 它们与 `_starved_verdict`/`_gap_verdict` 同型 —— 异常形态只在特定
    # 历史时刻成立，沙箱造不出来，故靠构造值 + 接线断言进射程。
    # 变异体直接改**判据函数本体**，让构造值自测红。

    # --- 异常轮次阈值被抹平（[投递结果判据] 应抓）---
    ("delivery-err-streak-always-ok",
     "selfcheck.py",
     '    return "fail" if n_err >= _ERR_ROUND_ALERT else "warn"',
     '    return "ok"   # 注入：异常轮次永不报警',
     "[投递结果判据] 连挂 13 轮不得判 ok（2026-10-29 事故形态）",
     "投递结果判据错"),

    # --- 槽位窗口判据失效（[投递结果判据] 应抓）---
    ("delivery-slot-due-never",
     "selfcheck.py",
     '        if now_hm >= _due and _slot not in pushed:',
     '        if False:   # 注入：漏投永不报警',
     "[投递结果判据] 过了宽限仍无推送记录的槽位必须报缺失",
     "投递结果判据错"),

    # --- 槽位窗口提前（边界松一档，[投递结果判据] 应抓）---
    # 只把 09:25 的关闭时刻提前 1 分钟 —— 若判据用 `>` 而非 `>=`（或反过来
    # 差一分钟），09:47 那条「尚未到点」用例就会红。
    # ⚠️ 这是"边界值"型注入，专抓 off-by-one 的退化。
    ("delivery-slot-due-off-by-one",
     "selfcheck.py",
     '    "09:25": 9 * 60 + 48,',
     '    "09:25": 9 * 60 + 47,   # 注入：关闭时刻提前一分钟',
     "[投递结果判据] 槽位关闭时刻的边界（09:47 未到点 / 09:48 到点）",
     "投递结果判据错"),

    # --- 生产接线被拔（[投递结果判据] 的接线断言应抓）---
    # 判据函数保持完好，只删掉 `[投递结果]` 段里的**调用** ——
    # 构造值自测全绿而生产永远不报，正是"判据在、调用没接上"那种退化。
    ("delivery-prod-call-unwired",
     "selfcheck.py",
     '            _ev_dl = _error_streak_verdict(_n_err_dl)',
     '            _ev_dl = "ok"   # 注入：判据不再被生产调用',
     "[投递结果判据] 接线断言（判据必须真的接进 [投递结果]）",
     "投递结果判据错"),

    # ===== hist 新鲜度闸门（2026-10-30 收盘轮 P0 修复）=====
    #
    # 守的是 10-30 事故：配额耗尽 → hist 停在 14:44 → 15:04 的「收盘定局」
    # 把旧值当现价播。判据两层：`_hist_stale_verdict` 三态判定（marketlib）
    # + build_brief 的「末帧」渲染接线（analyze）。下面两条各打一层。

    # --- 阈值被放大到永远 live（[快照复用] ⑨ 的构造值应抓）---
    ("hist-freshness-threshold-neutered",
     "marketlib.py",
     'HIST_MAX_AGE = 360.0',
     'HIST_MAX_AGE = 1e9   # 注入：新鲜度上界失效，任何时刻都判 live',
     "[快照复用] hist 末帧超 360s 必须判 stale"
     "（阈值失效 = 14:44 旧值又被当现价）",
     "hist 17 分钟前应判 stale"),

    # --- 判据在、渲染不判（[快照复用] ⑩ 的行为断言应抓）---
    # 函数本体一字不动，只让 build_brief 拿到的 verdict 恒为 live ——
    # "判据存在但没人用"的退化，纯函数自测 ⑨ 对此无感觉。
    ("hist-stale-verdict-not-wired",
     "analyze.py",
     '    _hv, _hlast_t, _h_age_min = M._hist_stale_verdict(h_, now=now)',
     '    _hv, _hlast_t, _h_age_min = ('
     '"live", (h_[-1].get("time") if h_ else None), 0.0)'
     '   # 注入：不判新鲜度',
     "[快照复用] hist 陈旧时【盘中变化】必须走「末帧」+ 采集中断警告"
     "（接线被拔 = 10-30 收盘轮旧值当现价播）",
     "陈旧 hist 未走"),

    # --- 收盘定局数据闸门（2026-10-31 加，[收盘定局数据闸门] 应抓）---
    # 四条分别对应闸门的四个可独立退化的环节：判据范围 / live 口径 /
    # 接线 / 痕迹处置。只断行为、不断源码。
    ("close-slot-scope-any-slot",
     "analyze.py",
     '    if slot != "15:00":\n        return False',
     '    if slot is None:   # 注入：闸门扩到所有整点槽位\n'
     '        return False',
     "[收盘定局数据闸门] 闸门只对 15:00 生效（扩到 10:00 等槽位 = 盘中"
     "数据一抖动就静默丢整点播报，下一轮不再是同一时段）",
     "10:00 q 全空被误拦"),

    ("close-slot-live-ignores-f2",
     "analyze.py",
     '        if isinstance(_r, dict) and M.isnum(_r.get("f2")):\n'
     '            return False\n    return True',
     '        if isinstance(_r, dict):   # 注入：行存在就算有货\n'
     '            return False\n    return True',
     "[收盘定局数据闸门] 「可用现价」必须认 f2 数值（口径同 build_brief 的"
     " _q_live），只判行存在 = 残缺行被当成有收盘数据、误杀正常收盘轮",
     "f2='-'（接口返回非数值）不算可用现价"),

    ("close-slot-gate-unwired",
     "analyze.py",
     '    if not dry and _close_slot_stale(slot, q):',
     '    if not dry and False and _close_slot_stale(slot, q):'
     '   # 注入：闸门接线断',
     "[收盘定局数据闸门] 闸门必须真的接进 _run_after_inflight（判据在、"
     "接线断 = 10-30「旧值当收盘播」原样复活）",
     "15:00 q 全空仍推送"),

    ("close-slot-drops-retry-credential",
     "analyze.py",
     '        # hourly_retry **不 pop** —— 见上方「保留」的论证。',
     '        st.get("hourly_retry", {}).pop(hourly, None)   # 注入：对齐 slot_expired',
     "[收盘定局数据闸门] 拦截时**保留** hourly_retry（取数失败是瞬时的，"
     "比对错了对象——照抄 slot_expired 的 pop = 数据恢复后也不再重试）",
     "拦截后 hourly_retry 被清掉"),

    # --- 2026-11-05 批量（P0 与优先 P1）---
    # ① 槽位内容失效复核只认 hourly → summary/deadline 走 hits 认领时整段
    #   漏查（AI 取消总时长上限后，summary 可拖过 15:13 的失效时刻）。锚点
    #   取 `_chk_slot = hourly` 那行：改回直接查 `hourly` 会让 hits 认领的
    #   摘要槽位不再进复查 —— 自检必须抓到（判据在 [收盘定局数据闸门] ④⑤）。
    ("slot-expiry-hits-claimed-implement-independent-uncovered",
     "analyze.py",
     '    if not _chk_slot:\n'
     '        for _sk, _sv in hits:',
     '    if False:   # 注入：不认 hits 里的语义槽位（只认 hourly）\n'
     '        for _sk, _sv in hits:',
     "[收盘定局数据闸门] 摘要/截止(走 hits 认领)槽位必须同样受失效复核约束"
     "（改回只查 hourly = summary 过期照发）",
     "summary 内容过失效时刻（15:13）后 status 应为"),

    # WebFetch 域名守卫接线（2026-11-05）：去掉分支 = 注入外带通道复开
    ("webfetch-domain-guard-unwired",
     "agent/src/mw_agent/__main__.py",
     '        if _name == "WebFetch":',
     '        if False:   # 注入：域名守卫接线断',
     "[agent 摘要链路] WebFetch 必须走域名白名单（Read 已限死路径，"
     "WebFetch 是唯一能\"把本机内容送出\"的出口）",
     "WebFetch 守卫没接线"),

    # ②b 范围判据恒假（2026-11-05）：当日全部 no_webhook 的根因永远不报
    ("nowebhook-scope-blind",
     "selfcheck.py",
     '        return bool(is_td and len(list(due_slots or [])) > 0)',
     '        return bool(False)   # 注入：范围恒不成立',
     "[投递结果] webhook 缺失导致的整日漏投必须被点名（根因不再被\"槽位\n"
     "漏投\"的含糊话术盖住）",
     "no_webhook 范围判据"),

    # agent 生产语境门接线（2026-11-05）：去掉 = 沙箱/手工跑真推群
    ("agent-prod-gate-unwired",
     "agent/src/mw_agent/__main__.py",
     '    if not _prod:\n        _tag("not_prod")',
     '    if False:   # 注入：生产语境门去掉\n        _tag("not_prod")',
     "[agent 摘要链路] 摘要推送必须限 launchd 生产语境（铁律：只有生产"
     "可推；沙箱/IDE 跑会真发群）",
     "非生产语境未被 _push_summary 拦下"),
]

# ── 五元组变异体的 must_contain 补齐（2026-11-12）──
#
# ⚠⚠ 背景：`MUTANTS` 里有 **105 条只有五个元素**（无
# `must_contain`）。`_run_one_mutant` 的判据于是退化成：
#
#     got = bool(new_bad) or bool(new_unv) or (_b_rc == 0 and rc != 0)
#
# 也就是「只要相对基线多出**任何**一条 ❌ 或「未验证」就算抓住」。
# 后果是**假绿**：它们会被一条不相关的错误行遗带过关
# （baidu 风控抖动、AI 冒烟卡死、网络抖动 —— 本会话三类都实际出现过）。
# 假绿比漏网**更危险**：漏网会让人去查，假绿不会。
#
# 实测统计（AST 扫 MUTANTS）：6 元組 274 条 / 5 元組 105 条。
#
# 为什么用**独立映射表**而不把 105 条改成六元組：
#   ① 模式化重写 105 条源码元组，每条都是一次改坏的机会；
#   ② 这张表**不改变元组的 repr** → 基线里的 `mutants` 哈希不变；
#   ③ 归因集中在一处，日后对文案变动可一目了然地同步。
#
# 每条短语都是**实测采集**的：逐条注入变异、跑自检、取它报出的
# **首条新增 ❌ 明细行**，再取一段**不含数字/引号/括号**的稳定片段
# （避开会随运行变化的计数与 repr），并确认它**不在干净树输出里**
# —— 否则会永远匹配成功，又是一种假绿。
#
# ⚠ 仍有 7 组重复：那些变异体真的会触发**同一句报错**
# （如 `_snap_stubs` 名单缺失、创建函数权限不对），文本层面无法区分。
# 要彻底归因，得让框架记录「期望段」而不只是文案。
_MUST_EXTRA: dict[str, str] = {
    "advice-back-to-empty-phrases": "【无数据路径】兜底模板建议段是空话",
    "advice-word-banned": "误拦 减仓",
    "agent-verdict-always-pending": "判据自测「非交易日、从来没跑过」得 pending，期望 missing —— `_agent_run_verdict` 的分支被改坏了",
    "ai-meta-multi-sep": "元说明块处理错误[多段分隔线",
    "arity-mismatch": "marketlib.threshold() 传 3 个位置参数",
    "audit-trail-stops": "执行流水**停写**",
    "baidu-silent-fail": "种失败形态未留痕",
    "claude-bin-missing": "claude_bin 重试行为异常：桩被调",
    "claude-bin-no-retry": "claude_bin 重试行为异常：桩被调",
    "clist-unavailable": "clist 不可用 —— 本项**本轮未验证**（热点候选池/板块数据将缺失；非配额原因）",
    "collect-hits-callsite-bypass": "顶层 _log_events_once() 无任何引用",
    "collect-hits-log-undeduped": "collect._main 三次相同触发触发",
    "cross-scope-name-ref": "检查项自身异常（本项未生效）: NameError: name",
    "daylow-term-rule-removed": "prompt 未写明「",
    "deadline-dates-empty": "deadline_dates 为空 —— 计划窗口最后一日不会有截止提醒",
    "digest-not-injected": "ai_analyze 未调用 _today_push_digest —— 「不要重复」这条规则仍然不可执行",
    "dry-advances-pending": "dry 轮推进了防抖闸门 pending_idx",
    "dry-claims-inflight": "让位块的 dry 轮仍写了 state（",
    "dry-general-status-ok": "通用 dry 分支没写 skip_dry（写成 ok 就与真实成功轮同形，面板把演练显示成「已执行」）",
    "dry-hourly-claims": "hourly dry 轮仍写了 state（",
    "dry-kind-missing": "通用 dry 分支缺 kind",
    "dry-kind-overwritten-by-urgent": "dry 轮 kind",
    "dry-run-flag-left-on": "DRY_RUN 标志存在且未过期（",
    "dry-urgent-status-ok": "dry 轮 status=",
    "dry-urgent-writes-empty-inflight": "dry 轮仍写了 state（",
    "dry-writes-empty-inflight": "hourly dry 轮销毁了盘上原有的 inflight 去重凭据 —— 现为",
    "dry-yield-leaves-retry": "让位块的 dry 轮仍写了 state（",
    "em-hosts-perm-unsorted": "排在候选**首位** —— 排序层未判 PERMANENT",
    "esc-drop-quote": "—— 属性值可被闭合注",
    "ev-alias-drop": "处不一致（会导致面板查不到已触发的",
    "fc-fail-not-persisted": "冷却期内仍发起了",
    "fresh-limit-huge": "FRESH_LIMIT_IN_SESSION",
    "fstring-unbound-name": "marketlib.py: log",
    "fund-missing-rule-removed": "【例外】未覆盖资金字段缺失 —— AI 可能重复写资金占位语",
    "grace-cap-disabled": "—— 宽限会吃掉别人的时段（_grace_cap / SLOT_TTL 被改坏",
    "grep-noise": "处「未验证」出现在成功/无关路径上（所在分支没有 _unverified 计数",
    "harden-log-overlist": "webui.py 的 harden_logs 里有多余的名字",
    "harden-log-underlist": "com.mison.marketwebui.plist 的 StandardErrorPath",
    "has-data-always-true": "项响应形状处理有问题：",
    "hit-msg-subject-dropped": "hstech_drop 的消息",
    "hk-failclosed-only-in-except": "项港股降级语义有误",
    "hk-gate-missing-analyze": "analyze 在港股开盘前（",
    "hk-gate-overkill-analyze": "analyze 的港股闸门把 A股板块信号也挡掉了（",
    "holiday-table-extra-day": "休市））被判成**休市** —— HOLIDAYS 多填了它，该日不会采集、不",
    "holiday-table-missing-day": "（国庆末日）被判成**交易日** —— HOLIDAYS 漏填了它，该日会空跑一",
    "holidays-empty": "（国庆首日）被判成**交易日** —— HOLIDAYS 漏填了它，该日会空跑一",
    "hostfail-drop-all-on-bad": "逐条容错判据不成立，但未定位到具体原因（读到 host_fail 键",
    "hstech-threshold-none": "个阈值函数返回值不可用于比较（调用方是裸比较，None 会 TypeError",
    "json-shape-502": "探针未真正执行（subprocess.run",
    "log-dedup-off": "次日志（应为 1）—— 去重未跨调用生效",
    "meta-guard-miss-section": "处 print 以换行开头、却不像检查段标题（应为 `\\n",
    "meta-guard-unfailable-section": "False 也无 _unverified 计数 —— 无论被测系统怎么坏都不会报",
    "mk600-back-to-open": "runlog._write600",
    "module-import-broken": "个被桩符号不在 _snap_stubs 名单里（还原漏了也",
    "news-raises": "快讯取数异常: RuntimeError: 注入：news 崩溃",
    "news-str-no-isinstance": "条：该条应被 `_str` 归一为",
    "news-time-not-str": "news.time 不是字符串: int",
    "none-ts-key-mismatch": "alert 的失败记忆读不回来（写用 alert:",
    "none-ts-no-prune": "写入后仍留着过期/非法条目",
    "norm-title-lead-blank": "_norm_title 未纠正前置空行的标题:",
    "norm-title-noop": "**午后开局 · 指数",
    "openlockfile-back-to-open": "marketlib._open_lockfile",
    "overseas-fx-misuse": "OVERSEAS 含汇率误用标的",
    "panel-fired-label-dropped": "—— fired 是去重记录，每少显示一个键就等于「有一条消息发过却查不到轨迹",
    "panel-ignores-run-dry": "（应 skipped：runs 落了 skip_dry 却没被认出来，面板会把演",
    "panel-trusts-fired": "而当日无该槽位的成功推送记录，面板却报 done —— 消息未发出却显示已完成",
    "plan-md-stale-position": "plan.md 的市场位置停在",
    "plan-md-threshold-drift": "hstech_down: plan.md 内部**自相矛盾**，同时写了",
    "premarket-tag-always": "盘前/收盘标注：16:01",
    "premarket-tag-slot-only": "盘前/收盘标注：事件类",
    "prodmutation-guard-blind": "_HOST_FAIL_FILE 的探针没有重定向到临时文件（AST 里找不到 `_redirect",
    "prompt-no-exception-rule": "【例外】未覆盖标注形态「数据未取到」—— AI 见到它仍会编",
    "push-punct-allow": "未被拦下，runlog",
    "q-no-shape-guard": "抛 TypeError",
    "quotes-always-empty": "取到 0 项，源=",
    "ratelimit-cap-zero": "不是正整数 —— 配额闸门失效",
    "return-contract-false": "返回非 int 常量（Consta",
    "rmap-dry-misplaced": "—— 历史错位的形态：演练文案挂在 ok 下、真 skip",
    "rmap-empty-value": "rmap 与 runlog.Status 不一致：",
    "round-ai-ms-clock-mixed": "AI 熔断判据错: AI 产出成功后**没有**调用 ai_fail_clear —— 通道恢复后熔断不会被解除，还要白白发",
    "round-data-ms-clock-mixed": "）—— 起点与终点用了**不同的钟**（墙钟 vs `time.monotoni",
    "sched-calendar-dead": "com.mison.marketwatch: 用的是 StartCalendarInterval —— 本机该方式**全局不投递**（历史故障），应为 StartInterval",
    "sched-phantom-job": "）—— 漏键的 job 其脚本名/调度间隔**完全不被校验*",
    "sched-wrong-script": "com.mison.marketwatch.plist 的 StandardOutPath",
    "selfcheck-lock-back-to-open": "个状态/日志/锁文件带 group/other",
    "sigma-copied-not-shared": "处不同源（会导致推送与快照判定分叉",
    "snapshot-crossday": "昨天的快照被当成今天复用 —— date 闸门失效",
    "snapshot-hk-open-boundary-off": "跨港股开盘的盘前快照仍被复用 —— 昨收会被当成实时港股行情推送",
    "state-hourly-retry-type": "hourly_retry 非字典: list",
    "storage-gate-off": "_storage_from_snapshot",
    "stub-list-drop": "个被桩符号不在 _snap_stubs 名单里（还原漏了也",
    "stub-list-hot-missing": "个被桩符号不在 _snap_stubs 名单里（还原漏了也",
    "threshold-default-drift": "处（不影响运行，属一致性维护项）：",
    "trading-day-always-true": "（国庆首日）被判成**交易日** —— HOLIDAYS 漏填了它，该日会空跑一",
    "ts-drop": "这些取数函数构造的行情行没有 _ts，新鲜度闸门对其失效：_hk_quotes_via_tencent",
    "unbound-call": "处 —— 运行期必抛 NameError，且多半被 except 吞掉（功能静",
    "urgent-no-return": "—— 补推宽限的凭据被误清，槽位只能以事件类补发（会占配额",
    "version-check-silent-pass": "+，自检无法运行；四个 job 应指向 /usr/local/bin/pytho",
    "vr-base-toplevel-list": "处缓存文件形状异常",
    "watchlog-rotate-os-replace-broken": "marketlib._rotate_log: 没有生成 `.",
    "webhook-missing": "config.json 缺少 webhook → 推送必失败",
    "webhook-url-malformed": "webhook URL 结构不对: 路径是",
    "win-end-no-grace": "个槽位的宽限是死代码（main 先 return out_of_window）",
    "workdoc-advice-clause-removed": "「群聊安全规则」段缺少「操作建议词是允许的」这句声明 —— prompt 第",
    "workdoc-guard-window-drift": "保护时段 guard_close 应为",
}

def _copytree_tolerant(src, dst):
    """copytree，容忍**并发消失**的文件（沙箱是从**活的**生产目录拷的）。

    ⚠️ 不这么写会让整个回归当场崩溃、且**已跑完的结果全丢**。
    `[实测]` 2026-10-08 22:04：跑全量回归时生产 `marketselfcheck`
    （launchd 每天 21:39 一次）恰好启动，它在 `cache/` 里建又删
    `_sc_shape_*.json` / `.lock` 探针文件，与 `copytree` 撞上——
        shutil.Error: [.../cache/_sc_shape_rate.json, ... No such file or directory]
    回归在某个变异体的 `make_sandbox` 里抛出，traceback 走 stderr、
    而 stdout 是块缓冲 —— 之前几十个变异体的结果一行都没留下。
    消失的文件对测试毫无价值，重试即可；仍失败就逐文件拷、缺的跳过。

    配套：`main()` 里把 stdout 改成行缓冲（见那里），崩溃时不再丢结果。
    """
    import time as _t
    for _try in range(4):
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return
        except shutil.Error:
            _t.sleep(0.5)
        except FileNotFoundError:
            _t.sleep(0.5)
    os.makedirs(dst, exist_ok=True)
    for _root, _dirs, _files in os.walk(src):
        _rel = os.path.relpath(_root, src)
        _tgt = dst if _rel == "." else os.path.join(dst, _rel)
        try:
            os.makedirs(_tgt, exist_ok=True)
        except OSError:
            continue
        for _f in _files:
            try:
                shutil.copy2(os.path.join(_root, _f), os.path.join(_tgt, _f))
            except OSError:
                pass


_SANDBOX_SWEPT = [False]


def _sweep_stale_sandboxes():
    """清掉**上一次**回归崩溃/被 kill 时留下的沙箱（`[实测]` 2026-10-22）。

    ⚠️ 为什么需要：`make_sandbox` 建的目录只在变异体正常跑完的 `finally`
    里 rmtree。回归被 Ctrl-C / kill / 崩溃时，那条路径不执行 —— 每个沙箱
    是一个完整的生产拷贝（含 cache/ history/ `__pycache__`），实测单个数 MB。
    `[实测]` 2026-10-22 我这轮调试手动跑了十几次 make_sandbox，
    攒下 **69 个目录 / 752 MB**，全在 `$TMPDIR` 里没人管。

    只在**首次** make_sandbox 时扫一次（`_SANDBOX_SWEPT`）：
      · 每次调用都扫 = 245 次 glob，白花时间；
      · 但必须在**本轮开始前**扫 —— 本轮正在用的沙箱也在同一个前缀下，
        不设防会把它们删掉。

    ⚠️ 判据用**年龄**而不是"当前进程不用的"：本轮第一个沙箱还没建，
    此刻所有 `mw_mut_*` 都是历史残留。给 1 小时宽限纯粹是防御
    （万一同时跑着另一个回归 —— 虽然框架明令串行，但人手动跑两次时
    不该互相删对方正在用的目录）。
    """
    if _SANDBOX_SWEPT[0]:
        return
    _SANDBOX_SWEPT[0] = True
    try:
        import glob as _gb
        import time as _tb
        _old = _tb.time() - 3600
        _base = tempfile.gettempdir()
        for _d in _gb.glob(os.path.join(_base, "mw_mut_*")):
            try:
                if os.path.isdir(_d) and os.path.getmtime(_d) < _old:
                    shutil.rmtree(_d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


def make_sandbox(fresh_pending=False):
    _sweep_stale_sandboxes()
    S = tempfile.mkdtemp(prefix="mw_mut_")
    # ⚠️ 2026-10-31 分层：沙箱必须**镜像生产的层结构**（src/ conf/ state/ logs/）。
    # 若仍把 .py 拷到沙箱根，selfcheck 在沙箱里 `import paths` 会失败、
    # `os.path.join(DIR,"conf","config.json")` 找不到文件 —— 全段降级成
    # 「未验证」，基线全绿但判据空转（零覆盖），比报错更危险。
    for _d in ("src", "conf", "state", "state/cache", "state/history", "logs"):
        os.makedirs(os.path.join(S, _d), exist_ok=True)
    # src/：11 个主模块（变异体注入目标 + selfcheck 的源码锚）
    for f in os.listdir(SRC):
        p = os.path.join(SRC, f)
        if os.path.isfile(p) and (f.endswith(".py") or ".bak-" in f):
            shutil.copy2(p, os.path.join(S, "src", f))
    # conf/：config.json sectors.json plan.md claude-plan.md private-notes.md
    _cd0 = os.path.join(ROOT, "conf")
    for f in os.listdir(_cd0):
        p = os.path.join(_cd0, f)
        if os.path.isfile(p) and (f.endswith(".json") or f.endswith(".md")):
            shutil.copy2(p, os.path.join(S, "conf", f))
    # state/ 顶层：state.json rate.json pending.json hot_cache.json vr_base.json DRY_RUN
    _sd0 = os.path.join(ROOT, "state")
    for f in os.listdir(_sd0):
        p = os.path.join(_sd0, f)
        if os.path.isfile(p) and (f.endswith(".json") or f == "DRY_RUN"):
            shutil.copy2(p, os.path.join(S, "state", f))
    # logs/watch.log（*.out/*.err 由下方自造）
    _wl0 = os.path.join(ROOT, "logs", "watch.log")
    if os.path.exists(_wl0):
        shutil.copy2(_wl0, os.path.join(S, "logs", "watch.log"))
    # ⚠⚠ 2026-11-08（复核）：**轮转后的 .1 也必须镜像**。
    # `watch.log` 超 MAX_BYTES 会轮转成 `watch.log.1`，而 `webui` 的结论提取
    # 本来就读 `[LOG1, LOG]` 两个文件（见 webui 的 `files = [LOG1, LOG] ...`）。
    # 只抄 `watch.log` 时，一旦生产刚好轮转，沙箱就**丢了历史覆盖** ——
    # `[面板时段归因]` 里「埋点前回退路径」那条断言（硬编码拿
    # 2026-09-23）就拿不到结论 → 基线报❌ → **整轮回归结果作废**。
    # `[实测]` 2026-11-08 20:18 生产 watch.log 轮转，回归随即在基线阶段中止。
    _wl1 = os.path.join(ROOT, "logs", "watch.log.1")
    if os.path.exists(_wl1):
        shutil.copy2(_wl1, os.path.join(S, "logs", "watch.log.1"))
    # ⚠️ 把 `__pycache__` 一起拷（2026-10-22 实测）：
    # 沙箱首跑要把 marketlib/analyze/collect 等 11 个模块**从源码重新编译**，
    # 实测冷启动 96.1s vs 热 28.3s —— 每条变异体付一次 68 秒，
    # 246 条就是 4.6 小时纯编译。
    #
    # `copy2` 保留 mtime，而 pyc 的有效性判据正是「源文件 mtime + size」，
    # 所以**没被改动的模块**会直接命中缓存；被变异体改过的那个文件
    # mtime 变了 → 自动重编译（正确行为，不会用到过期字节码）。
    #
    # ⚠️ 这是**只读**拷贝：生产 `__pycache__` 里若有半个 pyc 也只是当未命中。
    try:
        _copytree_tolerant(os.path.join(SRC, "__pycache__"),
                           os.path.join(S, "src", "__pycache__"))
    except Exception:
        pass          # 没有 __pycache__ 也无所谓，退化为每次编译
    # ⚠️ `cache/pre-opt-backup-*` **不拷**（2026-10-28）：14 个目录 ≈ 30MB，
    # 占 `cache/` 总体积的 90%+，而**没有任何检查项读它们**（磁盘卫生的
    # 扫描判据是 `backup-*`，前缀对不上，数不到它们；`pre-opt-backup` 是
    # 优化前源码快照，与自检无关）。实测跳过前后沙箱自检输出**逐行一致**
    # （只差时间戳与「几分钟前」这类实时量），make_sandbox 1.9s → 1.25s。
    # 295 条 × 每条省 0.6s ≈ 3 分钟；并行后是省在每个 worker 的 I/O 上。
    def _copy_cache(_src, _dst):
        os.makedirs(_dst, exist_ok=True)
        for _e in os.listdir(_src):
            if _e.startswith("pre-opt-backup-"):
                continue
            _p = os.path.join(_src, _e)
            if os.path.isdir(_p):
                _copytree_tolerant(_p, os.path.join(_dst, _e))
            else:
                try:
                    shutil.copy2(_p, os.path.join(_dst, _e))
                except OSError:
                    pass
    _copy_cache(os.path.join(ROOT, "state", "cache"),
                os.path.join(S, "state", "cache"))
    _copytree_tolerant(os.path.join(ROOT, "state", "history"),
                       os.path.join(S, "state", "history"))
    # ⚠️⚠️ **沙箱里的 webhook 必须一开始就没有**（2026-10-20 修）。
    #
    # 原先这一条写在 `run()` 之后的模块级代码里（`_f.write('{"webhook": ""}')`），
    # 而那行只在 make_sandbox **之后**执行一次 —— 于是：
    #   · 模块级那次给沙箱根 S 置了空 webhook；
    #   · 但每个变异体的 `make_sandbox()` 新建的 S 是**生产 config.json 的
    #     逐字拷贝，含真实 webhook**；
    #   · `run()` 直接用这个 S 起 selfcheck —— **从来没人再置空过**。
    # 实测 2026-10-20 09:49：单跑三条 push-guard 变异体期间，群里连收
    # 两条「自检异常（rc=1）」。那两条**不是误报**：`push-guard-*` 变异体
    # 每个都把自检打得 rc=1（沙箱 launchd 夹具里 selfcheck rc=1 是设计内，
    # 但【推送守卫】段因此报 ❌ → ok_all=False → rc=1），而该变异体的 S
    # 里带着真 webhook → 每抓住一条 = 往真实群推一条。
    # 这直接违反用户铁律「除了正式程序，其他任何时候都不该推真实 webhook」，
    # 而且是**回归自己制造的刷屏**。
    # ⚠️ 置空动作搬进 make_sandbox 本体，紧跟在拷 config.json 之后 ——
    # 这样**每一个**沙箱（包括每个变异体新建的）天生就是假 webhook。
    # ⚠️ 不能直接置 `""`：`[推送通道]` 段会报「config.json 缺少 webhook」
    # → 基线带 ❌ → 闸门当场中止（那是**检查项**，不是泄漏面）。
    # 改成**结构合法、key 无效**的地址：
    #   · scheme/path/host/key 四项结构判据全过 → `[推送通道]` 照常 ✅
    #   · 主机可达性照常测（真的 curl qyapi 根路径）
    #   · 真发出去企微会返回 errcode 93000（invalid webhook key），**不进群**
    # 这是"能测检查逻辑、但物理上不可能打扰用户"的唯一交集。
    _cfg_s = os.path.join(S, "conf", "config.json")
    if os.path.exists(_cfg_s):
        import json as _js_hk
        try:
            _dcfg = _js_hk.load(open(_cfg_s, encoding="utf-8"))
        except Exception:
            _dcfg = {}
        _dcfg["webhook"] = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                            "?key=00000000-0000-0000-0000-000000000000")
        # 0o600：`[状态文件权限]` 会把组/其他可读的配置报红（纵深防御）。
        _fdc = os.open(_cfg_s, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(_fdc, "w", encoding="utf-8") as _fcw:
            _js_hk.dump(_dcfg, _fcw, ensure_ascii=False)
    # ⚠️ 磁盘卫生项需要**多个**时间戳备份才能触发轮转逻辑 —— 真实生产
    # 有 4 个，若只靠拷贝现有的，某些时刻可能不足 3 个而测不到轮转。
    # 这里额外造几个合成的（纯数字名，才匹配 `_stamp_only` 正则），
    # 确保轮转路径一定被执行。
    for _i in range(5):
        _bp = os.path.join(S, f"selfcheck.py.bak-99999999{_i:02d}")
        open(_bp, "w").write("# synthetic backup\n")
    # ⚠️ `selfcheck.out` 必须在沙箱里**造出来**（2026-10-19）：上一补丁
    # 新增的 `[自检新鲜度]` 真实路径要读它，而本函数只挑
    # `.py/.json/watch.log/…` 后缀 —— 不造的话该段在回归里恒走
    # "无历史 → 未验证"，正则被改坏没有任何变异体会变（射程空）。
    # 造**两个**标记（昨天 / 今天）：判据窗口是 (最近标记日, 今天) 开区间，
    # 两个标记让它恒为空；只造"昨天"一个的话，回归跑过零点后窗口会多出
    # "创建日"这一天，而沙箱 runs.jsonl 是生产快照、确有该日记录 →
    # 凭空报「自检漏跑」→ 基线冻结、此后每条变异体被假判"抓住"。
    # 权限 0600：`[状态文件权限]` 扫 `.out`，644 会让基线带 ❌。
    import datetime as _dt_seed
    _seed_now = _dt_seed.datetime.now()
    _fd_so = os.open(os.path.join(S, "logs", "selfcheck.out"),
                     os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(_fd_so, "w", encoding="utf-8") as _fh_so:
        for _dd in (_seed_now - _dt_seed.timedelta(days=1), _seed_now):
            _fh_so.write(
                f"=== 盯盘系统自检 · {_dd:%Y-%m-%d} 06:00:00 ===\n")
    # ⚠️ 沙箱的 `history/runs.jsonl` 里**抹掉今日的 starved_slots**
    # （2026-10-19）。该字段记的是「上一轮太慢、launchd 丢了几个触发」，
    # 而 `_STARVED_ALERT=8` 的判据是**当日累计计数**——生产上真出现一次
    # 慢轮日（今天 16 次），基线就带上一条真 ❌，回归按设计当场中止，
    # 于是"系统今天确实退化"把整轮回归夹死。
    # 这不是放松判据：变异体 `starved-threshold-removed` 靠的是
    # **构造值**（`_starved_verdict` 的 7 例 + AST 接线断言），与生产
    # 当日是否有慢轮无关；而真实数据留在这里只会制造不可复现的红。
    # 只改**今天**的记录：历史日保留原样，`[自检新鲜度]` 等按日判定的
    # 检查项仍然看到真实分布。
    #
    # ⚠️ **同一个理由适用于当日 `status=error` 的 analyze 记录**（2026-10-30）：
    # `[投递结果]` 的「异常轮次 ≥3 判 fail」读的也是当日累计，而 10-30 当天
    # 生产真出现过连续 error（10-29 那场事故就是 13 轮）—— 沙箱原样拷进来
    # 就会让基线带 ❌、整轮回归中止。同样是「生产当日退化把回归夹死」，
    # 同样用**构造值 + 接线断言**保证判别力。归一为 `status="ok"` 而不是
    # 删行：删行会改变 `[执行流水新鲜度]` 的轮次计数与 `gap_s` 序列。
    #
    # ⚠️ **第三个同族成员：`gap_s`**（2026-10-30）。`[执行流水新鲜度]` 的断档
    # 判据改成了**滚动 24 小时窗**（原先只扫"今天"，14:25–15:35 永远盲），
    # 于是沙箱里**昨天**的真实 gap 也进了判据。生产若在最近 24 小时里出过
    # 一次 >15 分钟的真空档（10-21 就有 42.6 分钟、10-25 有 45 分钟），
    # 基线就带真 ❌、闸门中止 —— 又一次"生产退化把回归夹死"。
    # 归一方式：窗口内的 `gap_s` 一律**夹到 300s**（正常轮次间隔），不删字段
    # （删了 `runlog-gap-not-recorded` 那条夹具就没了，且序列长度会变）。
    # 判别力不受影响：`gap-threshold-never-fires` / `gap-session-filter-removed`
    # 抓的是 `_gap_verdict` / `_in_session_t` 的**构造值**用例 + AST 接线断言，
    # `runlog-gap-not-recorded` 抓的是**行为锚**（沙箱里现写两条 end 记录再读回），
    # 三者都与生产快照里的历史 gap 值无关。
    try:
        import json as _js_seed
        import datetime as _dt_seed2
        _rp_seed = os.path.join(S, "state", "history", "runs.jsonl")
        _today_seed = _dt_seed2.date.today().isoformat()
        _win_seed = (_dt_seed2.datetime.now()
                     - _dt_seed2.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        _d_seed_iso = (_dt_seed2.datetime.now()
                       - _dt_seed2.timedelta(hours=24)).date().isoformat()
        if os.path.isfile(_rp_seed):
            _keep = []
            with open(_rp_seed, encoding="utf-8", errors="replace") as _fh_r:
                for _ln_seed in _fh_r:
                    _ln_seed = _ln_seed.rstrip("\n")
                    # 只解析可能被改的三类行（全文件 ~2.5 万行，245 个沙箱都要
                    # 走一遍 —— 条件放宽到"每行都 parse"会白付分钟级开销）：
                    #   · 今日的 starved_slots / status=error
                    #   · 今日的 ai_fallback（P1 兜底率判据用；见下）
                    #   · 窗口内（今天或昨天）的 gap_s
                    _need = (
                        (('"starved_slots"' in _ln_seed
                          or '"error"' in _ln_seed
                          or '"ai_fallback"' in _ln_seed)
                         and _today_seed in _ln_seed)
                        or ('"gap_s"' in _ln_seed
                            and (_today_seed in _ln_seed
                                 or _d_seed_iso in _ln_seed)))
                    if _need:
                        try:
                            _o_seed = _js_seed.loads(_ln_seed)
                        except Exception:
                            _keep.append(_ln_seed); continue
                        _changed = False
                        if (_o_seed.get("starved_slots") and
                                str(_o_seed.get("t", "")).startswith(_today_seed)):
                            _o_seed.pop("starved_slots", None)
                            _changed = True
                        if (_o_seed.get("status") == "error"
                                and _o_seed.get("script") == "analyze"
                                and str(_o_seed.get("t", "")).startswith(_today_seed)):
                            _o_seed["status"] = "ok"
                            _o_seed.pop("note", None)
                            _changed = True
                        # ⚠️ `ai_fallback` 必须**抹掉**（2026-11-03 加，同
                        # `MW_HEALTH_FIXTURE` 的理由）：生产当天若 AI 大面积
                        # 兜底，`[投递结果]` ①b 会在沙箱基线里报 ❌ —— 而
                        # 回归要测的是「检查器能否抓住注入缺陷」，与"此刻生产
                        # AI 健不健康"无关。留着 = 一次生产 AI 故障就让整个
                        # 变异回归中止（基线闸门见脏就停），把判定力绑架到
                        # 外部状态上。抹掉后基线恒定；判据本身的判别力由
                        # `[投递结果判据]` 的构造值用例 + `ai-fallback-verdict-
                        # always-ok` 变异体保证，与历史数据无关。
                        if (_o_seed.get("ai_fallback")
                                and str(_o_seed.get("t", "")).startswith(_today_seed)):
                            _o_seed.pop("ai_fallback", None)
                            _changed = True
                        # 滚动窗内的 gap_s 夹到 300s（见上方注释）
                        _g_seed = _o_seed.get("gap_s")
                        if (isinstance(_g_seed, int) and _g_seed > 300
                                and str(_o_seed.get("t", "")) >= _win_seed):
                            _o_seed["gap_s"] = 300
                            _changed = True
                        if _changed:
                            _ln_seed = _js_seed.dumps(_o_seed,
                                                      ensure_ascii=False)
                    _keep.append(_ln_seed)
            _fd_rp = os.open(_rp_seed, os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(_fd_rp, "w", encoding="utf-8") as _fh_w:
                _fh_w.write("\n".join(_keep) + "\n")
    except Exception as _e_seed_rp:
        print(f"⚠️  沙箱 runs.jsonl 归一失败（基线可能带吞槽位 ❌）: "
              f"{_e_seed_rp}")
    # ⚠️⚠️ **沙箱的日配额必须清零**（2026-10-30 修，P1 假漏网）。
    #
    # `rate.json` 是**当日累计**的生产状态：配额真在 14:49 耗尽过一次
    # （`count=2000/2000`），`make_sandbox` 原样拷进沙箱 → 沙箱里
    # `M.RL.exhausted()` 恒真 → 四处网络检查（[数据源] / [行情取数] /
    # [快讯] / [板块列表]）**全部**走进「配额耗尽 → 本轮未验证」的降级分支，
    # 于是三条变异体哑火：
    #   · `quotes-always-empty`（quotes() 恒空）—— 本应 `❌ 取到 0 项`，
    #     实际与基线同样打「⚠️ 取到 0 项 —— 日配额已耗尽」，无新增失败；
    #   · `feeds-partial-dead`（health 夹具置 2 源假）—— 本应
    #     `❌ 2 个源不可用 … 降级链少 2 档`，实际走 `_quota_out` 分支；
    #   · `clist-unavailable`（clist_available() 恒 False）—— 本应报
    #     「非配额原因 … 未验证」，实际报「配额已耗尽 … 未验证」，
    #     同一段落同一文案，基线差集为空。
    # `[实测]` 2026-10-30 R38：三条全部 `rc=0, ❌数=0, 段=—`。而同日
    # 14:13 的 R37（当时配额未耗尽）是 323 抓 / 0 漏 —— **同一份代码、
    # 两次结果不同，差异全来自生产当日的配额余量**，与
    # `MW_LAUNCHCTL` / `MW_HEALTH_FIXTURE` 是同一个问题的同一解法：
    # 夹具化环境，让判据在恒定输入下可复现。生产不读沙箱，行为不变。
    #
    # 与 `starved_slots` / `status=error` / `gap_s` 的归一**同族但有区别**：
    # 那三条是"生产当日退化把回归夹死"（归一是为了让基线干净），
    # 这一条相反 —— 配额耗尽是**常态**，它把判别力**整个吃掉**却零信号。
    # 归到 `count=0`（而非删文件）：`[限流]` 段读 `M.RL.stats()`，缺文件
    # 会打出 `count=0` 但**日期为空**，`_roll()` 再重算一遍也无害；
    # 显式写当天 0 更贴近"新的一天第一轮"这个真实形态。
    try:
        import json as _js_rate
        import datetime as _dt_rate
        _rf_s = os.path.join(S, "state", "rate.json")
        _fd_rf = os.open(_rf_s, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(_fd_rf, "w", encoding="utf-8") as _fh_rf:
            _js_rate.dump({"date": _dt_rate.date.today().isoformat(),
                           "count": 0}, _fh_rf)
    except Exception as _e_rate:
        print(f"⚠️  沙箱 rate.json 清零失败（网络类变异体可能假漏网）: "
              f"{_e_rate}")
    # ⚠️ 同族的另外三个熔断/冷却状态文件也一并清空（同上理由，弱一档）：
    # `dead_hosts.json`（主机 30 分钟死亡戳）/ `host_fail.json`（跨进程
    # 失败计数，满 6 次熔断）/ `proxy_fail.json`、`firecrawl_fail.json`
    # （90s 冷却）。生产在最近半小时内撞过一次真故障，沙箱里那几条
    # 备用源路径就会**零发包直接返回空** —— 与配额耗尽完全同形的假漏网。
    # 放在 cache/ 下的两个要用同样的 0600 权限（`[状态文件权限]` 会扫）。
    for _rf2 in ("state/cache/dead_hosts.json", "state/cache/host_fail.json",
                 "state/cache/proxy_fail.json", "state/cache/firecrawl_fail.json"):
        _p2 = os.path.join(S, _rf2)
        try:
            _fd2 = os.open(_p2, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(_fd2, "w", encoding="utf-8") as _fh2:
                _fh2.write("{}")
        except Exception as _e2:
            print(f"⚠️  沙箱 {_rf2} 清零失败（备用源变异体可能假漏网）: {_e2}")
    # ⚠️ [调度方式] 检查项读的是 `~/Library/LaunchAgents/*.plist`
    # （可用 `MW_LAUNCHAGENTS` 覆盖）。沙箱里把生产的 4 个 plist 拷进来，
    # 这样 `sched-calendar-dead` 变异有对象可改，且不碰生产目录。
    _la = os.path.join(S, "LaunchAgents")
    os.makedirs(_la, exist_ok=True)
    _prod_la = os.path.expanduser("~/Library/LaunchAgents")
    for _lbl in ("com.mison.marketwatch", "com.mison.marketcollect",
                 "com.mison.marketwebui", "com.mison.marketselfcheck",
                 "com.mison.marketagent"):
        _src_pl = os.path.join(_prod_la, _lbl + ".plist")
        if os.path.exists(_src_pl):
            shutil.copy2(_src_pl, _la)
    # marketagent 的脚本是 agent/run.py（子目录）：拷进沙箱同构路径，否则
    # [定时任务] 基线报「脚本不存在」假红。同时拷 agent 的 marketlib 依赖
    # （run.py sys.path 上溯到 MW 根，沙箱根已有）。
    _ag_src = os.path.join(ROOT, "agent", "run.py")
    if os.path.exists(_ag_src):
        os.makedirs(os.path.join(S, "agent"), exist_ok=True)
        shutil.copy2(_ag_src, os.path.join(S, "agent", "run.py"))
    # agent 包本体（agent-* 变异体的目标；2026-10-17）：沙箱自检的
    # [agent 摘要链路] 探针 import 沙箱内拷贝 —— agent 代码此前完全在
    # 变异射程外（生产真跑的模块却无行为测试）。src/ 子树同构拷入，
    # 含空 __init__.py（缺它则 import 走 namespace 包，绑定行为可能漂移）。
    _ag_src2 = os.path.join(ROOT, "agent", "src")
    if os.path.isdir(_ag_src2):
        shutil.copytree(_ag_src2, os.path.join(S, "agent", "src"),
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    # ⚠️ [定时任务] 3c) 的对账读 plist 的 ProgramArguments[1]：绝对路径
    # 直接读该文件。若保持生产绝对路径，沙箱里注入的变异体（改 webui.py
    # 等）永远不被读到 → harden-log-under/overlist 单跑也漏网（run27 实测）。
    # 把绝对路径重写到沙箱（脚本名恰好都拷在沙箱根），与旧 basename
    # 拼接行为等价；相对路径的条目本来就落 selfcheck 同目录，无需动。
    for _fn3 in os.listdir(_la):
        if not _fn3.endswith(".plist"):
            continue
        _plp = os.path.join(_la, _fn3)
        import plistlib as _pl_rw
        _d3 = _pl_rw.loads(open(_plp, "rb").read())
        _pa3 = _d3.get("ProgramArguments")
        if (isinstance(_pa3, list) and len(_pa3) > 1
                and isinstance(_pa3[1], str) and os.path.isabs(_pa3[1])):
            # ⚠️ 保留相对 MW 根的子目录结构（agent/run.py → S/agent/run.py），
            # 不能 basename——basename 会把子目录脚本重写到沙箱根（不存在）。
            _rel = os.path.relpath(_pa3[1], ROOT)
            if not _rel.startswith(".."):
                _pa3[1] = os.path.join(S, _rel)
            else:
                _pa3[1] = os.path.join(S, os.path.basename(_pa3[1]))
            _d3["ProgramArguments"] = _pa3
        # ⚠️ StandardOutPath/StandardErrorPath 也要重写到沙箱（2026-11-01 加）。
        # selfcheck 的 [定时任务] 3c 现在核对**目录**（不只是 basename）——
        # 它要求 plist 的日志路径落在 LOGS_DIR 内，而沙箱的 LOGS_DIR 是
        # `S/logs`。只改 ProgramArguments 的话，plist 仍指向**生产**
        # `~/.market-watch/logs`，于是沙箱基线报 10 条
        # 「指向 ~/.market-watch/logs（应为 …/mw_mut_xxx/logs）」
        # → 基线不干净 → 整轮回归中止（R42 首次实测）。
        # 重写后沙箱自检看到的是自洽的（plist 与 harden/rotate 都在沙箱内）。
        for _k3 in ("StandardOutPath", "StandardErrorPath"):
            _v3 = _d3.get(_k3)
            if isinstance(_v3, str) and os.path.isabs(_v3):
                _d3[_k3] = os.path.join(S, "logs", os.path.basename(_v3))
        open(_plp, "wb").write(_pl_rw.dumps(_d3))
    if fresh_pending:
        # ⚠️ `[采集新鲜度]` 的"盘中停摆判定"只在 pending.json 的 date ==
        # **今天**时才执行（否则走"休市/窗口未开 → 未验证"分支）。
        # 而变异测试常在休市日跑 —— `[实测]` 2026-10-05（国庆休市）：
        # `fresh-limit-huge` 变异**漏网**，只因判定分支根本没执行
        # （pending.json 停在 09-30）。这不是盲区，是**测试环境没造出触发条件**。
        # 伪造一份"今天且 40 分钟前"的 pending.json 强制走进该分支。
        import datetime as _dt
        _now = _dt.datetime.now()
        _stale = _now - _dt.timedelta(minutes=40)
        _pj = {"date": _now.date().isoformat(),
               "time": _stale.strftime("%Y-%m-%d %H:%M"),
               "hist": [], "news": [], "idx": {}, "sec": {}, "ev": [],
               "events": [], "storage": {}}
        import json as _js
        with open(os.path.join(S, "state", "pending.json"), "w") as _fh:
            _js.dump(_pj, _fh)
    # ---------- launchd 夹具（MW_LAUNCHCTL，见 run()） ----------
    #
    # ⚠️ launchd 状态在沙箱里**无法用 MW_DIR 隔离** —— 不可注入时三处
    # `launchctl list/print` 读的是**宿主机真实状态**，于是：
    #   · 宿主机重启清零计数 → `marketselfcheck` 的 `last exit code =
    #     (never exited)` → rc 判定分支整段不可达 →
    #     `sched-selfcheck-rc1-not-alarm` **永久漏网**（2026-10-10 实测）；
    #   · `rc=2`（自检自身坏了）那一支在真机上**恒不可达**，从未被行为测过。
    # 夹具 = 一个假 `launchctl` 可执行 + 一份状态 JSON。默认状态复刻
    # 「重启后」的宿主：selfcheck rc=1（**一次性任务的结论**，设计内）、
    # webui rc=3 且无进程（**设计内自重启窗口**，`KeepAlive` 拉起前的瞬间），
    # marketwatch/collect rc=0 —— 这样 `[定时任务]` 的三支判定
    # （0 放行 / 设计内非零放行 / 真故障报错）在沙箱里**恒可达且恒定**，
    # 不再随宿主机重启、bootout/bootstrap 归零而漂移。
    import json as _js_lc
    _lc_state = os.path.join(S, "launchctl_state.json")
    with open(_lc_state, "w", encoding="utf-8") as _fh:
        _js_lc.dump([
            {"label": "com.mison.marketwatch",    "pid": "-", "rc": 0, "runs": 40},
            {"label": "com.mison.marketcollect",  "pid": "-", "rc": 0, "runs": 40},
            {"label": "com.mison.marketwebui",    "pid": "-", "rc": 3, "runs": 1},
            {"label": "com.mison.marketselfcheck","pid": "-", "rc": 1, "runs": 1},
            # ⚠️ 2026-10-16 补：marketagent 此前**不在夹具里** → `[定时任务]`
            # 的 rc 判定对它整段不可达（`_labels` 有它，但 `print` 走夹具的
            # 113「Could not find service」→ 正则找不到 last exit code → continue）。
            # rc=0 = 生产常态（每小时唤醒，窗口外直接 return 0）。
            {"label": "com.mison.marketagent",    "pid": "-", "rc": 0, "runs": 5},
        ], _fh)
    # ⚠️ 0600：`[状态文件权限]` 会把组/其他可读的**状态类文件**报红
    # （夹具文件也在此列，0644 会被误报成真缺陷）。
    os.chmod(_lc_state, 0o600)
    # 外部源探活夹具（MW_HEALTH_FIXTURE，见 run()）：全真 = 降级链完整。
    # 键集合必须与 feeds.health() 的返回值一致（6 个源），否则自检读不到键。
    _hf = os.path.join(S, "health_fixture.json")
    with open(_hf, "w", encoding="utf-8") as _fh:
        # ⚠️ `eastmoney_backup` **不能出现** —— feeds.health() 只在东财
        # 主源失败时才写这个键；主源正常时它压根不在返回字典里。夹具里
        # 显式写 False 等于声明“备用主机探过且坏了”，会被 `_dead` 判成
        # 一个不可用源（实测：基线因此恒带 1 条 ❌，闸门又卡死）。
        _js_lc.dump({"eastmoney": True,
                     "tencent": True, "sina": True, "baidu": True,
                     "ths": True}, _fh)
    os.chmod(_hf, 0o600)
    _lc = os.path.join(S, "fake_launchctl")
    # ⚠️ `list` 的输出格式必须是 `<PID>\t<状态>\t<label>`（自检按**行**精确
    # 比对、取第一列判 _alive）；`print gui/<uid>/<label>` 必须含
    # `runs = N` 与 `last exit code = X` 两行（自检的正则按这个格式找）。
    # 未收录进状态 JSON 的 label → 照真 launchctl 一样以 113 退出、
    # stderr 写 "Could not find service"（`sched-phantom-job` 依赖这个）。
    with open(_lc, "w", encoding="utf-8") as _fh:
        _fh.write(
            "#!/usr/bin/env python3\n"
            "# 假 launchctl（回归夹具，由 selfcheck_mutation.make_sandbox 生成）。\n"
            "# 从本目录的 launchctl_state.json 读状态，按 launchctl 输出格式打印。\n"
            "import json, os, sys\n"
            "_d = os.path.dirname(os.path.abspath(__file__))\n"
            "_st = {j['label']: j for j in json.load(open(\n"
            "    os.path.join(_d, 'launchctl_state.json'), encoding='utf-8'))}\n"
            "if len(sys.argv) >= 2 and sys.argv[1] == 'list':\n"
            "    _la = os.path.join(_d, 'LaunchAgents')\n"
            "    # 已加载 = plist 目录里存在的（与 [调度方式] 同一真值源）\n"
            "    _have = sorted(f[:-6] for f in os.listdir(_la)\n"
            "                   if f.endswith('.plist')) if os.path.isdir(_la) else []\n"
            "    for _lb in _have:\n"
            "        _j = _st.get(_lb) or {'pid': '-', 'rc': 0, 'runs': 0}\n"
            "        print(f\"{_j.get('pid','-')}\\t0\\t{_lb}\")\n"
            "    sys.exit(0)\n"
            "if len(sys.argv) >= 3 and sys.argv[1] == 'print':\n"
            "    _lb = sys.argv[2].rsplit('/', 1)[-1]\n"
            "    if _lb not in _st:\n"
            "        print(f'Could not find service \"{_lb}\"', file=sys.stderr)\n"
            "        sys.exit(113)\n"
            "    _j = _st[_lb]\n"
            "    print(f'\\t\"Label\" => \"{_lb}\"')\n"
            "    print(f\"\\truns = {_j.get('runs', 0)}\")\n"
            "    _rc = _j.get('rc')\n"
            "    if _rc is None:\n"
            "        print('\\tlast exit code = (never exited)')\n"
            "    else:\n"
            "        print(f'\\tlast exit code = {_rc}')\n"
            "    sys.exit(0)\n"
            "sys.exit(64)\n")
    os.chmod(_lc, 0o755)
    return S

def _run_tree(cmd, env, timeout, cwd=None):
    """跑子进程，超时**连孙进程一起**杀 —— 2026-11-06 全量复核 P1-9。

    ⚠️ `subprocess.run(timeout=)` 只对**直接子进程**发信号。这里跑的是
    `python selfcheck.py`，而 selfcheck 自己还会起 webui / 行为探针 /
    curl —— 孙进程与框架不在同一个进程组里逃过收割，超时后继续占 CPU、
    继续往沙箱写盘。并行回归下这些孤儿会和下一轮抢资源，表现为
    「越跑越慢」与随机假失败（`[实测]` 8 路并行把 load 打到 229~302）。

    修法：`start_new_session=True` 让子进程成为新会话/进程组的组长，
    超时后对整个组发信号（killpg）。先 TERM 给一次体面退出的机会，
    等不到再 KILL。

    返回值/异常与 `subprocess.run` 保持一致 —— 调用方已经在靠
    `TimeoutExpired` 区分「超时」与「失败」，不能换异常类型。
    """
    _p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=cwd,
                          start_new_session=True)
    try:
        _out, _err = _p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for _sig, _wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 10)):
            try:
                os.killpg(_p.pid, _sig)
            except Exception:
                # 进程组已不存在（子进程自行退出）—— 退回直接杀
                try:
                    _p.kill()
                except Exception:
                    pass
            try:
                _out, _err = _p.communicate(timeout=_wait)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            _out, _err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout,
                                        output=_out, stderr=_err)
    return subprocess.CompletedProcess(cmd, _p.returncode, _out, _err)


def run(S, skip_ai=True):
    # ⚠️ MW_MUT_SANDBOX=1：沙箱语境标记（2026-10-14 第二次 webhook 泄漏后加）。
    # ai-watchdog/ai-smoke 变异体不设 MW_SKIP_AI_PROBE，而 selfcheck __main__
    # 层的 webhook 守卫第二支（MW_DIR != DIR）在沙箱进程内是循环论证
    # （DIR 本来就等于 MW_DIR）—— 需要一个与检查逻辑无关的硬标记。
    env = dict(os.environ, MW_DIR=S, MW_MUT_SANDBOX="1",
               MW_LAUNCHAGENTS=os.path.join(S, "LaunchAgents"),
               # launchd 夹具：`[定时任务]` 的三支 rc 判定（0/设计内非零/真故障）
               # 从此在沙箱恒可达，不再随宿主机重启/bootstrap 归零而漂移。
               MW_LAUNCHCTL=os.path.join(S, "fake_launchctl"),
               # 外部源探活夹具：`[数据源]` 的 ❌ 由 health() 固定供给全真，
               # 使基线 rc=0/0❌ —— 否则 baidu 间歇风控会让基线闸门恒命中，
               # 整轮回归跑不起来（测的是检查器逻辑，与外网此刻通不通无关）。
               # 仍被其它 ❌ 击中时，检测器要靠**逻辑**增量抓住注入缺陷。
               MW_HEALTH_FIXTURE=os.path.join(S, "health_fixture.json"),
               # 推送通道可达性夹具（2026-11-05）：`[推送通道]` 真实 curl
               # qyapi.weixin.qq.com —— 外网抖动（R46 首轮 HTTP 000）曾把
               # 空操作对照判脏、拒绝回写基线。固定 403（根路径正常响应）
               # 后判据逻辑不变、环境噪声消失。生产不设该变量。
               MW_PUSH_REACH_FIXTURE="403")
    # ⚠️ 默认跳过 AI 冒烟（省一次真 AI 调用）。但 `ai-watchdog-timedout-ok-output`
    # 的目标段 `[AI 通道]` 的冒烟分支**只在未跳过时才执行** —— 该变异体必须
    # 让冒烟真的跑（它的桩会拦截那次调用，不会真调 AI）。
    # 面板应答夹具：`[WebUI 应答]` 改探**本进程内的假监听**，不再碰生产
    # 面板（理由见 `_probe_fixture_start` 的长注释）。映射**显式**给出：
    #   8787（沙箱默认端口）→ 正常应答监听
    #   8799（`webui-port-dead` 的目标）→ 必拒连监听
    # `webui-hang` 把 PORT 改成假死监听自己的端口，不在映射里 ——
    # selfcheck 侧取不到映射就**原样探**，正好命中那个假死监听（不必再列一条）。
    # ⚠️ 夹具必须在 apply_mutant 的 webui-hang 分支**之前**起好 —— 那条
    # 分支要读 `_PROBE_HANG_PORT` 来改写沙箱 webui.py 的 PORT。
    _pn, _ph, _pd = _probe_fixture_start()
    env["MW_PROBE_FIXTURE"] = f"8787={_pn},8799={_pd}"
    # ⚠️ 时刻夹具：`[投递结果]` 的槽位判据是**时间相关**的，而沙箱的
    # `history/pushes.jsonl` 是**生产快照**（make_sandbox 拷的）。两者错位
    # 就会凭空报 ❌ → 基线脏 → 闸门当场中止整轮回归。
    # `[实测]` 场景：回归跑在 14:10，而生产当天 09:25 因 analyze 故障没推成功
    # → 沙箱判「槽位漏投」→ 基线带 ❌。这与上方已处理的 `starved_slots`
    # 是**同一类**问题（生产当日退化把回归夹死），解法同源：**夹具化环境**，
    # 让判据在恒定输入下可复现。
    # 取 04:00（240 分钟）——早于任何槽位的 `_SLOT_DUE_MIN`（最早 09:48），
    # 于是沙箱里 `_slot_missed` 恒返回空、「异常轮次」恒看当日 error 数。
    # 判别力不受影响：判据逻辑由 `[投递结果判据]` 的构造值用例覆盖，
    # 生产接线由 AST 断言覆盖 —— 与 `_starved_verdict` 的做法完全一致。
    env["MW_NOW_HM"] = "240"
    # ⚠️ 滚动窗口关闭（2026-10-30 加，配套 `_slot_missed_win` 的滚动 24h 改造
    # [selfcheck.py] 那条）：窗口改滚动后，沙箱 `history/pushes.jsonl` 里
    # **昨天的**盘后漏投也会被判 —— 生产快照带进基线 → 基线脏 → 回归当场
    # 中止。与 `MW_NOW_HM=240` 同一解法：夹具化环境，窗口设为 0（只判"此刻
    # 恰好到点的槽位"）。沙箱里此时 04:00、无任何槽位到点 → 恒空。
    env["MW_DL_WIN_H"] = "0"
    if skip_ai:
        env["MW_SKIP_AI_PROBE"] = "1"
    else:
        # ⚠️ 必须**显式清掉**继承值（2026-10-28 修，P2 假漏网）：
        # `env = dict(os.environ, ...)` 会把**调用者**环境里的
        # `MW_SKIP_AI_PROBE` 一起带进来，而上面只做了「需要时置 1」、
        # 没有「不需要时置空」—— 于是任何在 `MW_SKIP_AI_PROBE=1` 下
        # 启动本 runner 的用法（例如手工 `MW_SKIP_AI_PROBE=1 MW_FULL=1
        # python3 selfcheck_mutation.py`），都会让 `ai-watchdog*` /
        # `ai-smoke*` 这批「必须让冒烟真跑」的变异体**静默失效**：
        # 沙箱里那条分支根本不执行，注入的缺陷无从触发 → 报「漏网」。
        # `[实测]` 2026-10-28 R28 首跑：`ai-watchdog-timedout-ok-output`
        # 与 `ai-smoke-empty-stderr-blinded` 双双假漏网，而同一份源码在
        # 不带该变量时 R27 是抓住的。
        # 教训与 [market-watch-must-contain-guess-wrong] 同族：
        # **假漏网和假抓住一样危险** —— 它会把「环境变量」误诊成
        # 「检查项失效」，然后去改一个本来正确的判据。
        env.pop("MW_SKIP_AI_PROBE", None)
    # ⚠️ 超时 600s → 1200s（2026-10-20）：沙箱自检的空跑基线已涨到 ~340s
    # （首字节闸门/备用链两条行为探针各要真起子进程），load≈20 时翻倍就顶到
    # 600s —— 基线连续两次超时直接抛穿，整轮回归以 Traceback 结束。
    # 单轮 hang 的上界仍由 launchd 外的 2400/3000s 外层 timeout 兜住。
    r = _run_tree([sys.executable,
                   os.path.join(S, "src", "selfcheck.py")],
                  env=env, timeout=1200)
    return r.returncode, r.stdout

# ---------- agent 摘要链路探针（2026-10-17，用户「要」） ----------
#
# 为什么由**变异框架**跑、而不是往 selfcheck.py 加一个检查段：
#   agent 包（agent/src/mw_agent/__main__.py）不在 selfcheck 的既有检查面里。
#   往生产自检加段要动 selfcheck.py 本体（改变每轮生产自检的输出与耗时），
#   而这里的目标只是让这条链路进**回归射程**：
#     注入缺陷 → 沙箱跑自检 → 追加 `[agent 摘要链路]` 探针输出 → 判据照旧
#   （_fail_lines 行粒度 diff + must_contain 双闸，见 main()）。
# 基线（_base）同样追加探针输出 —— 探针环境自身坏掉时两边同时出现 ❌、
#   相减抵消，不会把每条变异体都误判成"抓住"（判据被环境噪声污染的教训）。
_AGENT_PROBE_CODE = r'''
import datetime, importlib.util, json, os, shutil, sys, tempfile, types

S = sys.argv[1]
failed = []


def ck(cond, msg):
    if not cond:
        failed.append(msg)


def _run_probe_async(fn, arg):
    """跑 agent 里的 async 工具函数（探针里 SDK 被桩掉，@tool 只是原函数）。"""
    import asyncio
    try:
        r = asyncio.run(fn(arg))
        return (r.get("content") or [{}])[0].get("text", "")
    except Exception as e:
        return f"(异常 {type(e).__name__}: {e})"


# ⚠️⚠️ 生产保真（2026-10-17 修，本探针曾因此漏掉一条线上真缺陷）：
# MW_DIR 必须指向**沙箱根 S**，不能另建 tmp。生产里 MW_DIR 就是主系统根，
# `__main__.py` 靠 `sys.path.append(MW_DIR)` 才能 import marketlib/analyze。
# 探针原先「MW_DIR=空 tmp + 自己 sys.path.insert(0, S)」= 替生产把路径补上了，
# 于是「主根不在 sys.path → 交易日门静默放行」这条缺陷永远探不到。
# [实测] 2026-10-17 生产周六 16:47 真跑一轮并推群（17:08），探针全绿。
# 现在：MW_DIR=S、不额外插 S、子进程 cwd=/（launchd 同款），
# 「模块自己把主根接进 sys.path」成为被测对象。
os.environ["MW_DIR"] = S
os.makedirs(os.path.join(S, "state", "history"), exist_ok=True)
# ⚠️ 沙箱 config.json 的 webhook 已在 make_sandbox 里换成**无效 key**
# （见那里的长注释：置空会让 [推送通道] 报红，必须保留结构合法）。
# 这里再堵一道：即便 make_sandbox 那条被改坏，也不许把生产 webhook
# 带进这个模块级 S —— 用户的铁律是"任何非生产语境都不推真实 webhook"。
# ⚠️⚠️ 生产根必须由**调用方传进来**（2026-10-25 修）：本段代码跑在
# `python -c` **子进程**里，而 `SRC` 只在框架进程的模块级定义 ——
# 子进程里从未绑定。原实现直接引用 `SRC`，`NameError` 被下面那句
# `except Exception: pass` 吞掉，于是这道"第二道防线"**从来没执行过**
# （2026-10-25 实测证死：探针代码里 `SRC` 零定义）。这种"看着在、实际
# 从不生效"的守卫比没有更危险 —— 主防线（make_sandbox 的置空）坏了也
# 不会有人发现。现在走 argv[2]，缺失时**显式打印**而不是静默跳过。
_PROD_ROOT = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    if not _PROD_ROOT:
        failed.append("第二道 webhook 防线未生效：生产根未传入（argv[2] 缺失）")
    else:
        import json as _js_hk2
        _cp2 = os.path.join(S, "conf", "config.json")
        _d2 = _js_hk2.load(open(_cp2, encoding="utf-8"))
        _real2 = _js_hk2.load(open(os.path.join(_PROD_ROOT, "conf", "config.json"),
                                   encoding="utf-8")).get("webhook") or ""
        if _d2.get("webhook") and _d2.get("webhook") == _real2:
            _d2["webhook"] = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
                              "?key=00000000-0000-0000-0000-000000000000")
            _fd2 = os.open(_cp2, os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(_fd2, "w", encoding="utf-8") as _f2:
                _js_hk2.dump(_d2, _f2, ensure_ascii=False)
except Exception as _e_hk2:
    # ⚠️ 不许静默：这道防线坏掉意味着"沙箱可能带真 webhook"，
    # 而铁律是"非生产语境绝不推真实 webhook"。宁可让断言红。
    failed.append(f"第二道 webhook 防线异常：{type(_e_hk2).__name__}: {_e_hk2}")
try:
    sdk = types.ModuleType("claude_agent_sdk")   # 沙箱 venv 无 SDK，桩掉

    def _tool(*_a, **_k):
        def _d(f):
            return f
        return _d

    sdk.tool = _tool
    sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
    sdk.create_sdk_mcp_server = lambda *a, **k: None

    async def _query(*_a, **_k):
        if False:
            yield None

    sdk.query = _query
    # ⚠️ `HookMatcher` 必须也桩（2026-11-01）：`__main__.py` 现在 import 它
    # 用于 Read 路径白名单钩子。不桩则 `from claude_agent_sdk import HookMatcher`
    # 抛 ImportError —— 而模块级 import 失败会让**整份 agent 探针**失效
    # （所有断言一起报"探针无有效输出"），判据静默退化成零覆盖。
    sdk.HookMatcher = type("HookMatcher", (), {
        "__init__": lambda self, **kw: self.__dict__.update(kw)})
    # ⚠️ `ToolAnnotations` 必须也桩（2026-11-02）：`__main__.py` 现在按名
    # import 它给九个 MCP 工具打注解。不桩 → 模块级 ImportError → 整份
    # agent 探针报"无有效输出"，判据静默退化成零覆盖（与 HookMatcher 同型）。
    sdk.ToolAnnotations = type("ToolAnnotations", (), {
        "__init__": lambda self, **kw: self.__dict__.update(kw)})
    for _n in ("AssistantMessage", "ResultMessage", "SystemMessage", "TextBlock"):
        setattr(sdk, _n, type(_n, (), {}))
    sys.modules["claude_agent_sdk"] = sdk
    # ⚠️ 只模拟 .pth（agent/src）；**不**插 S —— 主根由被测模块自己接。
    sys.path.insert(0, os.path.join(S, "agent", "src"))
    _spec = importlib.util.spec_from_file_location(
        "mw_agent_probe",
        os.path.join(S, "agent", "src", "mw_agent", "__main__.py"))
    M = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(M)

    # —— 任务前三门（窗口 / 交易日 / 当日去重） ——
    ck(M._gate_now(datetime.datetime(2026, 10, 16, 10, 0)) == "window",
       "窗口闸门失效：盘前 10:00 未被拦（非收盘窗口也会跑调研）")
    ck(M._gate_now(datetime.datetime(2026, 10, 17, 16, 30)) == "holiday",
       "休市闸门失效：周六未拦（周末照样调研推群）")
    ck(M._gate_now(datetime.datetime(2026, 10, 1, 16, 30)) == "holiday",
       "国庆休市日（2026-10-01 周四）未被拦 —— 交易日判定未走 marketlib.HOLIDAYS 真值")
    ck(M._gate_now(datetime.datetime(2026, 10, 16, 16, 30)) == "run",
       "任务前闸门误伤：交易日窗口内未放行（调研将永不执行）")
    _mk = os.path.join(M.STATE_DIR, "last_run.json")
    if os.path.exists(_mk):
        os.remove(_mk)
    os.makedirs(M.STATE_DIR, exist_ok=True)
    with open(_mk, "w", encoding="utf-8") as _f:
        _f.write('{"date": "2026-10-16"}')
    ck(M._gate_now(datetime.datetime(2026, 10, 16, 16, 30)) == "done",
       "当日去重闸门失效：已跑过仍放行（同日重复调研烧配额）")
    os.remove(_mk)

    # —— 摘要选块（纯推理末块不得当摘要） ——
    ck(M._pick_summary(["过程叙述：已写入 plan。", "<think>想一下</think>"])
       == "过程叙述：已写入 plan。",
       "纯推理末块被当成摘要（剥完为空 → 触发「AI 无文本产出」误回滚）")

    # —— 推送前闸门链（纯函数 _prepare：剥除/禁词/分段/质量） ——
    _sent = ("恒生科技指数今日收报 4105 点，涨 1.09%，站稳 4070 支撑后向 4300 "
             "压力位靠近，成交温和放量，盘面结构较昨日改善。")
    _long = _sent * 8
    _t, _why = M._prepare(_long)
    ck(_why == "" and _long[:40] in _t,
       "干净正文被误抑制（推送链误伤）why=" + repr(_why))
    _t, _why = M._prepare("<think>All steps. </think>\n\n" + _long)
    ck(_why == "" and "think" not in _t.lower() and _long[:40] in _t,
       "推理块未被剥离（think 泄漏进推送正文）")
    _t, _why = M._prepare(_long + "\n---\n**写作说明**：约 90 字，会话环境已收敛。")
    ck(_why == "" and "写作说明" not in _t and "---" not in _t,
       "元说明块未被剥离（写作说明进群）")
    _t, _why = M._prepare("我的持仓今日减仓，成本价再降，账户浮亏扩大。")
    ck(_why == "banned" and _t == "",
       "禁词闸门失效：个人资产词未被拦下")
    _short = ("今日盘面震荡整理，主要指数涨跌互现，成交量较前一交易日略收敛，"
              "市场情绪偏谨慎，观望氛围较浓，短期缺乏增量资金入场。")
    _t, _why = M._prepare(_short)
    ck(_why.startswith("quality") and "过短" in _why,
       "质量闸门失效：过短（<80 字）摘要未被抑制")
    _nod = ("今日各大指数普遍高开后震荡回落，成交量较昨日明显收缩，板块之间分化明显，"
            "观望氛围较浓，短期缺乏增量资金入场，市场情绪趋于谨慎，等待方向确认，"
            "整体维持窄幅整理格局。")
    _t, _why = M._prepare(_nod)
    ck(_why.startswith("quality") and "数字" in _why,
       "质量闸门失效：无数字（零事实）摘要未被抑制")
    # —— 摘要质量**告警**（2026-11-04：用户反馈调研报告不专业后新增）——
    # 与上面两条**硬闸门**不同：告警不抑制推送（suppression 会让人当天
    # 没播报），但必须真的接到 `_prepare` 并写 stderr —— 否则 prompt 写了
    # 没人遵守也无从发现（prompt 约束必须有代码侧观测，项目既有教训）。
    _bad_warn = ("①盘面：上证 3686.17（+2.16%），科创50 1265.47（+9.92%），"
                 "创业板指 2391.65（-2.73%），成交 2.05 万亿。"
                 "②今日上涨原因是情绪修复。③今日无昨日判断可验证。"
                 "④明日关注市场情绪与成交量变化，等待方向确认。")
    ck(len(M._summary_warn(_bad_warn)) >= 2,
       "摘要质量告警失效：退化摘要（空话归因/无昨日验证/无时间锚）未产生告警")
    import io as _io, contextlib as _cl
    _ebuf = _io.StringIO()
    with _cl.redirect_stderr(_ebuf):
        M._prepare(_bad_warn)
    ck("摘要质量" in _ebuf.getvalue(),
       "摘要质量告警未接到 _prepare（stderr 无输出 —— 告警是死代码）")
    # —— 2026-11-05 加固：证据正则裸「据」恒真 bug 的回归 ——
    # 原判据含裸「据」→ 命中「数据」的「据」→ 空话告警**从未触发过**。
    _nodata = ("①盘面：上证 3686.17。②资金回补、情绪修复，数据转好。"
               "③无。明日看盘。")
    ck(any("空话" in _x for _x in M._summary_warn(_nodata)),
       "摘要质量告警被裸「据」字恒真欺骗：含「数据」但无来源的空话未被告警"
       "（2026-11-05 修 —— 该 bug 让「空话」判据从上线起从未生效）")
    # —— 生产语境白名单（2026-11-05 加，P1 安全）：非 launchd 语境不得外发 ——
    _env_save = os.environ.get("XPC_SERVICE_NAME")
    try:
        os.environ.pop("XPC_SERVICE_NAME", None)
        _pp, _pwhy = M._prod_push_context()
        ck(_pp is False, f"非 launchd 语境被放行外发（why={_pwhy!r}）")
        os.environ["XPC_SERVICE_NAME"] = "com.mison.marketagent"
        _pp2, _ = M._prod_push_context()
        ck(_pp2 is True, "launchd marketagent 语境被误拒（推送会永久静默）")
    finally:
        if _env_save is None:
            os.environ.pop("XPC_SERVICE_NAME", None)
        else:
            os.environ["XPC_SERVICE_NAME"] = _env_save
    # —— WebFetch 域名白名单（2026-11-05 加，P1 安全）——
    ck(M._webfetch_host_ok("https://finance.sina.com.cn/x") is True,
       "白名单财经域名被误拒（WebFetch 会失效）")
    ck(M._webfetch_host_ok("https://evil.example.com/?x=") is False,
       "非白名单域名被放行（注入外带通道仍然敞开）")
    _t, _why = M._prepare(_long)
    ck(_t.count("\n　\n") >= 1,
       "分段兜底失效：长单段未分段或段间缺全角空格间隔行"
       "（企微把连续空行折叠成一个换行，一坨照发）")

    # —— 摘要排版（2026-10-26 用户反馈「调研 agent 推送排版差」后重写）——
    # 生产实证（16:24 那条）：正文以 `summary.` 开头（模型把元标签写进正文），
    # ① ② ③ 段间只有 `\n\n` —— 企微折叠连续空行，群里仍是一整坨。四条断言
    # 分别锁住：元标签剥除 / 段首加粗 / 段间间隔行 / 纯数字不误加粗。
    _fmtp = ("> **调研 Agent · 10-26 16:24**\n\n"
             "summary.调研完成：plan 段已更新，触发线维持不动。\n\n"
             "①A股高低切加剧：存储芯片 -3.97%、CPO -4.21%，"
             "上证 3697.52（+0.19%）。\n\n"
             "②plan 已更新至 10-26：写入高低切结构与 10/22 缺口 3949–3986 未回补。\n\n"
             "③触发线维持 3781/3995 不动：52 周低点依据未变，明日观察 3781 能否守住。")
    _t, _why = M._prepare(_fmtp)
    ck(_why == "", "排版样本被误抑制 why=" + repr(_why))
    ck("summary" not in _t.lower(),
       "元标签未被剥离（模型写在正文开头的 summary. 原样进群）")
    ck("**①A股高低切加剧**" in _t,
       "段首标签未加粗（①… 仍是等权重裸文本，移动端无扫读锚点）")
    ck(_t.count("\n　\n") >= 3,
       "段间缺全角空格间隔行（企微折叠连续空行，三段挤成一坨）")
    ck("**15**" not in M._fmt_summary("15:03 港股收盘恒科 3858。"),
       "纯数字开头被误加粗（15:03 被拆成 **15**：03）")

    # —— 行内枚举切段（2026-11-05 用户「优化调研播报排版」）——
    # 生产实证（11-05 16:21 那条）：模型把 ① 接在「…记忆已写入。」后面、
    # ③ 接在 ② 段尾、④ 接在 plan 段尾，全在同一行内。旧实现只按「换行 +
    # 标记」切，这三段一个都切不到（用户截图：四段挤成三坨）。
    _glued = ("调研完成：plan 段已更新（11-05），触发线维持；记忆已写入。"
              "① 盘面与关键位：沪指收 3689.91（+0.10%）、恒科 4152.37（+5.50%）。"
              "② 驱动与证据：道指 +3.03%，半导体主力 +61.0 亿；"
              "反证：费半 -3.13% 而 A 股逆势 +2.78%。"
              "③ 昨日验证与今日决策：恒科站上 3949 已验证（4152.37），plan 已重写。"
              "④ 明日可操作观察点：①恒科守 3995、挑战 4243.65。"
              "②上证 3700 整数关。失效条件：恒科收盘回 3995 下方。")
    _gf = M._fmt_summary(_glued)
    ck(_gf.count("\n　\n") == 4,
       "行内枚举未切段：①③④ 接在上一句句号后时整段切不开"
       "（2026-11-05 16:21 生产实证的四段挤成三坨）")
    ck("**①盘面与关键位**" in _gf and "**③昨日验证与今日决策**" in _gf,
       "行内枚举段的段首标签未加粗（① ③ 落在段中，移动端无扫读锚点）")
    _seg4 = [s for s in _gf.split("\n　\n") if "④明日可操作观察点" in s]
    ck(bool(_seg4) and "①恒科守 3995" in _seg4[0]
       and "②上证 3700" in _seg4[0],
       "④ 段内重起序号的嵌套列举被误切成独立段"
       "（只许切「递增」的标记，①≤已见最大序号一律不动）")
    _long2 = ("①盘面与关键位：沪指收 3689.91（+0.10%）。"
              "② 驱动与证据：" + "半导体主力 +61.0 亿、CPO 净流出 85.5 亿。" * 6)
    _seg2 = [s for s in M._fmt_summary(_long2).split("\n　\n")
             if "②驱动与证据" in s]
    ck(bool(_seg2) and _seg2[0].count("\n") >= 3,
       "长段未按句换行（>120 字的段仍是一整行，移动端一屏读不完）")

    # —— get_snapshot 的日期校验（2026-10-20 新增）——
    # 背景：Agent 曾把**东财网页缓存快照**（10-13 收盘价）当成今日收盘值，
    # 写出「PCB +6.08%、沪电 +5.73%」。修法是让 Agent 改读主系统 collect
    # 落的 pending.json；这个断言锁住「日期不合法/不是当天 → 拒答」，
    # 否则过期快照会照答被当成今日。
    _snap_bad = _run_probe_async(M.get_snapshot, {"date": "bad"})
    ck("非法 date" in _snap_bad,
       "get_snapshot 放弃日期校验：非法日期未拒答")

    # —— _fmt_snapshot 的港股「盘中值」标注（2026-10-20 新增）——
    # 背景：pending.json 采集止于 15:05、港股 16:00 才收盘 —— 港股行
    # （100./116./124. 开头）**永远**是盘中值，不标注的话 Agent 把 15:03
    # 的恒生科技 4057 当收盘值写进摘要（10-20 真实收盘 4045.91）。
    _snap_txt = M._fmt_snapshot(
        {"time": "15:03",
         "idx": {"1.000001": {"name": "上证", "px": 3735.37, "chg": -0.15},
                 "124.HSTECH": {"name": "恒生科技", "px": 4057.0, "chg": -1.25}},
         "sec": {}}, "pending.json")
    ck("港股盘中值" in _snap_txt and "4057" in _snap_txt,
       "快照港股行未标「港股盘中值」（Agent 把 15:03 的港股当收盘值）")

    # —— update_threshold 回写 plan.md 关键位（2026-10-24 新增）——
    # 背景：`update_threshold` 原先只改 sectors.json，plan.md 里的关键位副本
    # 靠人工同步。2026-10-23 实际漂移过一次（配置 3995/3781，plan 仍写
    # 4300/4070），自检 [plan.md 新鲜度] 报 ❌ 但没人自动修。
    # 断三条：① 真回写到正确的行；② 只动那一个数字（正文其余数字不变）；
    # ③ 幂等（同值再调不改文件）；④ 不在段内的键（gold_up）不误伤。
    _planp = M.PLAN
    _plan_orig = None
    try:
        _plan_orig = open(_planp, encoding="utf-8").read()
        # 取一个**与现有值不同**的目标值（−100，避开"恰好相同→视为幂等"）
        import re as _re_pl
        _mm = _re_pl.search(r"下方\s*\*\*(-?\d+(?:\.\d+)?)\*\*",
                            _plan_orig)
        if _mm is None:
            failed.append("plan.md 关键位段缺「下方 **N**」行（结构变了，"
                          "同步表 _PLAN_KEY_SYNC 需跟着改）")
        else:
            _oldv = float(_mm.group(1))
            _newv = _oldv - 100.0
            _r = M._plan_sync_threshold("hstech_down", _newv)
            _after = open(_planp, encoding="utf-8").read()
            ck(_r is None,
               f"plan 同步返回了失败说明: {_r}")
            ck(f"下方 **{_newv:g}**" in _after,
               "update_threshold 未把新阈值回写 plan.md 关键位"
               "（两边会漂移，AI 读到旧触发线）")
            ck(_after.count("**") == _plan_orig.count("**") and
               len(_after) - len(_plan_orig) < 20,
               "plan 同步改动了正文其它内容（应只替换那一个数字）")
            # 幂等：同值再调，文件必须**逐字节不变**
            M._plan_sync_threshold("hstech_down", _newv)
            ck(open(_planp, encoding="utf-8").read() == _after,
               "plan 同步非幂等（同值再调仍重写文件）")
            # 不在关键位段的键不得误伤任何数字
            _before_g = open(_planp, encoding="utf-8").read()
            ck(M._plan_sync_threshold("gold_up", 9.99) is None,
               "不在关键位段的键（gold_up）被当成了同步失败")
            ck(open(_planp, encoding="utf-8").read() == _before_g,
               "不在关键位段的键改动了 plan.md")
    finally:
        if _plan_orig is not None:
            try:
                with open(_planp, "w", encoding="utf-8") as _fp:
                    _fp.write(_plan_orig)
            except Exception:
                pass

    # —— plan.md 关键位同步**必须限定在关键位段内**（2026-10-25 新增）——
    # 背景：原实现用 `_pat.sub(..., count=1)` 对**全文**替换。`## 当前市场位置`
    # 段排在 `## 关键位` **之前**，而叙述里同样会写出「上方 **N**」形态
    # （生产 plan.md 第 44 行 `**上方 3995** = 10/22 向下缺口上沿` 就是同一
    # 阈值的人写副本）。agent 每轮重写叙述，一旦叙述行里出现该形态，替换就
    # 改错行**且返回 None（成功）**——配置与 plan 继续漂移，而工具声称同步了。
    # 断三条：① 叙述段里的同形态文本不被改；② 关键位段里的那行被改；
    # ③ 关键位段内同键出现多处时**明说失败**而不是赌第一个。
    _planp2 = M.PLAN
    _plan_orig2 = None
    try:
        _plan_orig2 = open(_planp2, encoding="utf-8").read()
        _narr = ("## 当前市场位置（2026-10-24）\n\n"
                 "- 叙述行：上方 **1234**，下方 **5678**（这段是人写的市场叙述）\n\n"
                 "## 关键位（用于判断触发距离）\n"
                 "- 恒生科技指数：上方 **3995**，下方 **3781**\n")
        with open(_planp2, "w", encoding="utf-8") as _fp2:
            _fp2.write(_narr)
        _r2 = M._plan_sync_threshold("hstech_up", 4200)
        _after2 = open(_planp2, encoding="utf-8").read()
        ck(_r2 is None, f"关键位同步在结构正常的文件上失败: {_r2}")
        ck("上方 **1234**" in _after2,
           "关键位同步改到了「当前市场位置」段里的叙述行"
           "（必须限定在 ## 关键位 段内）")
        ck("上方 **4200**" in _after2,
           "关键位同步没改到关键位段里的那一行")
        ck("下方 **5678**" in _after2 and "下方 **3781**" in _after2,
           "关键位同步误伤了别的阈值行")
        # 段内同键两处 → 必须明说失败（不赌"第一个是对的"）
        with open(_planp2, "w", encoding="utf-8") as _fp2:
            _fp2.write("## 关键位\n- 甲：上方 **3995**\n- 乙：上方 **3995**\n")
        _r3 = M._plan_sync_threshold("hstech_up", 4300)
        ck(isinstance(_r3, str) and "2 处" in _r3,
           "关键位段内同键两处时未明说失败（会静默改错行）")
        # ⚠️ 判别「是否真的限定在关键位段」的唯一干净用例：让**关键位段内
        # 没有**该键、而**叙述段里有**。限定段落的实现会诚实报「找不到」；
        # 全文实现会静默改掉叙述行并返回成功 —— 两者行为正交，不会被
        # 「段内多处→报错」那条守卫抢先命中（上面的用例就是被它抢先的，
        # 实测首条 ❌ 变成"匹配到 2 处"，判据文案对不上）。
        with open(_planp2, "w", encoding="utf-8") as _fp2:
            _fp2.write("## 当前市场位置\n- 叙述：上方 **1234**\n\n"
                       "## 关键位\n- 恒生科技：下方 **3781**\n")
        _r3b = M._plan_sync_threshold("hstech_up", 4300)
        _after3b = open(_planp2, encoding="utf-8").read()
        ck(isinstance(_r3b, str),
           "关键位段里没有该键却返回成功（改到了段外的叙述行）")
        ck("上方 **1234**" in _after3b,
           "关键位同步改到了「当前市场位置」段里的叙述行"
           "（范围没限定在 ## 关键位 段内）")
        # 找不到「## 关键位」段 → 必须明说
        with open(_planp2, "w", encoding="utf-8") as _fp2:
            _fp2.write("## 别的标题\n- 上方 **3995**\n")
        _r4 = M._plan_sync_threshold("hstech_up", 4300)
        ck(isinstance(_r4, str) and "关键位" in _r4,
           "缺「## 关键位」段时未明说失败（静默返回成功）")
    finally:
        if _plan_orig2 is not None:
            try:
                with open(_planp2, "w", encoding="utf-8") as _fp2:
                    _fp2.write(_plan_orig2)
            except Exception:
                pass

    # —— 摘要闸门必须走主系统 safe()（2026-10-25 新增）——
    # 背景：`_safe_summary` 原先只查本地 `_SUMMARY_BANNED` 词表，比主系统
    # `safe()` 弱一层 —— 漏掉五个结构正则（金额/状态/计划编号/窗口措辞/人称）、
    # 规范化拆字匹配、以及孤立代理码点。agent 是**第二个推送出口**，守卫
    # 不该比主系统弱、更不该是独立维护的第二份真值。
    _s_clean = ("恒生科技指数今日收报 3871.66 点，涨 2.05%，距上沿 3995 还有 "
                "3.2%，距下沿 3781 仅 2.3%。盘面结构较昨日改善，成交温和放量，"
                "半导体链主力净流入 68.69 亿。明日观察能否守住 3781。")
    ck(M._safe_summary(_s_clean) == _s_clean,
       "干净摘要被误伤（推送链少一条正常播报）")
    # 结构正则命中（本地词表里根本没有这些词）→ 那一句必须被剔掉
    _s_win = M._safe_summary(_s_clean + "本轮窗口最后 3 日需注意节奏。")
    ck("本轮窗口" not in _s_win and "3871.66" in _s_win,
       "窗口措辞未被拦（_safe_summary 没走主系统 safe()，"
       "WINDOW_RE 漏网）")
    _s_amt = M._safe_summary(_s_clean + "另需备好 5万 现金。")
    ck("5万" not in _s_amt and "3871.66" in _s_amt,
       "个人金额未被拦（AMT_RE 漏网 —— 本地词表没有金额判据）")
    # 拆字绕过：`**仓**位` —— 需 _mask 规范化才拦得住
    _s_mask = M._safe_summary(_s_clean + "**仓**位维持不动。")
    ck("位维持不动" not in _s_mask,
       "拆字绕过未被拦（`**仓**位` 过闸门 —— 未走 _mask 规范化）")
    # 整条都脏 → 必须整条拒（不能推出只剩半句的碎片）
    ck(M._safe_summary("我的持仓今日减仓。成本价再降。账户浮亏扩大。") == "",
       "全脏摘要未被整条拒绝")

    # —— DRY_RUN 必须挡住 agent 推送（2026-10-25 新增）——
    # 用户放 DRY_RUN 的语义是「现在别往群里发」，对**所有出口**都成立。
    # agent 是第二个出口，原先完全不查 —— 暂停开关只关了半扇门。
    _dryp = os.path.join(S, "state", "DRY_RUN")
    try:
        open(_dryp, "w").write("")
        ck(M._dry_flag_active() is True,
           "DRY_RUN 存在时 _dry_flag_active() 未生效（agent 会照推）")
        # ⚠️ 判据必须看**标签**而不是只看返回值（2026-10-29 修）：
        # `_push_summary` 改成解析企微 errcode 之后，沙箱的无效 key
        # （errcode 93000）也会让它返回 False —— 于是「DRY_RUN 有没有生效」
        # 与「推送有没有失败」在返回值上**同形**，`is False` 这条断言
        # 对 `agent-dry-flag-ignored` 变异体彻底失去判别力（R29 实测漏网）。
        # `tag_out` 是这轮加的旁路标签，`skip_dry` 只在 DRY_RUN 分支出现，
        # 与 `fail_errcode:93000` 正交。
        _dtag = []
        ck(M._push_summary("> **调研 Agent · 10-17 16:30**\n\n" + _s_clean,
                           tag_out=_dtag) is False
           and _dtag == ["skip_dry"],
           "DRY_RUN 生效时 _push_summary 仍报告推送成功")
    finally:
        try:
            os.remove(_dryp)
        except Exception:
            pass
    ck(M._dry_flag_active() is False,
       "DRY_RUN 删除后 _dry_flag_active() 仍为真（推送将永久静默）")

    # —— 真推送路径前段（webhook 置空 → 不触网、不落真盘） ——
    # ⚠️ 必须在本段**临时**把沙箱 config 的 webhook 置空再恢复（2026-10-20）：
    # 本断言测的是 `_push_summary` 的"没配 webhook 就不推"分支。而沙箱
    # config 现在带一个**结构合法、key 无效**的地址（见 make_sandbox ——
    # 那里不能置空，否则 `[推送通道]` 段报红、基线闸门中止）。
    # 二者目标不同：前者要"有 webhook 但不敢真发"，后者要"根本没有 webhook"。
    # 早先直接断言 `_pushed is False`，而沙箱里 webhook 非空 → 真的发出去了
    # （key 无效所以没进群），断言随之变红，基线卡死。
    _cfgp = os.path.join(S, "conf", "config.json")
    _cfgbak = None
    try:
        _cfgbak = open(_cfgp, encoding="utf-8").read()
        _dc = json.loads(_cfgbak)
        _dc["webhook"] = ""
        with open(_cfgp, "w", encoding="utf-8") as _fh:
            json.dump(_dc, _fh, ensure_ascii=False)
        _pushed = M._push_summary("> **调研 Agent · 10-17 16:30**\n\n" + _long)
        ck(_pushed is False, "webhook 置空仍报告推送成功（记账失真）")
    finally:
        # 无论断言成败都把原地址写回去 —— 后面还有别的校验读 config。
        if _cfgbak is not None:
            try:
                with open(_cfgp, "w", encoding="utf-8") as _fh:
                    _fh.write(_cfgbak)
            except Exception:
                pass

    # —— 生产语境门**接线**（2026-11-05 加，P1 安全）——
    # webhook 已还原（结构合法的假 key），但本子进程没有 launchd 的
    # XPC_SERVICE_NAME —— 摘要必须在**外发之前**被拦下并打 not_prod。
    # 去掉这道门，沙箱/手工/IDE 跑就会真往群里发（铁律：只有生产可推）。
    _ntag = []
    _npush = M._push_summary("> **调研 Agent · 10-17 16:30**\n\n"
                             + _s_clean, tag_out=_ntag)
    ck(_npush is False and _ntag == ["not_prod"],
       "非生产语境未被 _push_summary 拦下（沙箱/IDE/手工跑会真推群）")

    # —— Read/Grep/Glob 路径白名单（2026-11-02 加） ——
    # 背景：`_read_guard_paths` 原先放行**整个 conf/ 目录**，而 conf 下
    # 还有 config.json（真实 webhook key，89 字）与 private-notes.md
    # （个人持仓）。本钩子存在的理由就是挡"注入页面诱导 agent 读
    # config.json 再把内容拼进 URL 外带"，结果把目标文件本身放行了。
    # 判据方向必须**双向**：该读的（plan.md/sectors.json）要放行，
    # 含凭据的（config.json）与系统文件（/etc/passwd）要拒 —— 只测
    # "拒了 config.json" 会让"把白名单清空"也蒙混过关。
    def _guard_dec(tool, ti):
        import asyncio as _aio_g
        _r = _aio_g.run(M._guard_read_hook(
            {"tool_name": tool, "tool_input": ti}, None, None))
        return (_r.get("hookSpecificOutput") or {}).get("permissionDecision")

    ck(_guard_dec("Read", {"file_path": M.PLAN}) == "allow",
       "Read 白名单把 plan.md 也拒了（agent 读不到市场语境，调研将失效）")
    ck(_guard_dec("Read", {"file_path": M.SECTORS}) == "allow",
       "Read 白名单把 sectors.json 也拒了（触发线现值读不到）")
    ck(_guard_dec("Read", {"file_path": os.path.join(S, "conf", "config.json")})
       == "deny",
       "Read 白名单放行了 conf/config.json（真实 webhook key 可被读出外带）")
    ck(_guard_dec("Read", {"file_path": "/etc/passwd"}) == "deny",
       "Read 白名单放行了 /etc/passwd（越界读未被拦）")
    # —— WebFetch 域名守卫**接线**（2026-11-05 加，P1 安全）——
    # 上面的 `_webfetch_host_ok` 只测纯函数；守卫真正生效靠 `_guard_read_hook`
    # 的 WebFetch 分支接线。白名单域名放行、非白名单拒绝，双向都要测。
    ck(_guard_dec("WebFetch", {"url": "https://finance.sina.com.cn/x"})
       == "allow",
       "WebFetch 守卫把白名单财经域名也拒了（收盘查证会被误堵）")
    ck(_guard_dec("WebFetch", {"url": "https://evil.example.com/steal?d=1"})
       == "deny",
       "WebFetch 守卫没接线：非白名单域名被放行（注入外带通道仍敞开）")

    # —— MCP 工具 schema 规范化（2026-11-02 加） ——
    # ⚠️ 为什么断言**源码文本**而不是 tool 对象：本探针把 SDK 的 `tool`
    # 桩成"原样返回函数"（上面 `sdk.tool = _tool`），于是 `M.web_search`
    # 就是裸函数，没有 input_schema / annotations 可查。而现有探针又必须
    # 直接**调用**这些函数（get_snapshot / _push_summary 等），不能改成
    # 返回对象。折中：schema 的"形状"在源码层判 —— 这是能被变异体打坏的
    # 那一层（改 required / 摘 annotations / 退回短式 dict）。
    _ag_src = open(os.path.join(S, "agent", "src", "mw_agent",
                                "__main__.py"), encoding="utf-8").read()
    _n_tool = _ag_src.count("@tool(")
    ck(_n_tool == 9,
       f"@tool 声明数不是 9（实际 {_n_tool}）—— 工具有增删或被拼行")
    _n_add = _ag_src.count('"additionalProperties": False')
    ck(_n_add == _n_tool,
       f"只有 {_n_add}/{_n_tool} 个 @tool 禁止了未知参数"
       "（additionalProperties: False 缺失 → 模型传错字段被静默忽略）")
    # ⚠️ 必须**按工具块**判，不能全局 grep（2026-11-02 实测漏网）：
    # `web_search_alt` 的 required 也是 ["query"]，全局 `in _ag_src` 在
    # web_search 被改成 ["query","max_results"] 之后依然为真 ——
    # 变异体 agent-mcp-schema-optional-required 因此报「漏网」。
    import re as _re_s
    _m_ws = _re_s.search(r'@tool\(\s*"web_search",.*?async def web_search\(',
                         _ag_src, _re_s.S)
    _blk_ws = _m_ws.group(0) if _m_ws else ""
    _req_ws = _re_s.search(r'"required":\s*\[([^\]]*)\]', _blk_ws)
    ck(bool(_blk_ws) and bool(_req_ws)
       and _req_ws.group(1).strip() == '"query"',
       "web_search 的 required 不含 max_results 的形式 —— 可选参数被当必填"
       "（短式 dict schema 的默认行为，SDK 会把每个键都塞进 required）")
    ck('"enum": list(_TH_ALLOWED)' in _ag_src,
       "update_threshold 的 schema 层没有 enum —— 合法键只能在运行时试错")
    _n_ann = _ag_src.count("annotations=_ANN_")
    ck(_n_ann == _n_tool,
       f"只有 {_n_ann}/{_n_tool} 个 @tool 带 MCP 注解"
       "（客户端读不到只读/写入/触网语义）")
    ck('create_sdk_mcp_server("mw", version=' in _ag_src,
       "create_sdk_mcp_server 未声明 version（工具定义变更不可辨识）")
finally:
    # 清掉探针写的当日标记（沙箱是抛荒的，但别给后续复用留脏状态）。
    # M 可能未绑定（exec_module 在赋值前就抛）—— 用 locals 探测，别让
    # 清理代码自身抛 NameError 把 JSON 输出吞掉。
    _Mp = locals().get("M")
    if _Mp is not None:
        try:
            os.remove(os.path.join(_Mp.STATE_DIR, "last_run.json"))
        except Exception:
            pass

print(json.dumps({"failed": failed}, ensure_ascii=False))
'''


def _run_agent_probe(S):
    """子进程跑 agent 探针，返回失败消息列表（空 = 全绿）。

    ⚠️ 必须**子进程**：探针要 import 沙箱的 analyze/marketlib/agent 包，
    而框架进程要跑几十条变异体、每条一个不同的沙箱副本 —— 进程内 import
    会按第一次的副本缓存 sys.modules，后续变异体读到旧拷贝、判据静默失效。
    子进程还让 MW_DIR / SDK 桩 / 临时目录与框架完全隔离（不污染自检进程）。
    """
    import json as _js_p
    env = dict(os.environ)
    env.pop("MW_DIR", None)        # 探针脚本内自建临时 MW_DIR
    env.pop("PYTHONPATH", None)
    # ⚠️ cwd="/" 与 launchd 一致：`python -c` 的 sys.path[0] 是 cwd，
    # 若继承框架的 cwd（=生产根），marketlib 又能从 cwd 找到 —— 探针再次失真。
    # argv[2] = 生产根：供探针内的"第二道 webhook 防线"对照（子进程里没有
    # 模块级的 `SRC`，见探针内对应注释）。
    r = _run_tree([sys.executable, "-c", _AGENT_PROBE_CODE, S, ROOT],
                  env=env, timeout=180, cwd="/")
    # ⚠⚠ 2026-11-16（P0，Codex 审查实测）：**必须同时校验 rc 与 JSON schema。**
    # 原实现只 `return list(...get("failed") or [])` —— 于是：
    #   进程 rc=7（自己崩了）但 stdout 恰好是 `{"failed": []}`
    #   → 返回 []，被当成「探针跑完、没问题」，变异体于是**凭空多一条通过**。
    # 这类"检查器崩溃被读成绿灯"正是变异测试最不能有的东西：
    # 它不只漏掉一条，还把判据的可信度整体拉低。
    # 现在：rc 非 0 直接判失败（探针自身异常是一种**必须暴露**的状态）；
    # JSON 形状不对（缺 failed 键 / 不是 list）同样判失败。
    _payload = None
    for _ln in reversed((r.stdout or "").splitlines()):
        _ln = _ln.strip()
        if _ln.startswith("{"):
            try:
                _payload = _js_p.loads(_ln)
            except Exception:
                _payload = None
            break
    _err = ((r.stderr or "").strip().splitlines() or [""])[-1]
    if r.returncode != 0:
        # 探针非零退出：把 stdout 尾行一并带出，便于区分"真失败"与"崩溃"
        return [f"探针自身异常退出（rc={r.returncode}）—— 本次探活结论不可用"
                f"; stderr={_err[:120]}"]
    if not isinstance(_payload, dict):
        return [f"探针输出不是合法 JSON 对象（got {type(_payload).__name__}）"
                f"; rc={r.returncode}; stderr={_err[:120]}"]
    _failed = _payload.get("failed")
    if not isinstance(_failed, list):
        return [f"探针输出缺 `failed` 列表（got {type(_failed).__name__}）"
                f"; rc={r.returncode}"]
    return [str(_x) for _x in _failed]


def _with_agent_probe(S, out):
    """把 agent 探针结果以 `[agent 摘要链路]` 段追加进自检文本（判据复用）。

    每条变异体都跑（约 +2~4s）—— 任何动到 agent 依赖（marketlib 交易日表、
    analyze 剥除函数）的变异体也会在此段留痕，属预期内的交叉覆盖。
    """
    try:
        _fails = _run_agent_probe(S)
    except subprocess.TimeoutExpired:
        _fails = ["agent 探针超时（180s）"]
    except Exception as _e:
        _fails = [f"agent 探针无法执行: {type(_e).__name__}: {_e}"]
    _lines = ["\n[agent 摘要链路]"]
    if _fails:
        _lines += [f"  ❌ agent: {_m}" for _m in _fails]
    else:
        _lines.append("  ✅ 推送前四道闸门与任务前三门均可失败"
                      "（剥除/禁词/分段/质量；窗口/休市/去重）")
    return out + "\n".join(_lines) + "\n"


def bad_sections(out):
    """把自检输出里每个 ❌ 归到它所属的检查段（最近一个 '\\n[XXX]' 标题）。

    返回被 ❌ 命中的段名集合 —— 这才是"哪个检查项真的报出了这个缺陷"。
    只靠 rc≠0 的"抓住"可能来自[检查项可失败性]这类元守卫或顶层兜底，
    不能算作目标段的判别力。

    ⚠️ 段标题在输出里位于**第 0 列**（`print("\\n[XXX]")` 的 `\\n` 让它另起
    一行、无缩进）。**不能 strip 后再判** —— 元守卫 `[检查项可失败性]`
    的失败输出里有 `       [跨进程冷却]` 这样的**缩进列表行**，strip 后
    形如段标题，会被误当成新段的开始、把后续 ❌ 归错段。用原始行判。"""
    cur, hit = None, set()
    for line in out.splitlines():
        s = line.strip()
        # 结论行不是缺陷行（可能引用「❌」作 grep 指引），见 `_fail_lines`。
        if s.startswith("总体:"):
            continue
        if (line.startswith("[") and line.rstrip().endswith("]")
                and "  " not in s and len(s) < 60):
            cur = s
        elif "❌" in line and cur:
            hit.add(cur)
    return hit

def _fail_lines(out, marker):
    """(段名, 归一化后的行文本) 的集合 —— 判"新增失败"要用**行**粒度。

    ⚠️ 为什么不能只用 `bad_sections` 的**段**粒度（2026-10-10）：
    基线里 `[数据源]` 因为 baidu 探活间歇失败**已经**是 ❌ 段。此时一个
    变异体若在**同一段**里新增一条 ❌（例如把降级链又砍一档），
    段集合相减后是空集 → 判"漏网"—— 明明抓住了却报盲区（**假漏网**）。
    行粒度能看见"这一条失败是新冒出来的"。

    归一化：去掉行首缩进与多余空白（各段的缩进层次不同，但同一段内稳定），
    避免因排版差异把同一条失败当成两条。

    ⚠️ **墙钟时长数字必须归一化**（2026-10-31 实测 P0）：`[执行流水新鲜度]`
    的「runs.jsonl 与 watch.log 都已静默 382 分钟」是拿 `now - 末条时间戳`
    实时算的 —— 基线沙箱与变异体沙箱**创建时刻不同**，这个数每过一分钟
    就变一次。行文本一字不差、只有数字漂移，差集照样把它当成「新增未验证
    行」→ **空操作对照被误判**（rc=0、❌数=0，却报「判据被污染」），
    整轮回归的「漏网 0」结论作废。同类形态还有 `[DRY_RUN 标志]` 的
    「N 分钟 / TTL M 分钟」、`[采集新鲜度]` 的「N 分钟前」。
    判据关心的是**行有没有新冒出来**，不是时长是几分钟 —— 把时长数字
    抹成占位符不会削弱任何判定（真要改坏新鲜度逻辑，行文本/段名/标记
    会变，照样抓得住）。只归一化时长单位，不动「共 26 处」这类稳定计数。"""
    cur, hit = None, set()
    # ⚠️⚠️ 2026-11-16（P0 修正，`fallback-maxpush-drift` 实测暴露）：
    # **失败项下面的缩进续行也是这条失败的一部分，必须归到同一 key 上。**
    # 自检里有大量"标题 + 明细"的打印形态，例如 `[常量一致性]`：
    #       ❌ 共享常量 1 处有问题（兜底副本仅在 … 期间生效）：
    #           兜底副本过期 _FALLBACK_MAX_PUSH: analyze=10 vs 兜底=6
    #   `兜底副本过期 _FALLBACK_MAX_PUSH` 这个**真正有判别力的片段在续行**里，
    # 而续行不带 ❌ 标记 —— 原实现于是只收集到那句泛泛的标题，
    # 以它为 must_contain 的变异体便判「漏网」（假漏网，实测 2026-11-16）。
    # 修法：记录"上一次命中的失败行索引"，紧跟其后的**更深缩进、且自身
    # 不含段标题**的行并入该失败项的文本。这样 must_contain 既能匹配标题
    # 也能匹配明细，且仍然只在**本轮新增**的失败里找（假绿防护不放松）。
    _last_i = -1
    _rows = out.splitlines()
    for _i, line in enumerate(_rows):
        s = line.strip()
        # ⚠️ 结论行（`总体: …`）永远不是缺陷行 —— 它可能引用「❌」字样
        # 作 grep 指引（2026-11-06 起「grep「未验证」或「❌」」），
        # 若被当作失败行计入，基线永远"脏"、变异体判据全被污染。
        if s.startswith("总体:"):
            continue
        if (line.startswith("[") and line.rstrip().endswith("]")
                and "  " not in s and len(s) < 60):
            cur = s
            _last_i = -1
            continue
        if marker in line and cur:
            _t = " ".join(s.split())
            _t = _DUR_RE.sub("<时长>", _t)
            hit.add((cur, _t))
            _last_i = _i
            continue
        # 续行：紧跟在失败行之后、自身带缩进、不是段标题、不是新的失败行
        if (_last_i == _i - 1 and cur and line[:1].isspace()
                and s and marker not in line and not s.startswith("总体:")):
            _t = " ".join(s.split())
            _t = _DUR_RE.sub("<时长>", _t)
            hit.add((cur, _t))
            # 允许连续多行续行：把索引前移，下一行仍可并入同一失败项
            _last_i = _i
    return hit


# 墙钟时长（分钟/小时/秒）—— 见 `_fail_lines` 的归一化说明。
_DUR_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:分钟|小时|秒钟|秒)")

# 沙箱**基线**本来就有的未验证段（休市日新鲜度、AI 冒烟被 MW_SKIP_AI_PROBE
# 跳过…）。判「新增加了的未验证」时必须减掉它，否则全员假绿。
# 首次使用时懒加载（跑一次无变异的沙箱自检）。
#
# ⚠️⚠️ **基线不只有「未验证」段，还有「❌ 段」** —— 判据必须两者都减。
#
# 原判据是 `(rc != 0) or ("❌" in out) or bool(new_unv)`，其中
# **`rc != 0` 与 `"❌" in out` 都是绝对值**，只要沙箱基线自己就带 ❌
# （真实存在：`[数据源]` 的 baidu 探活间歇失败、`[定时任务]` 曾因自指
# 假红长期置位），那么**每一条变异体**都恒判"抓住" —— 包括
# **什么都没改的空操作**。`[实测]` 2026-10-10：拿一个把
# `ROTATE_MAX = 512*1024` 替换成**同一字符串**的 no-op 变异体去跑，
# 报告 `✅抓住`。判别力归零，而输出完全看不出异常
# （"用例与实现同源 / 判据与被测对象同源"的又一变体：
#  这次是**判据被环境噪声污染**）。
#
# 修法：把 ❌ 与未验证都改成**相对基线的增量**。基线用同一台机器、
# 同一时刻跑一次无变异沙箱取；两者相减后，"新增的失败"才是变异体的
# 贡献。`_base_bad()` 与 `_base_unv()` 共用一次基线运行。

_BASE = None
_BASE_OUT = None        # 基线**原始输出**（供 must_contain 恒真自检用）
_BASE_LOCK = None
def _base():
    """跑一次无变异沙箱，返回 (❌行集合, 未验证行集合, rc)。

    ⚠️ 只跑一次并缓存 —— 基线要在**与变异体同一环境**下取，否则
    baidu 这类间歇源会让基线与变异体的噪声不同步、相减后残留假信号。
    ⚠️ 用**行**粒度（`_fail_lines`）而非段粒度，理由见那里的说明。
    ⚠️ **加锁**（2026-10-28 并行化）：缓存判据是 `_BASE is None`，
    并行下多个 worker 会同时看到 None → 各跑一遍完整基线（每次 ~60s），
    既浪费又会让不同 worker 用到**不同的基线**（间歇源噪声不同步，
    正是本函数要防的那件事）。锁内双检后与串行时行为等价。
    """
    global _BASE, _BASE_OUT, _BASE_LOCK
    if _BASE is not None:
        return _BASE
    if _BASE_LOCK is None:
        import threading as _thr
        _BASE_LOCK = _thr.Lock()
    with _BASE_LOCK:
        if _BASE is not None:
            return _BASE
        S0 = make_sandbox()
        try:
            try:
                _rc0, _out0 = run(S0)
            except subprocess.TimeoutExpired:
                # ⚠️ 基线超时**不能只记一次就算了**（原实现直接抛穿，
                # 整轮回归以 Traceback 结束、一条变异体都没跑）。
                # `[实测]` 2026-10-16：宿主同时有 4 个审查子代理收尾 +
                # Docker/VM，连续两次 600s 基线超时；而同一时刻手动
                # 跑沙箱自检只用了 ~200s。超时是**宿主负载**的信号，
                # 与"检测器坏了"无关，重试一次即可（与变异体循环同一策略，
                # 见那里的注释）。
                print("  ⏭️  基线超时（宿主负载），歇 15s 后重试 1 次…")
                time.sleep(15)
                shutil.rmtree(S0, ignore_errors=True)
                S0 = make_sandbox()
                _rc0, _out0 = run(S0)
            _out0 = _with_agent_probe(S0, _out0)
            _BASE = (_fail_lines(_out0, "❌"),
                     _fail_lines(_out0, "未验证"), _rc0)
            _BASE_OUT = _out0
        finally:
            shutil.rmtree(S0, ignore_errors=True)
        return _BASE

def _base_unv():
    """[死代码 2026-10-15] 判据收口后判据全部走 `_base()` 解包，这两个
    包装函数零调用。保留会让读者误以为存在第二套基线取值入口。"""
    return _base()[1]

def _base_bad():
    return _base()[0]

del _base_unv, _base_bad   # [死代码] 当场删除，不留零引用符号

def _first_fail_detail(out):
    """从自检输出里取**第一条真正的 ❌ 明细文案**，而不是汇总行。

    ⚠️ 存在理由（2026-10-23）：直接 `next(l for l in lines if "❌" in l)`
    拿到的是 `[兜底模板与结构闸门]` 段里的汇总行 `❌ 1 处：`——
    它**不含任何判据文案**，照着它去修 must_contain 会一头雾水。
    真正的明细在**下一行**（缩进的续行）。这里向下找第一条非空且
    不再是汇总形态（`❌ N 处` / `❌ N 项`）的行。
    """
    _ls = out.splitlines()
    for _i, _l in enumerate(_ls):
        if "❌" not in _l:
            continue
        _t = _l.strip()
        if _t.startswith("❌") and ("处：" in _t or "项：" in _t):
            for _nxt in _ls[_i + 1:]:
                if _nxt.strip():
                    return _nxt.strip()
            continue
        return _t
    return ""


def _inj_hygiene_glob_wrong(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 特殊：把 selfcheck 的 `_pats = (...)` 整块换成永不匹配的 glob。
    # ⚠️ 用**正则**而不是固定锚点（2026-11-01 改）：`_pats` 的元组从
    # 引入至今已改过三次（补 `\d{14}` 时间戳格式、补 cache/、补
    # `src/*.bak-*` + `conf/*.bak-*`），每次都要手工同步锚点，漏一次
    # 就静默记「锚点未匹配，跳过」→ 判别力无声消失（本项目反复栽的
    # 那类坑）。正则只锚"赋值开头 + 括号内容"，元组内部怎么改都不会断。
    import re as _re_hg
    _hp = os.path.join(S, "src", "selfcheck.py")
    if not os.path.exists(_hp):
        return False
    _hs = open(_hp, encoding="utf-8").read()
    _hs2, _n = _re_hg.subn(
        r'_pats = \([^)]*\)', '_pats = ("never-matches-*",)', _hs, count=1)
    if _n != 1:
        return False
    open(_hp, "w", encoding="utf-8").write(_hs2)
    return True


def _inj_fallback_th_drift(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 特殊：把 marketlib.TH 的 hstech_up 改成与 sectors.json **不同**的值。
    # ⚠️ 必须现读真值再改（理由同 fallback-maxpush-drift）：写死 "3995"
    # 会在下次重校准后 0 匹配、静默跳过。
    # 只动 TH、不动配置 —— 测的是「配置缺键时的兜底是否跟得上真值」。
    import re as _re_th
    import json as _js_th
    _mp = os.path.join(S, "src", "marketlib.py")
    try:
        _cfg = _js_th.load(open(os.path.join(S, "conf", "sectors.json"),
                                encoding="utf-8"))
        _real = float(_cfg.get("thresholds", {}).get("hstech_up"))
    except Exception:
        return False
    if not os.path.exists(_mp):
        return False
    _ms = open(_mp, encoding="utf-8").read()
    _bad = _real - 137.0        # 必然不同且不撞 0
    _ms2, _n = _re_th.subn(r'^    "hstech_up": -?[0-9.]+,\s*$',
                           f'    "hstech_up": {_bad:g},',
                           _ms, count=1, flags=_re_th.M)
    if _n != 1:
        return False
    open(_mp, "w", encoding="utf-8").write(_ms2)
    return True


def _inj_fallback_literal_drift(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 特殊：把 analyze.py 里 threshold("hstech_down", N) 的字面默认值
    # 改成与配置不同的数 —— 测「调用点字面量是否跟住真值」这条判据。
    import re as _re_fl
    import json as _js_fl
    _ap = os.path.join(S, "src", "analyze.py")
    try:
        _cfg = _js_fl.load(open(os.path.join(S, "conf", "sectors.json"),
                                encoding="utf-8"))
        _real = float(_cfg.get("thresholds", {}).get("hstech_down"))
    except Exception:
        return False
    if not os.path.exists(_ap):
        return False
    _as = open(_ap, encoding="utf-8").read()
    _bad = _real - 137.0
    # ⚠️ 只改**数字**，保留原调用形式与引号 —— 第一版统一替换成
    # `T("hstech_down", N)`，而 analyze.py:1649 那处所在函数**没有**
    # 局部 `T`（那是 detect() 里的 lambda），注入后变成 NameError，
    # 报错归因跑到别的段、本变异体判「漏网」（2026-10-24 实测）。
    _as2, _n = _re_fl.subn(
        r'(threshold|T)(\(\s*["\']hstech_down["\']\s*,\s*)-?[0-9.]+(\s*\))',
        lambda _m: (_m.group(1) + _m.group(2)
                    + f"{_bad:g}" + _m.group(3)),
        _as, count=1)
    if _n != 1:
        return False
    open(_ap, "w", encoding="utf-8").write(_as2)
    return True


def _inj_fallback_maxpush_drift(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 特殊：把 webui 的 `_FALLBACK_MAX_PUSH` 改成一个**与 analyze 真值不同**的数。
    #
    # ⚠️ 为什么不用固定锚点（2026-10-22）：原实现锚在字面量
    # `_FALLBACK_MAX_PUSH = 6` 上，而这个数**会变** —— 把
    # analyze.MAX_PUSH 从 6 提到 10 的当天，锚点当场 0 匹配，
    # 而 0 匹配在框架里是**静默跳过**（`apply_mutant` 返回 False →
    # 记「锚点未匹配」），**不算漏网** —— 于是这条测试的判别力
    # 无声消失，回归仍报「全绿」。这正是本项目反复栽的
    # 「锚点未匹配却报全绿」那一类（见记忆
    # market-watch-mutant-fixture-own-trigger）。
    # 现在改成现读真值再改，无论 MAX_PUSH 调成几都不会断锚。
    import re as _re_mp
    _ap = os.path.join(S, "src", "analyze.py")
    _wp = os.path.join(S, "src", "webui.py")
    if not (os.path.exists(_ap) and os.path.exists(_wp)):
        return False
    _as = open(_ap, encoding="utf-8").read()
    _ws = open(_wp, encoding="utf-8").read()
    _m = _re_mp.search(r"^MAX_PUSH\s*=\s*(\d+)\s*$", _as, _re_mp.M)
    if not _m:
        return False
    _real = int(_m.group(1))
    # 改成一个**必然不同**的值（真值+3，且不会撞上 0 这种特殊值）
    _bad = _real + 3
    _ws2, _n = _re_mp.subn(r"^_FALLBACK_MAX_PUSH\s*=\s*\d+\s*$",
                           f"_FALLBACK_MAX_PUSH = {_bad}", _ws,
                           count=1, flags=_re_mp.M)
    if _n != 1:
        return False
    open(_wp, "w", encoding="utf-8").write(_ws2)
    return True


def _inj_webui_hang(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 真缺陷形态：webui **进程活着、端口在听，但永不响应**
    # （死锁 / 被宿主负载压到无响应 —— 实测 2026-10-07 load1≈174 时
    # 生产 webui 就是这个形态，8 个端点全部超时）。
    # 与 `webui-port-dead`（拒连）是**两种不同的故障**：那条是没人听，
    # 这条是听了不回。判据必须分开归因（超时 ≠ 拒连），否则排查者
    # 会拿着"数据文件可能损坏"的错误方向去查一个负载问题。
    #
    # fixture：把沙箱 webui.py 的 PORT 指到 `_probe_fixture_start()` 起的
    # **假死监听**（accept 后永不回复）。⚠️ 2026-10-11 改：原先这里自己
    # 再起一个临时监听，于是同一份"假死"有两个来源 —— 现在统一走那个
    # 幂等夹具，`_HANG_SRV` 的开关/关闭逻辑整个删掉（少一处泄漏面）。
    _pn, _ph, _pd = _probe_fixture_start()
    _wp = os.path.join(S, "src", "webui.py")
    _ws = open(_wp, encoding="utf-8").read()
    _old = 'PORT = int(os.environ.get("MW_PORT", "8787"))'
    if _old not in _ws:
        return False
    open(_wp, "w", encoding="utf-8").write(
        _ws.replace(_old,
                    f'PORT = int(os.environ.get("MW_PORT", "{_ph}"))',
                    1))
    return True


def _inj_workdoc_guard_window_drift(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：改沙箱 claude-plan.md（不改源码）。
    _wp2 = os.path.join(S, "conf", "claude-plan.md")
    if not os.path.exists(_wp2):
        return False
    _ws2 = open(_wp2, encoding="utf-8").read()
    if name == "workdoc-guard-window-drift":
        if "**14:55-15:05**" not in _ws2:
            return False
        _ws2 = _ws2.replace("**14:55-15:05**", "**14:55-15:00**", 1)
    else:
        if "操作建议词是允许的" not in _ws2:
            return False
        _ws2 = _ws2.replace("操作建议词是允许的", "（已删除）", 1)
    open(_wp2, "w", encoding="utf-8").write(_ws2)
    return True


def _inj_plan_md_stale_position(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：改沙箱 plan.md（不改源码）。
    #
    # ⚠️ **锚点必须从 plan.md 自己推出来，不能硬编码日期/阈值**
    # （2026-10-09 改）。原实现写死 `"（2026-09-30）"` 与 `"下方 **4300**"`,
    # 而 plan.md 是**人写的、按市场周期更新**的文件 —— 一旦更新
    # （如长假复市后重新校准关键位），两个变异体的 `apply` 直接
    # `return False`，回归输出变成「锚点未匹配，跳过」，看起来像
    # "通过"而实际**零判别力**。这正是本项目反复栽的
    # "变异体没造出触发条件" 那一类（见记忆
    # market-watch-mutant-fixture-own-trigger）。
    # 现在改成：日期从标题里的 `(YYYY-MM-DD)` 正则取、阈值从
    # sectors.json 的 `hstech_down` 取 —— 两者都是**被测对象之外的
    # 独立真值**，plan.md 怎么改都能造出触发条件。
    import re as _re_pm, json as _js_pm
    _pp = os.path.join(S, "conf", "plan.md")
    if not os.path.exists(_pp):
        return False
    _ps = open(_pp, encoding="utf-8").read()
    if name == "plan-md-stale-position":
        _m = _re_pm.search(r"##\s*当前市场位置\s*[（(]\s*(\d{4}-\d{2}-\d{2})",
                           _ps)
        if not _m:
            return False
        _old_d = _m.group(1)
        # ⚠️ 回退幅度必须**稳稳超过**自检的红线（`_nd > 5` 个交易日）。
        # 第一版写"往前挪 15 天"，而 15 个自然日里含周末、交易日只有
        # **5 个** —— 恰好卡在边界（`> 5` 不成立）→ 自检只报 ⚠️ 不报 ❌
        # → 变异体**漏网**（`[实测]` 2026-10-10：rc=0、❌数=0）。
        # 这与本文件反复记的"变异体没造出触发条件"同型：**先问触发条件，
        # 再问检查项**。改成往前 **30 个自然日**（≥20 个交易日），
        # 无论中间夹多少周末/假期都远超阈值。
        import datetime as _dt_pm
        _new_d = (_dt_pm.date.fromisoformat(_old_d)
                  - _dt_pm.timedelta(days=30)).isoformat()
        _ps = _ps.replace(f"（{_old_d}）", f"（{_new_d}）", 1)
    else:
        # 真值从 sectors.json 读（判据侧也是这么读的），不抄 plan.md
        try:
            _th_pm = (_js_pm.load(open(os.path.join(S, "conf", "sectors.json"),
                                       encoding="utf-8"))
                      .get("thresholds") or {})
            _real = float(_th_pm.get("hstech_down"))
        except Exception:
            return False
        _old_s = f"下方 **{_real:g}**"
        if _old_s not in _ps:
            return False
        _ps = _ps.replace(_old_s, f"下方 **{_real - 100:g}**", 1)
    open(_pp, "w", encoding="utf-8").write(_ps)
    return True


def _inj_panel_trusts_fired(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 复合变异：既改源码（删掉 _fired_no_push 纠正分支），又造夹具
    # （今天有推送记录，但 state.fired 里含一个**没有**成功推送记录的
    # hourly 槽位 —— 这正是"未发出却显示已完成"的触发条件）。
    #
    # ⚠️ 单改源码测不到：该段的行为测试用**真实沙箱 state/pushes**，
    # 而沙箱里 state.date 是 09-30（非今天），断言块 `if _pt12 and
    # _st12.get("date") == _today12` 直接跳过。必须先造出触发条件。
    import json as _js, datetime as _dt
    _sp = os.path.join(S, "state", "state.json")
    _pp = os.path.join(S, "state", "history", "pushes.jsonl")
    if not os.path.exists(_sp) or not os.path.exists(_pp):
        return False
    _today = _dt.date.today().isoformat()
    # 1) state.json：date=今天，fired 含一个无推送记录的 hourly 槽位
    _d = _js.load(open(_sp, encoding="utf-8"))
    _d["date"] = _today
    _d["fired"] = ["hourly_15:00"]     # 15:00 今天**没有**成功推送
    with open(_sp, "w", encoding="utf-8") as _fh:
        _js.dump(_d, _fh, ensure_ascii=False)
    # 2) pushes.jsonl：今天有一条**别的**槽位的成功记录（让 _pt12 非空），
    #    但 15:00 不在其中 → 触发 fired-no-push
    _rec = {"t": f"{_today} 10:00:00", "slot": "10:00", "kind": "hourly",
            "ok": True, "text": "x", "resp": "{}"}
    with open(_pp, "a", encoding="utf-8") as _fh:
        _fh.write(_js.dumps(_rec, ensure_ascii=False) + "\n")
    # 3) 源码：删掉 _fired_no_push 纠正分支
    _wp = os.path.join(S, "src", "webui.py")
    _ws = open(_wp, encoding="utf-8").read()
    _old = ("        elif key in _fired_no_push:\n"
            "            # 状态文件说\"推过了\"，推送记录说\"没有\" —— 以**推送记录**为准：\n"
            "            # 消息是否送达才是读者关心的，而记录是 push() 亲手写的。\n"
            "            status = \"missed\"\n")
    if _old not in _ws:
        return False
    open(_wp, "w", encoding="utf-8").write(_ws.replace(_old, "", 1))
    return True




def _inj_webhook_missing(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 config.json 删掉 webhook → 推送必失败。
    import json as _js
    _cp = os.path.join(S, "conf", "config.json")
    if not os.path.exists(_cp):
        return False
    try:
        _d = _js.load(open(_cp, encoding="utf-8"))
    except Exception:
        return False
    if "webhook" not in _d:
        return False
    _d["webhook"] = ""
    with open(_cp, "w", encoding="utf-8") as _fh:
        _js.dump(_d, _fh, ensure_ascii=False)
    return True


def _inj_vr_base_toplevel_list(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 vr_base.json 顶层改成 list（应为 dict）。
    import json as _js
    _vp = os.path.join(S, "state", "vr_base.json")
    if not os.path.exists(_vp):
        return False
    with open(_vp, "w", encoding="utf-8") as _fh:
        _js.dump([{"date": "2026-01-01"}], _fh, ensure_ascii=False)
    return True


def _inj_deadline_dates_empty(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 sectors.json 的 deadline_dates 清空。
    # ⚠️ `config()` 读的是 sectors.json（`_CFG_PATH`），不是 config.json。
    import json as _js
    _sp2 = os.path.join(S, "conf", "sectors.json")
    if not os.path.exists(_sp2):
        return False
    try:
        _d = _js.load(open(_sp2, encoding="utf-8"))
    except Exception:
        return False
    if "deadline_dates" not in _d:
        return False
    _d["deadline_dates"] = []
    with open(_sp2, "w", encoding="utf-8") as _fh:
        _js.dump(_d, _fh, ensure_ascii=False)
    return True


def _inj_news_time_not_str(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 pending.json 的 news[0].time 改成数字。
    # `[序列化回归]` 要断言真的检查过条目（空 news 走 _unverified），
    # 所以必须先塞一条 news 进去。
    import json as _js
    _pp = os.path.join(S, "state", "pending.json")
    if not os.path.exists(_pp):
        return False
    try:
        _d = _js.load(open(_pp, encoding="utf-8"))
    except Exception:
        return False
    _d["news"] = [{"time": 1234567890, "title": "注入"}]  # time 应为 str
    with open(_pp, "w", encoding="utf-8") as _fh:
        _js.dump(_d, _fh, ensure_ascii=False)
    return True


def _inj_state_hourly_retry_type(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：改沙箱 state.json 的 hourly_retry 类型（list 而非
    # dict）。这是 analyze.py:1619 `setdefault(...)[hourly]=ts` 的真实
    # 崩溃形态（历史缺陷）。date 改今天让 [状态文件] 的校验分支进入。
    import json as _js, datetime as _dt
    _sp = os.path.join(S, "state", "state.json")
    if not os.path.exists(_sp):
        return False
    try:
        _d = _js.load(open(_sp, encoding="utf-8"))
    except Exception:
        return False
    _d["date"] = _dt.date.today().isoformat()
    _d["hourly_retry"] = ["injected-bad"]   # 应为 dict
    _d.setdefault("fired", [])
    with open(_sp, "w", encoding="utf-8") as _fh:
        _js.dump(_d, _fh, ensure_ascii=False)
    return True


def _inj_sandbox_carries_real_webhook(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型（2026-10-20）：把**生产 config.json 的真实 webhook**
    # 写回沙箱 —— 复刻 09:49 事故的数据条件。make_sandbox 已把沙箱
    # 换成无效 key，这里显式覆盖回真的，检查项必须当场报红。
    # ⚠️ 不真发任何请求：本变异体只改数据，不触发推送。
    import json as _js_wh2
    _cp_r = os.path.join(S, "conf", "config.json")
    _prod_cp = os.path.join(ROOT, "conf", "config.json")
    try:
        _d_r = _js_wh2.load(open(_cp_r, encoding="utf-8"))
        _real_r = (_js_wh2.load(open(_prod_cp, encoding="utf-8"))
                   .get("webhook") or "")
    except Exception:
        return False
    if not _real_r:
        return False
    _d_r["webhook"] = _real_r
    with open(_cp_r, "w", encoding="utf-8") as _fh_r:
        _js_wh2.dump(_d_r, _fh_r, ensure_ascii=False)
    return True


def _inj_probe_leftover_uncleaned(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 纯数据注入型：沙箱 cache/ 里放一个真实的残留锁（0600，形态与
    # `_open_lockfile` 建出来的一致），判据必须当场报出来。
    # ⚠️ 0600 是硬要求：0644 会先被 `[状态文件权限]` 抓走，本变异体的
    # must_contain 就永远匹配不上（`[实测]` 2026-10-21）。
    _lk = os.path.join(S, "state", "cache",
                       ".probe_mutant_leftover.json.lock")
    os.makedirs(os.path.dirname(_lk), exist_ok=True)
    _fdl = os.open(_lk, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(_fdl)
    return True


def _inj_sched_calendar_dead(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 特殊：改沙箱里的 marketwatch plist（不在 SRC 内，在 LaunchAgents/）。
    _pl = os.path.join(S, "LaunchAgents", "com.mison.marketwatch.plist")
    if not os.path.exists(_pl):
        return False
    txt = open(_pl).read()
    if old not in txt:
        return False
    open(_pl, "w").write(txt.replace(old, new, 1))
    return True


def _inj_webhook_url_malformed(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 config.json 的 webhook 路径少一个字母（send→sen）。
    import json as _js_wh
    _cp_w = os.path.join(S, "conf", "config.json")
    if not os.path.exists(_cp_w):
        return False
    _d_w2 = _js_wh.load(open(_cp_w, encoding="utf-8"))
    _h_w = _d_w2.get("webhook") or ""
    if "/cgi-bin/webhook/send" not in _h_w:
        return False
    _d_w2["webhook"] = _h_w.replace("/cgi-bin/webhook/send",
                                    "/cgi-bin/webhook/sen", 1)
    open(_cp_w, "w", encoding="utf-8").write(
        _js_wh.dumps(_d_w2, ensure_ascii=False))
    return True


def _inj_sched_wrong_script(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 plist 的 ProgramArguments[1] 改成 collect.py。
    # 危害：跑起来的是**别的东西**且 exit 0 —— last-exit-code 判据抓不到。
    import plistlib as _pl_w
    _wp = os.path.join(S, "LaunchAgents", "com.mison.marketwatch.plist")
    if not os.path.exists(_wp):
        return False
    _d_w = _pl_w.loads(open(_wp, "rb").read())
    _pa_w = _d_w.get("ProgramArguments")
    if not (isinstance(_pa_w, list) and len(_pa_w) >= 2):
        return False
    _pa_w[1] = _pa_w[1].replace("analyze.py", "collect.py")
    _d_w["ProgramArguments"] = _pa_w
    open(_wp, "wb").write(_pl_w.dumps(_d_w))
    return True


def _inj_sched_selfcheck_no_runatload(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱 selfcheck plist 的 RunAtLoad true → false。
    # ⚠️ 变异体的**触发条件必须自己造**：沙箱 plist 是从生产拷的，
    # 生产已是 true，所以这里改成 false 才有东西可抓（若生产 plist
    # 本身没 RunAtLoad，本分支 return False → 锚点未匹配，宁可跳过
    # 也不能让它变成空操作后报"漏网"）。
    import plistlib as _pl_rl
    _rp = os.path.join(S, "LaunchAgents",
                       "com.mison.marketselfcheck.plist")
    if not os.path.exists(_rp):
        return False
    _d_rl = _pl_rl.loads(open(_rp, "rb").read())
    if _d_rl.get("RunAtLoad") is not True:
        return False
    _d_rl["RunAtLoad"] = False
    open(_rp, "wb").write(_pl_rl.dumps(_d_rl))
    return True


def _inj_dry_run_flag_left_on(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：沙箱造一个新鲜 DRY_RUN（mtime=现在）。默认沙箱不带该文件。
    _dp = os.path.join(S, "state", "DRY_RUN")
    open(_dp, "w").write("")
    return True


def _inj_audit_trail_stops(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：造「任务在跑、埋点没写」—— runs.jsonl 末条改到 2 小时前，
    # **同时把 watch.log 刷新到当下**。判据应报 ❌「执行流水停写」。
    #
    # ⚠⚠ 2026-11-13（复核，时段依赖修复）：原实现只改 runs.jsonl，
    # 注释写「watch.log 保持新鲜」—— 那是**默认生产刚好在跑**。
    # 而本变异体是晚间跑的：盘后 `watch.log` 停在 15:01，注入后
    # runs 与 watch.log **都陈旧**，判据走的是「两边都静默 → 无法验证」
    # 那条 else 分支，压根不会报「停写」。
    # `[实测]` 同一个版本：13:50 单跑 ✅抓住，22:19 全量 ❌漏网，
    # rc=0/❌数=0 —— 差别只是「跑的时候生产有没有在写日志」。
    # 这属于**变异体自身的环境依赖**，不是检查项的盲区：
    # 判据要求「watch.log 仍新」是**正确的**（都静默时无法区分
    # 「埋点坏了」与「任务压根没跑」，那时候只能记「未验证」）。
    # 修法：注入时主动把 watch.log 的 mtime 与末条时间戳都推到当下，
    # 让「任务在跑、埋点没写」这个场景**确定成立**，与运行时刻无关。
    import json as _js_at, datetime as _dt_at, time as _tm_at
    _rp = os.path.join(S, "state", "history", "runs.jsonl")
    _lp = os.path.join(S, "logs", "watch.log")
    if not (os.path.exists(_rp) and os.path.exists(_lp)):
        return False
    _old_ts = (_dt_at.datetime.now()
               - _dt_at.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    _lines = [l for l in open(_rp, encoding="utf-8").read().splitlines()
              if l.strip()]
    if not _lines:
        return False
    _d = _js_at.loads(_lines[-1])
    _d["t"] = _old_ts
    _lines[-1] = _js_at.dumps(_d, ensure_ascii=False)
    open(_rp, "w", encoding="utf-8").write("\n".join(_lines) + "\n")
    # watch.log 补一条**当下时刻**的记录并同步 mtime —— 判据读的是
    # 末行时间戳与 mtime 两者，缺一个都算不新鲜。
    _now_at = _dt_at.datetime.now().strftime("%m-%d %H:%M:%S")
    with open(_lp, "a", encoding="utf-8") as _fl:
        _fl.write(f"[{_now_at}] 注入：保持 watch.log 新鲜（变异体自用）\n")
    try:
        os.utime(_lp, (_tm_at.time(), _tm_at.time()))
    except Exception:
        pass
    return True


def _inj_feeds_partial_dead(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：把夹具健康表里的 baidu/ths 置假（源码不动 —— 见该
    # 变异体定义处的长注释：`res = {}` 在 MW_HEALTH_FIXTURE 短路之后，
    # 改源码对返回值零影响）。判据带 must_contain「降级链少 2 档」——
    # 防「报了 ❌ 但文案指向别处」。
    _hp = os.path.join(S, "health_fixture.json")
    if not os.path.exists(_hp):
        return False
    import json as _js_hp
    _hd = _js_hp.load(open(_hp, encoding="utf-8"))
    if not isinstance(_hd, dict):
        return False
    _hd["baidu"] = False
    _hd["ths"] = False
    with open(_hp, "w", encoding="utf-8") as _fh:
        _js_hp.dump(_hd, _fh)
    return True


def _inj_feeds_risk_blocked(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：夹具健康表 baidu 置假 + 带 `_risk: ["baidu"]`（源码
    # 不动 —— 见该变异体定义处的长注释：夹具分支读 `_risk` 填
    # feeds.RISK_BLOCKED，返回值里不含 `_risk`）。
    # 判据带 must_contain「风控」—— 防「报了 ❌ 但文案指向别处」：
    # 风控源若被当成坏源，会打「❌ 1 个源不可用: baidu —— 降级链少
    # 1 档」，而正确实现打「⚠️ baidu … 被目标站点风控拦截 … 本轮未验证」。
    _hp = os.path.join(S, "health_fixture.json")
    if not os.path.exists(_hp):
        return False
    import json as _js_hp2
    _hd2 = _js_hp2.load(open(_hp, encoding="utf-8"))
    if not isinstance(_hd2, dict):
        return False
    _hd2["baidu"] = False
    _hd2["_risk"] = ["baidu"]
    with open(_hp, "w", encoding="utf-8") as _fh:
        _js_hp2.dump(_hd2, _fh)
    return True


def _inj_sched_selfcheck_rc2_alarm(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：把夹具状态里的 selfcheck rc 从 1 改成 2（源码不动，
    # 代码是好的 —— 这条测的是「rc=2 出现时检查必须报」，单向覆盖；
    # 反向「代码把 rc=2 吞掉」仍归代码审查，理由同旧注释）。
    # ⚠️ rc=2 不能进**默认夹具** —— 基线会带 ❌（正确代码会报），
    # 回归当场中止。所以只有这一条变异体自己改状态。
    _sp = os.path.join(S, "launchctl_state.json")
    if not os.path.exists(_sp):
        return False
    import json as _js_rc2
    _st = _js_rc2.load(open(_sp, encoding="utf-8"))
    _hit = False
    for _j in _st:
        if _j.get("label") == "com.mison.marketselfcheck":
            _j["rc"] = 2
            _hit = True
    if not _hit:
        return False
    with open(_sp, "w", encoding="utf-8") as _fh:
        _js_rc2.dump(_st, _fh)
    return True


def _inj_sched_agent_rc1_alarm(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 数据注入型：把夹具里 marketagent 的 rc 从 0 改成 1（源码不动）。
    # ⚠️ rc=1 不能进默认夹具 —— 正确代码**本来就要报**（设计如此），
    # 基线会带 ❌、回归当场中止。只有这条变异体自己改状态。
    _sp = os.path.join(S, "launchctl_state.json")
    if not os.path.exists(_sp):
        return False
    import json as _js_arc
    _st = _js_arc.load(open(_sp, encoding="utf-8"))
    _hit = False
    for _j in _st:
        if _j.get("label") == "com.mison.marketagent":
            _j["rc"] = 1
            _hit = True
    if not _hit:
        return False
    with open(_sp, "w", encoding="utf-8") as _fh:
        _js_arc.dump(_st, _fh)
    return True


def _inj_sched_agent_rc1_misclassified(S, name, old, new):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 双重注入：① 夹具把 agent rc 设成 1（造出触发条件）；② 源码打断
    # agent 分类分支 → rc=1 落回常驻分支 → 误报「反复起、反复失败」。
    _sp = os.path.join(S, "launchctl_state.json")
    if not os.path.exists(_sp):
        return False
    import json as _js_am
    _st = _js_am.load(open(_sp, encoding="utf-8"))
    _hit = False
    for _j in _st:
        if _j.get("label") == "com.mison.marketagent":
            _j["rc"] = 1
            _hit = True
    if not _hit:
        return False
    with open(_sp, "w", encoding="utf-8") as _fh:
        _js_am.dump(_st, _fh)
    # ⚠️ 哨兵 fname（__DATA_LC_…）不是文件 —— 源码固定编辑 selfcheck.py
    _p2 = os.path.join(S, "src", "selfcheck.py")
    _sc2 = open(_p2, encoding="utf-8").read()
    # ⚠️ 必须换成**已定义但不匹配**的表达式（`in ()`），不能换成
    #    `_AGENT_JOBS_x` 这种未定义名 —— 那会抛 NameError，被 `[定时任务]`
    #    的段级 except 吞成「检查项自身异常」，走不到「落回常驻分支」，
    #    于是 must_contain 的文案永远不出现、变异体假漏网（2026-10-16 实测）。
    _old2 = "            if label in _AGENT_JOBS:"
    if _old2 not in _sc2:
        return False
    open(_p2, "w", encoding="utf-8").write(
        _sc2.replace(_old2, "            if label in ():", 1))
    return True


def _inj_meta_guard_miss_section(S, name, old, new, p, sc):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 去掉 `[数据源]` 标题的方括号 —— 任何"收集 [XXX] 段标题"的判据
    # 都认不出它，制造"整段没被收集、段内标记成孤儿"。
    if 'print("\\n[数据源]")' not in sc:
        return False
    open(p, "w").write(sc.replace('print("\\n[数据源]")',
                                  'print("\\n数据源")', 1))
    return True


def _inj_meta_guard_unfailable_section(S, name, old, new, p, sc):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 把 `[跨进程冷却]` 段内所有 ok_all=False / _unverified 计数改成 noop。
    i = sc.index('print("\\n[跨进程冷却]")')
    j = sc.index('print("\\n[', i + 10)
    seg = sc[i:j]
    new = (seg.replace("ok_all = False", "_noop = False")
              .replace("_unverified[0] += 1", "_noop2 = 1"))
    if new == seg:
        return False
    open(p, "w").write(sc[:i] + new + sc[j:])
    return True




def _inj_holidays_empty(S, name, old, new, p, sc):
    """数据注入型变异体注入器（由 apply_mutant 按名字分派）。"""
    # 特殊：把整个 HOLIDAYS dict 字面量替换成 `{}`（不能只替换 `HOLIDAYS = `
    # —— 那会留下原 dict 内容的残体，制造语法错误）。
    # ⚠️ 必须行首锚定：裸子串 "HOLIDAYS = {" 现在也会命中
    # "HK_HOLIDAYS = {"（2026-10-19 新增港股日历后），`index()` 取到的
    # 是**第一个**出现（A 股表，巧合正确）—— 但一旦文件里顺序变化就会
    # 悄悄清空错误的表。用换行前缀把它钉死。
    i = sc.index("\nHOLIDAYS = {") + 1
    j = sc.index("{", i)
    depth = 0
    for k in range(j, len(sc)):
        if sc[k] == "{":
            depth += 1
        elif sc[k] == "}":
            depth -= 1
            if depth == 0:
                break
    open(p, "w").write(sc[:i] + "HOLIDAYS = {}" + sc[k + 1:])
    return True


# ------------------------------------------------------------------
# 数据注入型变异体的注入器注册表（2026-11-15 精练，从 apply_mutant 抽出）
# ------------------------------------------------------------------
# 每个注入器 (S, name, old, new) -> bool：在沙箱 S 里造出该缺陷，
# 返回 True=注入成功、False=前置条件不满足（调用方记「跳过」）。
# 抽取理由与约定见 apply_mutant 里分派处的注释。
# 第二张表 `_INJECTORS_SRC` 见下方：那 3 条改的是**沙箱源码**本身，
#
# ⚠️ 2026-11-15 一并清掉两条**历史遗留的死注入器**（在 MUTANTS 里早已无
# 对应定义，只剩注入分支，永远不会被触发）：
#   · `sched-job-missing`  —— 实测漏网且**测试环境无法隔离**：[定时任务]
#     读的是宿主真实 `launchctl list`，沙箱删 plist 对它零影响；要覆盖得
#     真 bootout 一个生产任务，代价不可接受。该段只能人工核对。
#   · `section-unfailable` —— 定义与实际注入严重脱节，且语义与
#     `meta-guard-unfailable-section` 重复，当年已删定义、漏删分支。
# 保留这段说明是为了后人别再"补"回来。
_INJECTORS = {}
# 改「沙箱源码」的注入器：签名多两个参数 (p, sc)，须在 apply_mutant 里
# 取到 p/sc 之后调用（源码替换与这些注入互斥）。
_INJECTORS_SRC = {}
_INJECTORS_SRC['holidays-empty'] = _inj_holidays_empty
_INJECTORS_SRC['meta-guard-miss-section'] = _inj_meta_guard_miss_section
_INJECTORS_SRC['meta-guard-unfailable-section'] = _inj_meta_guard_unfailable_section

_INJECTORS['hygiene-glob-wrong'] = _inj_hygiene_glob_wrong
_INJECTORS['fallback-th-drift'] = _inj_fallback_th_drift
_INJECTORS['fallback-literal-drift'] = _inj_fallback_literal_drift
_INJECTORS['fallback-maxpush-drift'] = _inj_fallback_maxpush_drift
_INJECTORS['webui-hang'] = _inj_webui_hang
_INJECTORS['workdoc-guard-window-drift'] = _inj_workdoc_guard_window_drift
_INJECTORS['workdoc-advice-clause-removed'] = _inj_workdoc_guard_window_drift
_INJECTORS['plan-md-stale-position'] = _inj_plan_md_stale_position
_INJECTORS['plan-md-threshold-drift'] = _inj_plan_md_stale_position
_INJECTORS['panel-trusts-fired'] = _inj_panel_trusts_fired
_INJECTORS['webhook-missing'] = _inj_webhook_missing
_INJECTORS['vr-base-toplevel-list'] = _inj_vr_base_toplevel_list
_INJECTORS['deadline-dates-empty'] = _inj_deadline_dates_empty
_INJECTORS['news-time-not-str'] = _inj_news_time_not_str
_INJECTORS['state-hourly-retry-type'] = _inj_state_hourly_retry_type
_INJECTORS['sandbox-carries-real-webhook'] = _inj_sandbox_carries_real_webhook
_INJECTORS['probe-leftover-uncleaned'] = _inj_probe_leftover_uncleaned
_INJECTORS['sched-calendar-dead'] = _inj_sched_calendar_dead
_INJECTORS['sched-processtype-background'] = _inj_sched_calendar_dead
_INJECTORS['webhook-url-malformed'] = _inj_webhook_url_malformed
_INJECTORS['sched-wrong-script'] = _inj_sched_wrong_script
_INJECTORS['sched-selfcheck-no-runatload'] = _inj_sched_selfcheck_no_runatload
_INJECTORS['dry-run-flag-left-on'] = _inj_dry_run_flag_left_on
_INJECTORS['audit-trail-stops'] = _inj_audit_trail_stops
_INJECTORS['feeds-partial-dead'] = _inj_feeds_partial_dead
_INJECTORS['feeds-risk-blocked'] = _inj_feeds_risk_blocked
_INJECTORS['sched-selfcheck-rc2-alarm'] = _inj_sched_selfcheck_rc2_alarm
_INJECTORS['sched-agent-rc1-alarm'] = _inj_sched_agent_rc1_alarm
_INJECTORS['sched-agent-rc1-misclassified'] = _inj_sched_agent_rc1_misclassified


def apply_mutant(S, fname, old, new, name=None):
    # 2026-11-15（精练）：原先这里是 31 个 if/elif 分支、约 600 行
    # 平铺注入代码。每条数据注入型变异体各占一段，读完要滚 600 行才知道
    # 某条到底改了什么；新增一条得在这条长链末尾再插一段，位置敏感、
    # 极易插错缩进。
    #
    # 现改为注册表分派：每个注入器是独立函数（见 _INJECTORS），
    # 签名统一 (S, name, old, new) -> bool（True=注入成功）。
    # 1 查一条只需看它自己那个函数；2 新增不依赖插入位置；
    # 3 每个注入器可被测试直接调用，不必跑整条 apply_mutant。
    #
    # 分派必须在源码替换逻辑之前 —— 这些注入器改的是沙箱里的
    # 配置文件/plist/state，不是 SRC 下的源码，与下面逐字替换互斥。
    _inj = _INJECTORS.get(name)
    if _inj is not None:
        return _inj(S, name, old, new)
    # 分层后源码在 src/；带 "/" 的 fname（agent/...）按沙箱同构路径取
    p = (os.path.join(S, fname) if "/" in fname
         else os.path.join(S, "src", fname))
    sc = open(p).read()
    # ⚠️ 2026-11-15（精练）：这 3 条注入器要读/写**沙箱里那份源码**
    # （`sc` 与 `p`），所以必须在上面 p/sc 取到之后才能调用 ——
    # 它们不放进 `_INJECTORS` 注册表（那张表是"改沙箱外文件"的注入器，
    # 与逐字替换互斥），这里单独分派。
    if name in ("holidays-empty", "meta-guard-miss-section",
                 "meta-guard-unfailable-section"):
        return _INJECTORS_SRC[name](S, name, old, new, p, sc)
    # ⚠️ 2026-11-15（精练）：此处原有一条
    #     `if name in ("sched-calendar-dead", "sched-processtype-background"):
    #          return False` 的守卫，已删除 —— 它是**死代码**。
    # 函数上方（处理 plist 的那条分支）对该两个名字的**所有**路径都
    # 显式 return（无 plist → False、锚点不中 → False、改成功 → True），
    # 永远不会落到这里。留着会让读者以为存在"第二道防线"，
    # 从而在排查"为什么这两条永远跳过"时找错地方。
    if old not in sc:
        return False
    # ⚠️ 非唯一锚点不能静默只替第一处：那会让变异体只造出**一半**缺陷，
    # 判据若恰好由"未变异那一半"驱动就会报「漏网」，而成因指向变异体
    # 自身（假漏网），排查方向完全错。webui.py 的 `render()` 里有**两份**
    # 逐字相同的 rmap 块（状态列与结论列各一），两个 rmap 变异体都踩这条。
    if sc.count(old) > 1:
        import sys as _sys_u
        print(f"[变异体] {name}: 锚点在 {fname} 出现 {sc.count(old)} 次（应为 1）"
              f" —— 拒绝替换，避免造出半血缺陷", file=_sys_u.stderr, flush=True)
        return False
    _new_sc = sc.replace(old, new, 1)
    # ⚠️ 变异后必须仍能 `ast.parse`（2026-10-25 加）：
    # 替换串里一个 raw-string 的 `\n` 写成字面量，就会让 marketlib.py
    # 语法错 —— 而它的表现是 **`analyze` 导入时 `sys.exit(1)` → 自检报
    # 「自检自身异常退出: SystemExit: 1」、rc=2**，看上去像真缺陷，
    # 实际是变异体自己坏了（我为此白查了二十分钟）。
    # 语法错就地拒绝并大声说明，别让它伪装成被测系统的异常。
    if fname.endswith(".py"):
        import ast as _ast_v
        try:
            _ast_v.parse(_new_sc)
        except SyntaxError as _se_v:
            import sys as _sys_v
            print(f"[变异体] {name}: 变异后 {fname} 语法错误"
                  f"（第 {_se_v.lineno} 行: {_se_v.msg}）—— 拒绝注入，"
                  f"多半是替换串里的转义写成了字面量", file=_sys_v.stderr,
                  flush=True)
            return False
    open(p, "w").write(_new_sc)
    return True

def _all_sections():
    """用 AST 静态列出 selfcheck.py 的全部检查段标题（与运行无关）。"""
    import ast
    tree = ast.parse(open(os.path.join(SRC, "selfcheck.py")).read())
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "print" and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            v = n.args[0].value.strip()
            if v.startswith("[") and v.endswith("]"):
                out.append(v)
    return sorted(set(out))

# ---------- 单条变异体的执行 + 判定（串行/并行共用的唯一实现） ----------
#
# ⚠️ 抽成函数是**并行化的前提**，也是**判据唯一性**的保证：串行路径与并行
# 路径必须调同一份代码，否则「两份 must_contain 闸门改一处忘一处」的历史
# 会以「串行版对了、并行版没改」的形式再犯一次（本文件已因同型问题亏过
# 两轮：4770/4820 那对闸门）。
_CTRL_NAME = "__noop-control__"   # 空操作对照的保留名（绝不与真实变异体重名）

# 默认并行度 —— **刻意取小**。2026-11-03 实测：8 路把本机 CPU 打到 97%、
# load average 冲到 229~302（用户当场手动杀进程，要求「不要一次开那么多」）。
# 每条变异体自己还会起子进程（selfcheck），实际并发是 MW_JOBS 的倍数。
#
# ⚠️⚠️ 2026-11-17（动态并发）：这里**不套用** `concurrency.dynamic_workers()`。
#
# 那个函数是为**单元测试**标定的 —— 单元测试是 IO 密集型
# （`[实测]` 8 个 sleep 子进程的 CPU 平均并发只有 0.9，时间全花在进程启动
# 和文件 IO 上），所以并发越高越快（2路 66s · 12路 25s）。
#
# 而变异测试是 **CPU 密集型**：每条都跑一整个 2 万行的 selfcheck
# （`[实测]` 单条 ~150s，其中 selfcheck 53s + agent 探针 27s，都在真算/真跑）。
# 对 CPU 密集型任务，超过物理核数只会互相拖累 —— 上面 2026-11-03 那条
# 「8 路把 CPU 打到 97%」就是证据。
#
# 所以这里仍用固定小值，`MW_JOBS` 可显式调。默认值 3 保持不变。
_DEFAULT_JOBS = 3
try:
    _MW_JOBS = max(1, int(os.environ.get("MW_JOBS") or _DEFAULT_JOBS))
except ValueError:
    _MW_JOBS = _DEFAULT_JOBS


def _run_one_mutant(_mut, _emit):
    """在**独立沙箱**里跑一条变异体并判定，返回结果 dict。

    `_mut is None` = 空操作对照：一字不改源码，**必须**判「干净」。

    返回 {name, status: caught|missed|skipped|clean, reason, hits, silent_fail}。
    ⚠️ 本函数被多个线程同时调用 —— 只碰自己的沙箱 S 与加锁的单例
    （`_base` / `_probe_fixture_start`），不得新增模块级可写状态。
    """
    # ⚠️ 必须先初始化：下面 if/else 两个分支里只有一个会给它赋值，
    # 而 `_judge()` 两个分支都读 —— 漏了就是闭包 NameError
    # （2026-11-16 实测：空操作对照走 `if _mut is None` 分支，直接抛
    #  `cannot access free variable '_has_anchor'`，整条对照判失败，
    #  进而让整轮「漏网 0」作废）。
    _has_anchor = False
    # ⚠️ 同理：`must_contain` 也要在两个分支前定下来 —— `_judge` 用它算
    # 「归一化后的锚」。见下方归一化说明。
    _mc_norm = None
    if _mut is None:
        name, fname, old, new, desc = (_CTRL_NAME, "", "", "", "空操作对照")
        must_contain = None
    else:
        name, fname, old, new, desc = _mut[:5]
        # 五元組条目没有内联 must_contain —— 回退查 `_MUST_EXTRA`
        # （见其定义处：否则它们只要碰到任何一条不相关 ❌ 就算抓住）。
        must_contain = (_mut[5] if len(_mut) > 5
                        else _MUST_EXTRA.get(name))
        # ⚠️ 2026-11-15（精练）：五元组**必须**在 `_MUST_EXTRA` 里有条目。
        # 缺了会静默退回"只要多出任何一条 ❌ 就算抓住"的假绿判据 ——
        # 这是本项目最危险的那种退化：它不报错、只让判别力消失。
        # 现状 105 条五元组全有条目（已核对）；这里把它变成**运行期告警**，
        # 让将来新增五元组却忘记登记时立刻现形，而不是悄悄变绿。
        if must_contain is None:
            import sys as _sys_mc
            print(f"[变异体] {name}: 五元组缺 must_contain（_MUST_EXTRA 无此条）"
                  f" —— 判据会退化成「任何 ❌ 都算抓住」，请补上",
                  file=_sys_mc.stderr, flush=True)
        # ⚠⚠ 2026-11-16（P0，Codex 审查实测）：**空锚 = 假绿**。
        # 六元组的 `old` 可能是空串（如 `fallback-literal-drift` 用特殊注入器
        # 自己找锚），此时"锚点唯一性"无从检查，但判据仍会拿 must_contain
        # 去 match 全文 —— 文案只要在**任何**地方出现就算抓住。
        # 这里把"没有有效锚"的情形从**静默**变成**显式失败**（见下方 _judge）。
        _has_anchor = bool(old) and not str(old).startswith("__")
    # ⚠⚠⚠ 2026-11-16（本轮全量实测暴露的**真回归**）：
    # `must_contain` 若含时长数字，必须**同样归一化**再比对。
    #
    # `_fail_lines()` 把「15 分钟 / 17 分钟 / 45 分钟」统一抹成 `<时长>`
    # （那是为了防止墙钟漂移造成空操作对照误判，见其 docstring）。
    # 但 P0 改判据后，比对变成 `must_contain in <已归一化的行>` ——
    # 而 `must_contain` 是**原始字符串**，里面写着 `心跳超过 15 分钟仍判存活`。
    # 于是「15 分钟」永远不可能出现在「心跳超过 <时长>仍判存活」里：
    #   本轮实测漏网 2 条，全是这个原因（且全库只有这 2 条带时长数字）：
    #     · live-stale-never-true            → '心跳超过 15 分钟仍判存活'
    #     · hist-freshness-threshold-neutered → 'hist 17 分钟前应判 stale'
    #   两条的 ❌ 其实都**精准出现在新增失败行里**（假漏网）。
    #
    # 修法：把锚也过一遍 `_DUR_RE`。语义无损 —— 「有这条文案」这件事与
    # 具体是几分钟无关；真要改坏时长逻辑，行文本的其余部分会变，照样抓得住。
    # ⚠️ 这是**判据侧**的归一化，不是改变异体定义：锚保持人类可读的原文，
    # 免得将来有人照着 `<时长>` 去写锚（那会退化成对占位符的匹配）。
    if must_contain:
        _mc_norm = _DUR_RE.sub("<时长>", " ".join(must_contain.split()))
        if _mc_norm != must_contain:
            import sys as _sys_dn
            print(f"[变异体] {name}: must_contain 含时长数字，比对时已归一化为 "
                  f"{_mc_norm!r}（原文 {must_contain!r}）",
                  file=_sys_dn.stderr, flush=True)
    else:
        _mc_norm = must_contain
    # ⚠️ AI 冒烟相关的变异体必须让冒烟**真跑** —— runner 默认设
    # `MW_SKIP_AI_PROBE=1`，那条分支根本不执行（假漏网）。桩会拦截该次调用。
    _no_skip_ai = (name.startswith("ai-watchdog")
                   or name.startswith("ai-smoke"))

    def _judge(rc, out):
        _b_bad, _b_unv, _b_rc = _base()
        new_bad = _fail_lines(out, "❌") - _b_bad
        new_unv = _fail_lines(out, "未验证") - _b_unv
        hits = ({s for s, _t in new_bad} | {s for s, _t in new_unv})
        got = bool(new_bad) or bool(new_unv) or (_b_rc == 0 and rc != 0)
        why = ""
        # ⚠⚠ 2026-11-16（P0，Codex 审查实测的**假绿**）：
        # 原判据是 `must_contain not in out` —— 匹配的是**整份输出的全文**。
        # 于是「无关段新冒一条 ❌」+「目标文案恰好出现在别处」两条凑一起，
        # 判据就报 caught，而那条变异体的目标缺陷**根本没被验证到**。
        # Codex 用真实 `_run_one_mutant` 复现过：
        #   基线   "[目标] PASS"            变异输出 基线 + "[无关段] ❌ xx"
        #   → status="caught", hits={"[无关段]"}   ← 目标段什么都没验
        #
        # 修法：文案必须出现在**本轮新冒出来的失败行**里，而不是全文任意位置。
        # `new_bad`/`new_unv` 是 (段名, 行文本) 集合，天然就是"相对基线新增"，
        # 且已做过时长归一化 —— 直接在里面找子串即可。
        #
        # ⚠️ 不归因到**具体段名**：`must_contain` 是文案片段，无法可靠推断它
        # 该属于哪一段（同一条文案可能由多个段输出）。要求"出现在新增失败里"
        # 已经堵住了假绿的主路径，再绑定段名会造出新的假漏网。
        if _mc_norm:
            # ⚠️ 用**归一化后的** `_mc_norm` 比对：失败行已被 `_fail_lines`
            # 抹过时长数字，原始锚里的「15 分钟」永远匹配不上（见其定义处）。
            _hit_new = any(_mc_norm in _txt for _s, _txt in (new_bad | new_unv))
            if not _hit_new:
                got = False
                _act = _first_fail_detail(out)
                _inout = "（文案在全文里出现，但**不在本轮新增失败行**里 —— "
                _inout += "典型是措辞撞车或它本就在基线里）" if must_contain in out \
                    else "（全文里都没有）"
                why = (f"（新增失败行不含 {_mc_norm!r} {_inout}；"
                       f"实际首条 ❌：{_act[:110]!r}）")
        # ⚠️ 空锚：没有任何注入锚点时，这条变异体**无法证明**它真的被注入过
        # （`old` 为空 / `__XXX__` 特殊标记）。此时即便抓到也不计入有效判别力，
        # 必须显式标注，否则"379 全绿"里混着没有杠杆的条目。
        if not _has_anchor and got:
            why = (why or "") + "（⚠️ 无有效源码锚 —— 特殊注入器自定义注入位置，"
            why += "判别力不能等同于常规源码变异）"
        # ⚠️ 归因明细必须**带出去**（2026-11-03）：空操作对照被污染时，唯一
        # 有用的信息就是"相对基线**新冒出来**的是哪几行"。原实现只回 status，
        # 失败块里读 `_ctrl_res.get('line')` —— 而结果 dict 从来没有 `line`
        # 这个键，于是永远打印「对照未取得结果」，让人只能另写探针去逐个
        # 找翻转项（[[market-watch-mutation-anchor-drift]] 的同一类坑：
        # 诊断代码本身不可达 / 读不到的键）。把增量行与基线 rc 一起带回。
        return got, hits, why, new_bad, new_unv, _b_rc

    S = make_sandbox(fresh_pending=(name == "fresh-limit-huge"))
    try:
        if _mut is not None and not apply_mutant(S, fname, old, new, name):
            _emit(f"  ⏭️  {name}: 锚点未匹配，跳过")
            return {"name": name, "status": "skipped", "reason": "锚点未匹配",
                    "hits": set(), "silent_fail": False}
        try:
            rc, out = run(S, skip_ai=not _no_skip_ai)
            out = _with_agent_probe(S, out)
        except subprocess.TimeoutExpired:
            # ⚠️ 宿主负载尖峰会把正常变异体挤过 run() 的 1200s 上限
            # （2026-10-16 run38 实测 6 条：单跑复核**全部** ✅抓住）。超时
            # ≠ 漏网：直接记跳过会逼人手动复核。限重试 1 次，仍超时才记跳过。
            # 重试用**全新沙箱**重注入 —— 首跑被 kill 时沙箱可能停在半改状态。
            _emit(f"  ⏭️  {name}: 超时，歇 15s 后重试 1 次…")
            time.sleep(15)
            S2 = make_sandbox(fresh_pending=(name == "fresh-limit-huge"))
            try:
                if (_mut is not None
                        and not apply_mutant(S2, fname, old, new, name)):
                    _emit(f"  ⏭️  {name}: 重试时锚点未匹配")
                    return {"name": name, "status": "skipped",
                            "reason": "重试时锚点未匹配", "hits": set(),
                            "silent_fail": False}
                rc, out = run(S2, skip_ai=not _no_skip_ai)
                out = _with_agent_probe(S2, out)
            except subprocess.TimeoutExpired:
                _emit(f"  ⏭️  {name}: 重试后仍超时")
                return {"name": name, "status": "skipped",
                        "reason": "重试后仍超时", "hits": set(),
                        "silent_fail": False}
            finally:
                shutil.rmtree(S2, ignore_errors=True)

        got, hits, _why, _nbad, _nunv, _brc = _judge(rc, out)
        # ⚠️ "置位了却打不出 ❌" —— 结论行会报「有异常，见上」，但全文 0 个 ❌，
        # 读者 grep 不到任何东西。判据：rc≠0（有 ok_all=False 生效）但 0 个 ❌。
        silent_fail = (rc != 0) and ("❌" not in out)
        if _mut is None:
            _emit(f"  {'✅空操作对照干净' if not got else '❌空操作对照被误判'}"
                  f"  (rc={rc}, ❌数={out.count('❌')})")
            # ⚠️ 污染诊断：把"相对基线新冒出来的行"逐条列给上层（2026-11-03）。
            # 没污染时为空；污染时这就是唯一能定位「哪一段在翻转」的证据。
            _diag = []
            for _s, _t in sorted(_nbad):
                _diag.append(f"新增 ❌ [{_s}] {_t[:100]}")
            for _s, _t in sorted(_nunv):
                _diag.append(f"新增未验证 [{_s}] {_t[:100]}")
            if _brc == 0 and rc != 0:
                _diag.append(f"rc 由 0 变 {rc}（基线 rc={_brc}）")
            return {"name": name, "status": "clean" if not got else "polluted",
                    "hits": hits, "silent_fail": False,
                    "line": "\n   ".join(_diag) or None}
        tag = "✅抓住" if got else "❌漏网"
        mark = "  🔇静默失败" if silent_fail else ""
        _emit(f"  {tag}  {name}  (rc={rc}, ❌数={out.count('❌')}, "
              f"段={','.join(sorted(hits)) or '—'}){mark}{_why}  [{desc}]")
        return {"name": name, "status": "caught" if got else "missed",
                "hits": hits, "silent_fail": silent_fail}
    finally:
        shutil.rmtree(S, ignore_errors=True)

def main():
    # ⚠️ stdout 改**行缓冲**：本脚本要跑一小时，默认块缓冲下任何一次崩溃
    # （实测 2026-10-08 的 copytree 竞争）都会把已跑完的几十条结果整个丢掉。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    # ---------- 互斥闸门（2026-11-16 加，事故驱动） --------------------
    # ⚠⚠ **本脚本不能被两个实例同时跑。**
    # `[实测]` 2026-11-16 10:19：全量还在跑（MW_JOBS=6），我又另起一个
    # `MW_ONLY=urgent-no-return` 单跑复核，结果**干净环境下本该 ✅抓住 的
    # 那一条报「❌漏网」** —— 并发把宿主压满，检查项里依赖时钟/超时的分支
    # 走了不同路径，❌ 数从 4 变成 2，判据于是判「文案不含预期串」= 漏网。
    # 我据此差点去改一条**根本没问题**的变异体匹配串。
    # 这类假漏网比真漏网更费时间：它让人去修一个不存在的缺陷。
    # 判据只会被"同时写 state/ 与抢 CPU"破坏，故用文件锁直接拒绝第二个实例。
    # `MW_NO_LOCK=1` 留给确实知道自己在干什么的场景（如 CI 已串行化）。
    if not os.environ.get("MW_NO_LOCK"):
        global _LOCK_FH
        _lk_p = os.path.join(ROOT, "state", "cache", "mutation.lock")
        try:
            import fcntl as _fc
            # ⚠️ 先用 **"a+"** 打开再 flock：`"w"` 会在 flock **之前**就截断文件，
            # 而"锁被占"时正是**另一个活实例**在持锁 —— 那一截断会把它的 pid
            # 抹掉，于是提示里只能打 `pid=?`（实测踩到）。
            # `"a+"` 不截断；拿到锁后再 seek(0)+truncate 写自己的 pid。
            # ⚠️ 权限必须 0600：`open()` 走的是 umask（实测落成 0o644），
            # 而自检有一条「状态/日志/锁文件不得带 group/other 位」的检查，
            # 当场报 ❌。先用 os.open + O_CREAT 建好再包成文件对象。
            import os as _os_lk
            _fd_lk = _os_lk.open(_lk_p, _os_lk.O_RDWR | _os_lk.O_CREAT, 0o600)
            try:
                _os_lk.chmod(_lk_p, 0o600)      # 已存在的旧文件也纠正回来
            except OSError:
                pass
            _LOCK_FH = _os_lk.fdopen(_fd_lk, "a+")
            _fc.flock(_LOCK_FH, _fc.LOCK_EX | _fc.LOCK_NB)
            _LOCK_FH.seek(0)
            _LOCK_FH.truncate()
            _LOCK_FH.write(str(os.getpid()))
            _LOCK_FH.flush()
        except (OSError, IOError):
            _hint = ""
            try:
                _hint = open(_lk_p).read().strip()
            except Exception:
                pass
            print(f"❌ 已有一个变异测试实例在跑（pid={_hint or '?'}）—— 拒绝并发。\n"
                  f"   并发会把宿主压满，令依赖时钟的检查项走不同分支，\n"
                  f"   产出**假漏网**（实测 2026-11-16：本该抓住的报成漏网）。\n"
                  f"   等它跑完再跑；确需并跑请显式设 MW_NO_LOCK=1（后果自负）。")
            return 2

    print("变异测试：逐条注入缺陷，看自检是否抓住\n")

    # ---------- 增量模式（2026-10-15，用户要求：全量 168 条 ~5h 太浪费） ----------
    #
    # 基线文件 cache/mut_baseline.json 记录上次全绿时的：
    #   · 各目标文件的内容哈希（analyze/marketlib/webui/collect/feeds/selfcheck）
    #   · 每条变异体定义的哈希（repr 的 sha）
    #   · 上次漏网名单
    # 增量选择：目标文件变过 ∪ 定义变过 ∪ 上次漏网 ∪ 新增变异体。
    # 两条**硬规则**：selfcheck.py（检测器）变了 → 全量 —— 检测逻辑变了，
    # 每条变异体的判定都可能翻转，选择性重跑没有意义；无基线 → 全量（首跑）。
    # MW_FULL=1 强制全量；MW_ONLY 照旧（调试优先级最高）。
    # 全绿（0 漏网 0 锚点跳过）后回写基线 —— 这样基线永远指向"已验证"的状态。
    def _src_of(f):
        """变异体目标文件的真实路径：带 "/" 的按生产根（agent/...），
        其余在 src/（11 个主模块）。"""
        return (os.path.join(ROOT, f) if "/" in f
                else os.path.join(SRC, f))

    def _sha_file(p):
        try:
            import hashlib
            return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
        except OSError:
            return None

    def _mut_def_hash(m):
        import hashlib
        return hashlib.sha256(repr(m).encode("utf-8")).hexdigest()[:16]

    _MUT_BASELINE = os.path.join(ROOT, "state", "cache", "mut_baseline.json")

    def _load_baseline():
        try:
            import json as _jb
            d = _jb.load(open(_MUT_BASELINE, encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    # ⚠⚠⚠ 2026-11-16（P1，Codex 审查复现的**版本混合**）：
    # **快照必须在这里拍 —— 早于增量选片。**
    #
    # 原顺序是：①按需实时算 `_fchg` → ②选 `_inc_names` → ③（很久以后）拍
    # `_pre_file_hash`。中间隔着基线自检（可达数十秒）。Codex 用内存版本模型
    # 复现了这个序列：
    #     marketlib 变 → 选中它那 84 条
    #     选片之后 analyze 又被改 → ③ 的快照记下 **analyze 新哈希**
    #     84 条跑完 → 回写「全部文件哈希」
    #     → 下一轮增量：analyze 无变更 → **它那 130 条永不重跑**，却被记进
    #       新基线当成"已验证"
    # 这就是把一份**根本没跑过**的版本冻结成已验证 —— 与 2026-11-05 那次
    # 「脏基线仍回写」、2026-11-06「跑完才取哈希」是同一族缺陷的第三次复发。
    #
    # 修法：把「散落在各处的 `_sha_file()` 实时调用」收敛成**一次**快照，
    # 选片与回写都只读这份快照。同一轮内看到的世界只有一个版本。
    # ⚠️ 必须定义在 `_snap_files` 之前 —— 快照要把它并入（2026-11-16 修）
    _DATA_INPUTS = ("conf/sectors.json", "conf/config.json",
                    "conf/claude-plan.md")

    _snap_files = sorted({m[1] for m in MUTANTS}
                         | {"selfcheck.py", "selfcheck_mutation.py",
                            # `paths.py` 是全系统路径真值源，非任何变异体的
                            # 注入目标，但改它等于所有变异体的运行前提变了。
                            "paths.py"}
                         | set(_DATA_INPUTS))
    _pre_file_hash = {f: _sha_file(_src_of(f)) for f in _snap_files}
    _pre_mut_hash = {m[0]: _mut_def_hash(m) for m in MUTANTS}

    _only = os.environ.get("MW_ONLY", "")
    _inc_names = None          # None = 全量；set = 只跑这些
    # ⚠️ 数据注入型变异体的**输入文件**（2026-11-05，P2）：这类变异体改的是
    # 沙箱里的 conf/state 副本的**内容**（plan.md 窗口、sectors 阈值、
    # config webhook、LaunchAgents 状态…），前提直接取决于生产输入。原先
    # 增量哈希只登记源码文件 → 输入改了（比如 sectors.json 阈值调整）却算出
    # 「无变更」，这些变异体不再重跑，而它们的夹具前提可能已经变了。
    # ⚠️ 刻意**不含 plan.md**：它由调研 Agent **每天**更新，登记进去会让
    # 每轮增量都强制全量。"输入变了就重跑"针对的是低频的结构性输入。
    if _only:
        _mode = f"调试（MW_ONLY={_only!r}）"
    elif os.environ.get("MW_FULL") == "1":
        _mode = "全量（MW_FULL=1）"
    else:
        _baseline = _load_baseline()
        if not _baseline or "mutants" not in _baseline:
            _mode = "全量（无基线，首跑）"
        else:
            _base_files = _baseline.get("files") or {}
            # ⚠️ 读**快照**，不实时算 —— 见上面拍快照处的长注释（版本混合）。
            _fchg = [f for f, h in _base_files.items()
                     if _pre_file_hash.get(f) != h]
            # 基线里**没有登记**的输入文件同样算变更（框架升级后首轮
            # 必须补跑，否则新登记的输入永远比对不上）。
            for _f in _DATA_INPUTS:
                if _f not in _base_files and _f not in _fchg:
                    _fchg.append(_f)
            if "selfcheck.py" in _fchg:
                _mode = ("全量（selfcheck.py 变更 —— 检测器变了，"
                         "每条变异体的判定都可能翻转）")
            elif "selfcheck_mutation.py" in _fchg:
                # ⚠️ 2026-10-28 加：**框架自身**也要参与"检测器变了"判据。
                # 反例就是当天：把串行循环改成线程池 + 空操作对照 + 加锁，
                # 而 `files` 里只登记变异体目标文件与 selfcheck.py ——
                # 增量模式会算出「无变更 → 无需重跑」，把一次**判据实现
                # 整体换掉**的改动当成没发生。判定逻辑（`_run_one_mutant`
                # 的 must_contain 闸门 / 基线增量口径 / 超时重试）改过之后，
                # 每条变异体的结论都可能翻转，与 selfcheck.py 变更是同一类。
                _mode = ("全量（selfcheck_mutation.py 变更 —— 回归框架自身"
                         "变了，每条变异体的判定都可能翻转）")
            elif "paths.py" in _fchg:
                # ⚠️ 2026-11-01 加：`paths.py` 不是任何变异体的注入目标，
                # 但它是**全系统的路径真值源**（DIR/CONF/STATE/LOGS/CACHE/HIST
                # 与全部 p_* 助手）。改它等于同时改掉每条变异体的运行前提 ——
                # 若只登记为"变更文件"，`_inc_names` 里不会有任何一条
                # （没有变异体的 m[1] == "paths.py"），增量模式会报
                # 「无需重跑」。故与两个 selfcheck 文件同列，强制全量。
                _mode = ("全量（paths.py 变更 —— 路径真值源变了，"
                         "全部变异体的运行前提随之改变）")
            elif any(f in _fchg for f in _DATA_INPUTS):
                # ⚠️ 2026-11-05（P2）：数据注入型变异体的夹具前提直接来自
                # 这些输入 —— 阈值/窗口/配置一变，它们的注入可能整体失配，
                # 而源码哈希不会变。与 paths.py 同列，保守全量。
                _mode = ("全量（数据注入型输入变更：" +
                         "、".join(f for f in _DATA_INPUTS if f in _fchg) +
                         " —— 夹具前提可能变，必须全量复查）")
            else:
                _base_muts = _baseline.get("mutants") or {}
                _base_missed = set(_baseline.get("missed") or [])
                # ⚠⚠ 2026-11-16（P1，Codex 审查）：**失效判定必须走依赖闭包**。
                # 原来只判 `_m[1] in _fchg`（"变异体的目标文件自己变了吗"）。
                # 但 `marketlib.py` 是全系统的底座 —— 8 个模块全部 import 它
                # （analyze 35 处 / collect 25 / webui 24 / alert 16 / hot 2 /
                # feeds / news / runlog）。改 `marketlib` 的时间/节假日/取数
                # 助手，完全可能翻转 130 条 analyze 变异体的判定，而它们
                # 的目标文件没变 → 增量**一条都不重跑**。
                # Codex 实测：`marketlib.py` 变化只选中它自己的 84 条，
                # analyze 130 条与 hot 5 条全部未被选中。
                #
                # 修法：手写一张保守的依赖表（**不追求精确**，宁可多跑）。
                # 底座变了 → 依赖它的模块的变异体一并重跑。代价是多花时间，
                # 收益是"共享底座改动导致跨模块回归被跳过"这类静默不再发生。
                _DEPS = {
                    "marketlib.py": {"analyze.py", "collect.py", "alert.py",
                                     "webui.py", "hot.py", "feeds.py",
                                     "news.py", "runlog.py"},
                    "paths.py": {"analyze.py", "collect.py", "alert.py",
                                 "webui.py", "hot.py", "feeds.py",
                                 "news.py", "runlog.py"},
                }
                # 把变更文件**闭包展开**（底座 → 依赖它的全部模块）
                _fchg_eff = set(_fchg)
                for _cf_, _df in _DEPS.items():
                    if _cf_ in _fchg:
                        _fchg_eff |= _df
                _inc_names = set()
                for _m in MUTANTS:
                    _nm = _m[0]
                    if (_nm not in _base_muts
                            or _base_muts[_nm] != _mut_def_hash(_m)):
                        _inc_names.add(_nm)      # 新增或定义改过
                    elif _m[1] in _fchg_eff:
                        _inc_names.add(_nm)      # 目标文件或它的底座变过
                    elif _nm in _base_missed:
                        _inc_names.add(_nm)      # 上次漏网，复查
                _mode = (f"增量（变更文件 {_fchg or '无'} → "
                         f"重跑 {len(_inc_names)}/{len(MUTANTS)} 条；"
                         f"MW_FULL=1 强制全量）")
                if not _inc_names:
                    print(f"增量模式：目标文件与变异体定义均无变更"
                          f"（基线 {_baseline.get('ts')}）—— 无需重跑。")
                    print("MW_FULL=1 可强制全量。")
                    return 0
    print(f"模式：{_mode}\n")

    # ⚠️⚠️ **基线自检**（2026-10-10 加）：跑任何变异体之前，先确认
    # **无变异的沙箱本身是干净的**。
    #
    # 为什么必须有：判据是"相对基线的**增量**"（见 `_base()` 的说明）。
    # 若基线自己就带 ❌（`[数据源]` 的 baidu 探活间歇失败、`[定时任务]`
    # 曾长期自指假红），增量判据本身是对的 —— 但**反向**的坑更隐蔽：
    # 基线脏的时候，某条变异体**真漏网**（`rc=0, ❌数=0`）也会因为
    # 段集合差集非空而被判"抓住"（`[实测]` 2026-10-10：
    # `plan-md-stale-position` 那一轮 `rc=0, ❌数=0`，命中段只有
    # `[检查项可失败性]` —— 那是基线残留，不是它的判别力）。
    # 也就是说**脏基线会让"漏网 0"这条结论本身不可信**。
    #
    # 判据：基线 rc 必须为 0、且 ❌ 行数为 0。不为 0 就**当场中止**并说明
    # 原因（继续跑出来的数字没有意义），而不是照跑然后报一个漂亮但
    # 不可信的"全抓住"。
    # ⚠️ 基线取样要带**重试**（2026-10-10）：基线里有两处
    # **与外部世界相关**的检查项 —— `[WebUI 应答]` 探本机面板、
    # `[数据源]` 探外部行情源。它们在**宿主负载高的一瞬**会超时
    # 而产生一条假 ❌，于是整轮回归（一小时）**当场中止**。
    # `[实测]` 2026-10-10：两个游离探针进程（各 ~95% CPU、燃了 5h40m）
    # 把 load1 拉到 16，webui 8 个端点全部 8s+12s 双次超时 → 基线脏。
    # 重试不能放松**中止阈值**（那会回到“脏基线放行、漏网计成抓住”
    # 的老坑），只是给瞬时抖动几次机会 —— 真脏的基线重试多少次都还是脏。
    _b_bad0 = _b_unv0 = None
    _b_rc0 = None
    for _bi in range(3):
        # 清 `_base()` 的模块级缓存 —— 必须走 globals()，`_BASE = None`
        # 在函数里只是造个局部名，`_base()` 读的仍是模块全局。
        globals()["_BASE"] = None
        # _b_unv0 解包后未使用（[死代码 2026-10-15]），用 _ 占位表明刻意忽略。
        _b_bad0, _b_unv0, _b_rc0 = _base()
        if _b_rc0 == 0 and not _b_bad0:
            break
        if _bi < 2:
            print(f"⚠️  基线第 {_bi + 1} 次不干净（多为宿主负载导致的瞬时超时），"
                  f"15s 后重试（共 3 次）…")
            for _s, _t in sorted(_b_bad0):
                print(f"     {_s} | {_t[:140]}")
            time.sleep(15)
    if _b_rc0 != 0 or _b_bad0:
        print("❌ 沙箱**基线不干净** —— 本次回归的结论不可信，已中止。")
        print(f"   基线 rc={_b_rc0}、❌ 行数={len(_b_bad0)}（已重试 3 次）")
        for _s, _t in sorted(_b_bad0):
            print(f"     {_s} | {_t[:140]}")
        print("   → 基线带 ❌ 时，判据（相对增量）会把「真漏网」也判成「抓住」，"
              "于是「漏网 0」这条结论失去意义。")
        print("   → 先修掉基线的失败项（多半是外部源探活的间歇失败，"
              "或自检自身的假红），再跑回归。")
        return 2

    caught, missed, skipped = [], [], []
    silent = []          # rc≠0 但输出 0 个 ❌ —— "置位了却无法定位"
    cov = {}          # 变异体 -> 它命中的检查段集合

    # ---------- 选片（与并行无关，逻辑不变） ----------
    _jobs = []
    for _mut in MUTANTS:
        _nm0 = _mut[0]
        # ⚠️ MW_ONLY 支持 `|` 分隔多个模式（2026-10-20）：原先单串子串匹配，
        # 写成 `a|b` 时**一条都匹配不上**却照样报「抓住 0/漏网 0/跳过 0」——
        # 看起来像"全过"，实际是零判别力的空跑。拆分后任意一段命中即选中。
        if _only:
            _pats = [x for x in _only.split("|") if x]
            if _pats and not any(_p in _nm0 for _p in _pats):
                continue
        if _inc_names is not None and _nm0 not in _inc_names:
            continue
        _jobs.append(_mut)

    # ---------- must_contain 恒真自检（2026-10-30） ----------
    #
    # ⚠️⚠️ 为什么必须有：`must_contain` 的用途是「报错必须指向**该指的地方**」，
    # 判据是 `must_contain not in out`。若这个字符串**在干净基线里就存在**
    # （最常见的形态是它同时出现在某条 ✅ 行里，例如 `放行`/`港股休市`/
    # `MAX_PUSH`），那么它对任何变异体都恒为真 —— 闸门形同不存在，
    # 变异体可以被**任何**无关的 ❌ 冒认成"抓住"。
    # `[实测]` 2026-10-30：全量扫出 **41 条**这种恒真锚（占全部带锚
    # 变异体的 19%），其中 9 条还是本轮一开始当成"已收紧"的那批。
    # 全部改成"变异体输出里有、基线里没有"的锚之后，判据才真正成立。
    # 教训同 `[检查项可失败性]`：**判据本身可不可失败**必须由元检查盯着，
    # 否则新增一条恒真锚时没有任何信号。
    _bout = globals().get("_BASE_OUT") or ""
    if not _bout:
        # ⚠️ 基线原文没拿到时**不能默认放行** —— 那会让本守卫恒绿，
        # 正是它自己要防的形态（同 `_BASE` 为 None 时 `_judge` 的问题）。
        print("❌ must_contain 恒真自检无法执行：基线原文缺失"
              "（`_BASE_OUT` 为空）—— 守卫本身失效，中止。")
        return 2
    # ⚠⚠ 2026-11-16（P2，Codex 审查指出）：**五元组被漏检了**。
    # 原判据 `len(m) > 5 and m[5]` 只扫六元组的内联 must_contain，
    # 而 105 条五元组的锚在 `_MUST_EXTRA` 里 —— 它们**从未被这道守卫检查过**。
    # 现状这 105 条都已补锚（人工核对），所以不是当下的假绿；
    # 但守卫不覆盖，就意味着"以后新增一条五元组、补了个恒真锚"没有任何信号。
    # 修法：统一用同一个取值函数（与 `_run_one_mutant` 里完全同源），
    # 六元组/五元组一视同仁。
    def _eff_anchor(_m):
        """六元组取内联、五元组取 `_MUST_EXTRA` —— 与判据**同源**，避免两处漂移。"""
        return _m[5] if len(_m) > 5 else _MUST_EXTRA.get(_m[0])

    _vac = [(m[0], _eff_anchor(m)) for m in _jobs
            if _eff_anchor(m) and _eff_anchor(m) in _bout]
    # 另一类同样零判别力的形态：**根本没有 must_contain**（六元组缺项 /
    # 五元组没在 `_MUST_EXTRA` 登记）。它们会退化成"任何 ❌ 都算抓住"。
    # 只告警不中止 —— 例外清单在下面显式列出，新增未登记项会立刻现形。
    _noanc = [m[0] for m in _jobs if not _eff_anchor(m)]
    if _noanc:
        print(f"\n⚠️  {len(_noanc)} 条变异体没有 must_contain（判据会退化成"
              f"「任何 ❌ 都算抓住」）：{', '.join(_noanc[:8])}"
              f"{' …' if len(_noanc) > 8 else ''}")
    if _vac:
        print("\n" + "!" * 56)
        print(f"❌ must_contain 恒真：{len(_vac)} 条变异体的锚在**干净基线"
              f"输出**里就存在 —— 该闸门对它们恒为真、判别力为零。")
        for _n, _c in _vac[:20]:
            print(f"   - {_n}: {_c!r}")
        if len(_vac) > 20:
            print(f"   ……以及另外 {len(_vac) - 20} 条")
        print("   → 换成一个**只在缺陷触发时**才出现的锚（照抄实际首条 ❌ 的"
              "片段；必要时带上 `❌ ` 前缀以区分同文案的 ✅ 行）。")
        print("!" * 56)
        return 2

    # ---------- 并行执行（2026-10-28） ----------
    #
    # ⚠️⚠️ 为什么可以并行：每条变异体的全部工作都发生在**自己的 tempdir
    # 沙箱**里（`make_sandbox`），判据是「相对基线的增量」，而基线与探针
    # 端口夹具都已改成**加锁的单例**（见 `_base` / `_probe_fixture_start`
    # 的 docstring）。共享可写状态**只有**这两处，加锁后并行结果与串行
    # 逐条等价。
    #
    # ⚠️ 代价与实测（16 核 / 8 物理核，本机）：
    #   · 串行单条 ~57s，其中 ~30s 是子进程等待（网络/子进程 sleep），
    #     CPU 大部分时间在等 —— 这正是并行的收益来源；
    #   · 4 路单条 20s；8 路单条 13s；12/16 路单条 8.6s（瓶颈饱和在~103s/批）。
    #     295 条串行 ~4.7h → 6 路约 1h。
    #   · 再往上收益递减（子进程本身就并发，网络与磁盘开始争用）。
    # 默认 3 路（`_DEFAULT_JOBS`）—— **刻意压低**：2026-11-03 实测 8 路把本
    # 机 CPU 打到 97%、load average 229~302（用户当场手动杀进程）。要提速
    # 用 `MW_JOBS=6` 显式指定；`MW_JOBS=1` 退回串行（排查并行假信号时用）。
    # ⚠️ 2026-11-06（全量复核 P1-7 附）：选片为空必须显式失败。
    # `MW_ONLY` 写错（或增量算出「无变更」但 inc 名单为空）时会一条都不选中，
    # 旧行为照样打印「抓住 0 / 漏网 0 / 跳过 0」并退出 0 —— 看起来像"全过"，
    # 实际零判别力。这是本文件反复记的「空跑」形态，必须在结论层拦。
    if not _jobs:
        print("\n❌ 选片为空（0 条变异体）—— 本轮什么都没验证。")
        print(f"   MW_ONLY={_only!r}  inc_names="
              f"{'None（全量）' if _inc_names is None else str(len(_inc_names)) + ' 条'}")
        print("   → MW_ONLY 写错的常见形态是模式串拼错；增量模式则是基线认为无变更。")
        return 2
    # ⚠️ 2026-11-16（P1）：快照已在**增量选片之前**拍好（见那边长注释），
    # 这里不再重拍 —— 重拍会重新引入「选片用一个版本、回写用另一个版本」
    # 的版本混合。`_snap_files` / `_pre_file_hash` / `_pre_mut_hash` 三个名字
    # 在本次运行内**只赋值一次**。
    import concurrent.futures as _cf
    _n_jobs = max(1, _MW_JOBS)
    if len(_jobs) < _n_jobs:
        _n_jobs = max(1, len(_jobs))
    print(f"并行度 {_n_jobs}（MW_JOBS 可调，=1 退回串行）；"
          f"本批 {len(_jobs)} 条 + 1 条空操作对照\n")

    import threading as _th_p
    _emit_lock = _th_p.Lock()

    def _emit(txt):
        # ⚠️ 每行一次性写出：并发下 print 的两段（文本 + 换行）可能被别的
        # 线程插进来，日志变成半行拼接 —— 加锁保证行粒度原子。
        with _emit_lock:
            print(txt, flush=True)

    _results = {}
    _ctrl_res = None          # ⚠️ 必须先初始化：下面的 `_ctrl_ok` 直接读它
    with _cf.ThreadPoolExecutor(max_workers=_n_jobs) as _ex:
        # ⚠️ 空操作对照**先提交**（2026-10-28）：它一字不改源码，**必须**判
        # 「干净」。先提交才能让它与变异体**同批并发** —— 若并行有串扰
        # （两条变异体的沙箱/基线互相看得见），同批的对照最可能先被污染。
        # 后提交的话它多半排在某条变异体跑完后才开跑，反而测不到并发态。
        # 它同时守两件事：
        #   ① 判据没有被污染（历史上出现过：沙箱基线自带 ❌ → 每条都恒判
        #      "抓住"，包括空操作，判别力归零而输出看不出异常）；
        #   ② 并行没有串扰。
        # 它不进 caught/missed，只作为一道闸门 —— 判成「抓住」则本轮结论
        # **不可信**、且**不回写基线**。
        _ctrl_fut = _ex.submit(_run_one_mutant, None, _emit)
        _futs = [_ex.submit(_run_one_mutant, _m, _emit) for _m in _jobs]
        for _f in _cf.as_completed(_futs + [_ctrl_fut]):
            try:
                _r = _f.result()
            except Exception as _e_f:
                _emit(f"  ❌ 变异体执行线程抛出未捕获异常: "
                      f"{type(_e_f).__name__}: {_e_f}")
                continue
            if _r["name"] == _CTRL_NAME:
                _ctrl_res = _r
                continue
            _results[_r["name"]] = _r

    # ---------- 汇总（按 MUTANTS 顺序，保证输出与串行时一致） ----------
    for _mut in _jobs:
        _r = _results.get(_mut[0])
        if _r is None:
            skipped.append((_mut[0], "执行结果丢失（线程异常）"))
            continue
        _st = _r["status"]
        if _st == "skipped":
            skipped.append((_mut[0], _r["reason"]))
        elif _st == "caught":
            caught.append(_mut[0])
            cov[_mut[0]] = _r["hits"]
        else:
            missed.append((_mut[0], _mut[4]))
            cov[_mut[0]] = _r["hits"]
        # ⚠️ 静默失败与 caught/missed **正交**：原实现是无条件
        # `if silent_fail: silent.append(name)`，两种判定下都要记 ——
        # 第一版抽函数时只在 caught 分支里记，漏了 missed 那一半。
        if _r["silent_fail"]:
            silent.append(_mut[0])

    _ctrl_ok = bool(_ctrl_res and _ctrl_res["status"] == "clean")
    if not _ctrl_ok:
        print("\n" + "!" * 56)
        print("❌ 空操作对照未通过 —— 本轮「漏网 0」的结论不可信，且不回写基线。")
        print(f"   {(_ctrl_res or {}).get('line') or '对照未取得结果'}")
        print("   说明判据被污染（沙箱基线自带 ❌）或并行存在串扰。")
        print("   → 先 `MW_JOBS=1` 单跑复核；若串行也报同样结果，查沙箱基线。")
        print("!" * 56)
    print(f"\n抓住 {len(caught)} / 漏网 {len(missed)} / 跳过 {len(skipped)}")
    # ⚠️ 2026-11-06（全量复核 P1-7）：汇总结论必须反映到**进程退出码**。
    # 原先 main() 在脏基线/漏网/跳过时 `return 2` / 返回 None，
    # 而入口是 `main()`（不接收返回值）—— 于是 `python3 selfcheck_mutation.py`
    # 无论结果如何退出码都是 0。任何用 `cmd && …` 或 CI
    # 判退出码的包装都会把「基线脏、回归中止」读成「通过」。
    _rc = 0
    if not _ctrl_ok:
        _rc = 3          # 判据被污染：本轮结论作废
    elif missed or skipped or silent:
        _rc = 1          # 漏网 / 锚点漂移跳过 / 静默失败
    if silent:
        print(f"\n🔇 静默失败（rc≠0 但输出 0 个 ❌ —— 结论行说「见上」却无可 grep）: "
              f"{len(silent)} 个")
        for n in silent:
            print(f"  - {n}")
    if missed:
        print("\n漏网的变异（检查项盲区）:")
        for n, d in missed:
            print(f"  - {n}: {d}")

    # --- 覆盖率对账：哪些检查段从未被任何变异体命中 ---------------------
    # 只在全量跑（含 MW_ONLY？不——MW_ONLY 是子集）时有意义。
    # 2026-10-15 复核：守卫漏 `not _only`，MW_ONLY 调试跑会拿子集 cov
    # 把几乎全部段误报成「零覆盖段」（纯输出污染）。
    if _inc_names is None and not _only:
        all_secs = _all_sections()
        ever = set().union(*cov.values()) if cov else set()
        zero = [s for s in all_secs if s not in ever]
        print(f"\n{'='*56}")
        print(f"检查段覆盖：{len(all_secs)} 段，被变异体命中 {len(all_secs)-len(zero)} 段，"
              f"零覆盖 {len(zero)} 段")
        if zero:
            print("零覆盖段（没有任何变异体能验证它们的判别力）:")
            for s in zero:
                print(f"  - {s}")

    # --- 锚点健康度（2026-11-15 精练新增） -------------------------------
    # 为什么要这一节：**锚点漂移是这个框架唯一的长期致命伤**。
    # 变异体靠逐字匹配源码片段注入缺陷，源码一改锚点就失配，而失配是
    # **静默跳过**（apply_mutant 返回 False → 记「跳过」）。跳过会进
    # `skipped` 让 rc=1，但读者只看到数字、看不到"为什么脆"。
    #
    # 更要紧的是**抗重构能力**：本项目 352 个有效锚点里，
    #   · 177 个（50%）是**跨行锚点** —— 源码只要重排一行、改个缩进就断；
    #   · 12 个超过 200 字符 —— 牵一发动全身；
    #   · 9 个不足 20 字符 —— 反向风险，可能命中别处的相同片段。
    # 跨行锚点是 50%，意味着「想把市场层改成可插拔」时会有约一半锚点
    # 当场失效。这个数必须**每次全量都打出来**，否则重构时无从预估代价。
    #
    # 只统计、不设 ok_all=False：锚点脆不脆是"未来成本"，不是"当前缺陷"。
    # 让它在每次全量的结尾露一次脸，谁要动源码谁就先看到这个数。
    if _inc_names is None and not _only:
        _anch = [e for e in MUTANTS
                 if len(e) > 2 and e[2] and not str(e[2]).startswith("__")]
        _multi = [e[0] for e in _anch if "\n" in e[2]]
        _long = [e[0] for e in _anch if len(e[2]) > 200]
        _short = [e[0] for e in _anch if len(e[2]) < 20]
        print(f"锚点健康度：{len(_anch)} 个有效锚点 · "
              f"跨行 {len(_multi)} 个（也是重构时最先失效的那批）· "
              f">200 字符 {len(_long)} 个 · <20 字符 {len(_short)} 个")
        # 按目标文件排行：要重构某个文件前，先看这行知道得同步重做多少条
        # 锚点。只列**跨行**锚点 —— 单行锚点只要那行字还在就不断，
        # 抗重排；跨行的只要挪一行、改个缩进就失配。
        import collections as _cl_ah
        _by_file = _cl_ah.Counter(e[1] for e in _anch if "\n" in e[2])
        _tot_file = _cl_ah.Counter(e[1] for e in _anch)
        _rows = [(f, n) for f, n in
                 sorted(_by_file.items(), key=lambda kv: -kv[1])
                 if not str(f).startswith("__")]
        if _rows:
            _txt = " · ".join(f"{str(f).replace('.py', '')} {n}/{_tot_file[f]}"
                              for f, n in _rows)
            print(f"  跨行锚点按文件（= 重写该文件要同步的条数）：{_txt}")
        if len(_multi) > len(_anch) // 2:
            print(f"  ⚠️ 跨行锚点已过半（{len(_multi)}/{len(_anch)}）—— "
                  f"改动对应源码前先预期这批会集体「锚点未匹配」")

    # --- 基线回写（2026-10-15 增量模式） ---------------------------------
    # 全绿（无漏网、无锚点跳过）才写 —— 基线永远指向"已验证"的状态；
    # 漏网/跳过的条目留给下次增量复查（missed 名单）。
    # MW_ONLY 调试跑不写（子集结果不是基线）。
    # ⚠️ 2026-10-28：空操作对照未通过时**也不写** —— 那一刻判据本身被污染
    # （基线自带 ❌ 或并行串扰），"漏网 0"只是假象，把它写进基线会让
    # 后续增量跑**永远跳过**这一批（`_mut_def_hash` 与文件哈希都没变），
    # 等于把一次未经证实的结论冻结成"已验证"。
    # ⚠️⚠️ 2026-11-05（P0）：原判据 `_inc_names is not None or …` 让**增量
    # 模式无条件回写** —— 空操作对照脏、甚至漏网非零时也把当前全部文件
    # 哈希与变异体定义哈希冻进基线，下一轮增量算出「无变更 → 无需重跑」，
    # 那批没被验证的变异体从此静默。修法：增量模式只豁免 `not missed`
    # 的要求（漏网名单本就要记进 `missed` 供下轮复查），但 `_ctrl_ok`
    # 与 `not _only` 两道闸对**全量/增量一视同仁**。
    # ⚠️⚠️ 2026-11-16（P0，Codex 审查实测）：**`silent` 必须并入待复查集合**。
    # `silent` = 进程 rc≠0、但输出里一个 ❌ 都没有（结论行说"见上"却无可 grep）
    # 的变异体 —— 那种情况下判据其实**什么都没看到**，只是碰巧 rc 变了。
    # 原实现两条路都没覆盖它：
    #   · 回写条件只判 `missed / skipped` → silent 也允许回写基线
    #   · `missed` 名单只并 `_skip_names` → 下一轮增量**不会重跑**它
    # 合起来的后果：一条判据失效的变异体被冻进"已验证"，且永不复查。
    # 修法：回写要求 `not silent`，并把它并入待复查名单（下轮必重跑）。
    if not _only and _ctrl_ok and not silent and (
            _inc_names is not None or (not missed and not skipped)):
        if _only:
            pass
        else:
            _missed_names = [n for n, _d in missed]
            _skip_names = [n for n, _r in skipped]
            # ⚠️ silent 与 missed/skipped 同等对待：都进待复查名单，下轮必重跑
            _silent_names = [n for n in silent]
            try:
                import json as _jb
                _d = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "files": {}, "mutants": {},
                      "missed": sorted(set(_missed_names) | set(_skip_names)
                                       | set(_silent_names))}
                # 用**跑之前**拍的快照回写（见 `_pre_file_hash` 处的长注释）
                _d["files"] = dict(_pre_file_hash)
                _d["mutants"] = dict(_pre_mut_hash)
                # 跑完之后再比一次：变了就告警。基线里记的仍是**跑之前**
                # 那一版，所以下一轮增量会把这批变异体重跑 —— 不会漏验证。
                _drift = [f for f in _snap_files
                          if _sha_file(_src_of(f)) != _pre_file_hash.get(f)]
                if _drift:
                    print(f"\n⚠️  回归期间源码被改过（{len(_drift)} 个文件）: "
                          f"{', '.join(_drift[:5])}")
                    print("   基线记录的是**开跑前**的哈希 —— 下一轮增量会重跑这批，"
                          "本次结论对应开跑前那一版。")
                os.makedirs(os.path.dirname(_MUT_BASELINE), exist_ok=True)
                _tmp = _MUT_BASELINE + ".tmp"
                # ⚠️ 必须 0600（2026-10-18 实测）：裸 open("w") 在 umask 022
                # 下建出 0644，而 `cache/mut_baseline.json` 属于自检
                # `[状态文件权限]` 的扫描范围 —— 每跑完一次全量回归，下一轮
                # 生产自检就会报 ❌ 并推群（9:14 那条告警就是这么来的）。
                # 用 os.open 一步建文件并定权限，与 selfcheck 告警去重文件
                # 的写法一致（避免 open 后再 chmod 的 0644 窗口）。
                _fdb = os.open(_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(_fdb, "w", encoding="utf-8") as _fbw:
                    _jb.dump(_d, _fbw)
                os.replace(_tmp, _MUT_BASELINE)
                print(f"基线已更新（{_MUT_BASELINE}，"
                      f"{'含 ' + str(len(_d['missed'])) + ' 条待复查' if _d['missed'] else '全绿'}）")
            except Exception as _eb:
                print(f"⚠️  基线写入失败: {_eb}")
    return _rc

if __name__ == "__main__":
    sys.exit(main() or 0)
