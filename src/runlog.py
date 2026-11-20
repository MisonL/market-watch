#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行流水埋点（append-only JSONL）。

存在理由：系统原先**不记录"每一轮是否执行"**。analyze.main() 有 5 条静默
return 路径（非交易日 / 时间窗外 / 抢锁失败 / 无触发 / 配额用尽），全部不写
任何日志。watch.log 1446 行里 [RUN] 标记 0 个 —— 所以"某时刻那一轮跑没跑、
为什么没推"无法从任何现有数据回放，只能靠 launchctl print 的累计计数。

本模块提供一个极小的追加接口，把每轮执行落盘成一行 JSON，供 webui.py 调阅。

设计约束（每条都有原因）：
  · **绝不抛异常**。埋点失败不能拖垮盯盘主链路 —— 整个 write 包在 except 里。
  · **不改 watch.log 格式**。watch.log 由 marketlib._rotate_log 做 2MB 单代
    轮转，混入流水会加速它的轮转、挤掉真正的诊断日志。
  · **O_APPEND 单次 write**。launchd 每 5 分钟起的新进程之间不会写交错。
    ⚠️ 原先这里写的理由是「单条记录 < PIPE_BUF(4096)，POSIX 下单次 append
    写是原子的」——**这个论证是错的，两处都错**：
      · `getconf PIPE_BUF /` 在本机返回 **512**，不是 4096（实测）。
      · 更根本的是，**PIPE_BUF 约束的是管道/FIFO，不是普通文件**。
        这里写的是 `os.open(path, O_CREAT|O_WRONLY|O_APPEND)` 打开的
        **普通文件**，PIPE_BUF 与它无关。
    真正保证不交错的是：O_APPEND 下 write(2) 的写入位置与写入**在同一次
    系统调用内**完成（POSIX 对 O_APPEND 的要求），本机实测也确实如此 ——
    8 进程各写 250 条、单条最大 **20040B**（远超 4096），2000 条**零交错
    零丢失**（2026-09-28，subprocess 并发实测，四种行长 240/2040/5040/
    20040B 全部 ✅）。
    之所以要改这段注释：**错误的安全依据比没有依据更危险** —— 后来者若
    相信"上限是 4096"，就会认为"pushes.jsonl 存推送全文、行长不受控"
    是个必须立刻修的缺陷，或者反过来在别的文件上套用这条不成立的规则。
    实测数据表明当前无需改动；但依据必须写对。
    ⚠️ 注意这**不是**跨文件系统的保证：本结论只在 APFS/本机验证过。
    若 history/ 被放到网络文件系统上，需重新实测。
  · **超限只截尾**。文件超 MAX_BYTES 时从尾部按字节保留
    （MAX_BYTES * KEEP_BYTES_RATIO），避免无界增长；
    这里刻意不做多代轮转 —— 审计流水丢掉就没有了，宁可占点磁盘。
"""
import os
import json
import time
import datetime
from collections.abc import Sequence
from typing import Literal, TypedDict, get_args

# ⚠️ 2026-11-08（复核）：`HIST_DIR` 是**用到**的（RUNS/PUSHES 都基于它），
# 只有 `p_hist` 未使用。清理未用导入时不能整行删：
# 原写 `HIST_DIR = HIST_DIR` 是转手导出的写法，删掉导入就变成
# 名字未定义（实测直接 NameError，全部 10 个模块导不进来）。
from paths import HIST_DIR  # noqa: E402（路径单一真值源）
RUNS = os.path.join(HIST_DIR, "runs.jsonl")
PUSHES = os.path.join(HIST_DIR, "pushes.jsonl")

MAX_BYTES = 4 * 1024 * 1024     # 超过就截尾
# 截尾后保留的行数。必须与 MAX_BYTES **匹配**，且留足余量：
#   · 截尾后要明显低于 MAX_BYTES，否则下一次 _append 会立刻再次触发截尾
#     （每次都要全量 readlines）—— 变成"每次追加都全量重写"，永不收敛
#   · 按每日约 426 行（analyze 双条 138 + collect 单条 288）算 ≈ 47 天
# 原值 2000 行 ≈ 0.25MB ≈ **4.7 天** —— 只用到容量的 5%，与本文件开头
# "审计流水丢掉就没有了，宁可占点磁盘"的设计意图正好相反。
#
# ⚠️ 20000 是按 runs.jsonl（约 126 B/行 → 2.4MB）定的，但**同一个常量
# 也用在 pushes.jsonl 上，而它的行长大 7 倍**（含推送全文，实测 862 B/行）。
# 20000 行 × 862 B ≈ **16.4 MB**，远超 MAX_BYTES=4MB —— 实测：7.41MB 的
# pushes.jsonl 触发截尾后仍是 7.41MB，且此后每次追加都全量重写一次。
# 所以改成**按字节算保留量**：从文件尾部往前累加，累计到 MAX_BYTES 的
# 一个安全比例（60%）就停 —— 这样不管行多长都必然收敛到阈值以下，
# 也不必为两个文件维护两个常量。
KEEP_BYTES_RATIO = 0.6          # 截尾后目标大小 = MAX_BYTES * 该比例

# 所有 status 取值。webui 用它做筛选与配色，也是"为什么没推"的答案词表。
#
# ⚠️ 原先 STATUS 只是个**裸元组**，写成注释约定 —— 没有任何静态检查，
# 打错一个字母（"not_tradng_day"）不会报错，面板只会静静显示成未知状态。
# 现在 Status 是 Literal 联合类型，元组由它推导，两者**不可能不同步**；
# 给写入函数标注 `status: Status` 后，拼错字面量在 basedpyright 下直接报错。
Status = Literal[
    "ok",              # 正常完成（analyze 走完 run，无推送也算 ok 之外的状态）
    "push_ok",         # 产生并成功推送
    "push_fail",       # 尝试推送但失败（含 BLOCKED / 超时 / 缺 webhook）
    "skip_dry",        # 未推送：DRY_RUN 标志生效（人工暂停，**不是故障**）
    "no_trigger",      # 跑了，但没有任何触发 —— 此前 100% 静默
    "no_data",         # 跑了，但行情取不到
    "skip_lock",       # 抢锁失败（上一轮仍在运行）
    "skip_quota",      # 已达当日推送上限
    "out_of_window",   # 不在交易时段内
    "not_trading_day", # 非交易日
    "error",           # 抛异常
    "import_fail",     # 依赖导入失败（如 marketlib 语法错误）——
                       # 进程崩在任何日志代码之前，靠 analyze/collect 顶部的
                       # try 兜底写这条。**不加进这里就没人认得出**：
                       # 面板会显示成未知状态，排查时也会漏看。
    "slot_expired",    # 整点槽位的内容已过失效时刻（SLOT_VALID_UNTIL），
                       # 按设计整条不发 —— 见 analyze._run_after_inflight
                       # 的「内容失效复查」块。
                       # ⚠️ 必须与 push_fail / no_trigger 区分开：它不是
                       # 故障（不该告警），也不是"没触发"（槽位确实认领了，
                       # 只是 AI 跑完时内容已过期）。`[实测]` 2026-10-26
                       # 09:25 槽位：09:19 认领、AI 1312s、09:41 才推，
                       # 而失效时刻是 09:35 —— 正是加这个状态要防的形态。
]

# 元组视图：给 webui 做筛选/配色，以及运行时校验用。
STATUS = get_args(Status)


def is_status(v):
    """运行时校验：v 是否为合法的 status 取值。

    静态标注管不了 JSON 里读出来的数据（runs.jsonl 是外部文件，
    可能被手工编辑或来自旧版本），所以读入口再校验一次。
    """
    return isinstance(v, str) and v in STATUS


def _coerce_status(v, fallback="error"):
    """把任意值收敛成合法 Status —— 非法值记为 fallback 并留痕。

    绝不抛异常：这是审计流水，写不进去比写得难看严重得多。
    """
    if is_status(v):
        return v
    if v is not None:
        _log_illegal_status(v)
    return fallback


def _log_illegal_status(v):
    """非法 status 落一条痕迹（best-effort，绝不抛）。"""
    try:
        rec = {
            "ts": round(time.time(), 3),
            "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "script": "runlog",
            "phase": "end",
            "status": "error",
            "note": f"非法 status 取值，已按 error 记录: {v!r}"[:300],
        }
        _append(RUNS, rec)
    except Exception:
        pass



def _ensure_dir():
    os.makedirs(HIST_DIR, exist_ok=True)


def _mk600(path, flags=None):
    """以 0o600 创建文件并返回 fd —— 本模块**唯一**的建文件入口。

    ⚠️ 不能用 `open(path, "w"/"a")` 新建：权限由 **umask** 决定，launchd 下
    022 → **644**。这里服务三类文件，都不能是 644：
      · 截尾用的 `<流水>.tmp` —— 写完 `os.replace` 成 `runs.jsonl` /
        `pushes.jsonl`，**替换会把临时文件的权限一并带过去**，于是审计流水
        在**第一次触发 4MB 截尾时变成 644**（里面有推送全文）；
      · `.lockfail` / `.truncfail` 标记文件 —— 含失败日期与文件路径；
      · 锁文件 `*.lock`（见 `_lock`）。
    `[实测]` 2026-10-09 权限审计：`open(..., "w")` 新建在 umask 022 下得 0644
    （已用 A/B 实测确认），而 `_open600` 型入口得 0600。
    自检的 [状态文件权限] 段用**行为断言**钉住：建一个探针文件，
    `stat.S_IMODE & 0o077` 必须为 0 —— 源码正则类判据最弱，不采用。
    """
    if flags is None:
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    return os.open(path, flags | os.O_CREAT, 0o600)


def _write600(path, text):
    """以 0o600 创建/覆盖写一个小文本文件（见 _mk600 的说明）。"""
    with os.fdopen(_mk600(path), "w", encoding="utf-8") as f:
        f.write(text)


def _lock(path, exclusive, timeout=3.0):
    """对 <path>.lock 加 flock。返回 fd（调用方释放），失败返回 None。

    · 追加走**共享锁**（LOCK_SH）：多进程可同时追加，O_APPEND 保证单行原子
    · 截尾走**排他锁**（LOCK_EX）：必须独占，否则读-改-写窗口内的新行会被覆盖

    用 flock 而非 O_EXCL 锁文件：flock 由内核在进程退出时自动释放，
    进程被 kill 也不会留下死锁；O_EXCL 锁文件会残留，需要额外的陈旧清理。
    有界重试后放弃 —— 宁可文件大一点，也不要卡住主链路。
    timeout=3.0（2026-10-08 审查，原 0.5）：共享锁只与截尾的排他锁互斥，
    截尾是「读全文件→写 tmp→replace」，pushes.jsonl 的 4MB 全量重写在
    磁盘繁忙时可超过 0.5s —— 超时后 _append 落进「无锁写入」分支，与截尾
    窗口重叠即丢一条审计记录。3s 覆盖最坏截尾耗时；仍超时才无锁写
    （宁可冒险写，不可丢埋点），且写入点已有 _daily_log_once 可观察。
    """
    try:
        import fcntl
        fd = _mk600(path + ".lock", os.O_CREAT | os.O_RDWR)
    except Exception:
        return None
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                try: os.close(fd)
                except Exception: pass
                return None
            time.sleep(0.01)


def _unlock(fd):
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try: os.close(fd)
    except Exception: pass


def _truncate_locked(path):
    """截尾的**实际逻辑**。调用方必须已持 <path>.lock 的排他锁。"""
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            return
    except OSError:
        return
    try:
        # ⚠️ errors="replace"（2026-10-08 审查）：严格模式下一个非法 UTF-8
        # 字节（来源正是 _append 注释所述：截尾 os.replace 期间被 kill、
        # 磁盘写满只写半截多字节字符）就让 readlines 抛 UnicodeDecodeError，
        # 被下面 except 吞掉 -> **此后每次追加都重复"读全文件→失败"，
        # 截尾一次也不会发生**，流水无界增长（实测：带 0xff 的文件 4 次
        # 追加后只增不减；去掉坏字节即恢复正常截尾）。读侧 read() 早就用
        # errors="replace" 防同一来源，写侧漏了。
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        # ⚠️ 按**字节**从尾部累加，而不是固定行数。
        # 原先那个按行数的上限（KEEP_LINES=20000）是按 runs.jsonl 的行长
        # 校准的，用在行长大 7 倍的
        # pushes.jsonl（含推送全文）上会保留 16MB —— 超过 MAX_BYTES，
        # 于是截尾后文件仍超限，下次追加再截，永远收敛不了。
        # 按字节算则与行长无关，两个文件都必然落到阈值以下。
        _budget = int(MAX_BYTES * KEEP_BYTES_RATIO)
        keep = []
        _used = 0
        for _ln in reversed(lines):
            _n = len(_ln.encode("utf-8"))
            if _used + _n > _budget and keep:
                break
            keep.append(_ln)
            _used += _n
        keep.reverse()
        # 兜底：连一行都放不下时至少留最后一行，别把文件截成空的
        if not keep and lines:
            keep = lines[-1:]
        tmp = path + ".tmp"
        # ⚠️ 权限必须在这里定：os.replace(tmp, path) 会把 tmp 的 mode
        # 一起带成流水文件的 mode（见 _mk600 的说明）。
        with os.fdopen(_mk600(tmp), "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except Exception as e:
        # 不再静默：截尾失效 = 流水无界增长，必须留痕（按文件去重，避免刷屏）
        try:
            _daily = time.strftime("%Y-%m-%d")
            _warn = path + ".truncfail"
            _last = ""
            try:
                with open(_warn, encoding="utf-8") as _wf:
                    _last = _wf.read().strip()
            except Exception:
                pass
            if _last != _daily:
                _write600(_warn, _daily)
                # ⚠️ 必须 `import sys as _sys`（2026-10-11 审查）：本文件
                # **没有** import sys，裸写 `sys.stderr` 必然 NameError ——
                # 而它落在下面那个 `except Exception: pass` 里，于是整条
                # 告警**静默消失**：截尾失败时既不写 stderr 也不留痕，
                # 流水无界增长而无人知晓。同文件 287 行的锁超时告警就是
                # 正确写法（`import sys as _sys`），这里是漏改的另一半。
                import sys as _sys
                print(f"[runlog] {path} 截尾失败（{type(e).__name__}: {e}），"
                      f"流水将持续增长", file=_sys.stderr, flush=True)
        except Exception:
            pass


def _append(path, rec):
    """单次 O_APPEND 写一行。任何异常都吞掉。

    追加持**共享锁**，与截尾的排他锁互斥 —— 这样截尾的读-改-写窗口内
    不会有新行写进来（那正是丢数据的场景）。
    """
    try:
        _ensure_dir()
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        lk = _lock(path, exclusive=False)
        if lk is None:
            # 锁超时（3s，见 _lock）——截尾正持排他锁或锁文件异常。
            # 仍**必须写**：埋点丢一条就少一轮审计记录（追加本身在
            # O_APPEND 下单行原子，与截尾重叠最坏丢这一条）；不写则
            # 「那一轮到底跑没跑」又回到不可回放。丢弃是要防的，
            # 两害相权取其轻写下去，且留痕（按日去重）。
            try:
                import sys as _sys
                _d = time.strftime("%Y-%m-%d")
                _w = path + ".lockfail"
                _last = ""
                try:
                    with open(_w, encoding="utf-8") as _wf:
                        _last = _wf.read().strip()
                except Exception:
                    pass
                if _last != _d:
                    _write600(_w, _d)
                    print(f"[runlog] {path} 共享锁 3s 超时，无锁追加"
                          f"（与截尾重叠可能丢本条）", file=_sys.stderr, flush=True)
            except Exception:
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                # ⚠️ 上一行若是**半截行**（无结尾换行），必须先补一个换行。
                # 否则本次 O_APPEND 会与它**粘成一行**：`{...torn...{"ts":...}`
                # 这一行 json.loads 必然失败，于是本函数刚写下的这条**完整、
                # 合法、已成功落盘**的记录，对所有读者永久不可见 ——
                # 而且之后每一条新记录都会被粘上去，直到下次截尾。
                # 实测（2026-09-27）：
                #   写 '{"n":1}\n{"n":2}\n{"n":3,"torn":"ab'（无 \n）
                #   _append({"n":4}) 后文件 = '...{"n":3,"torn":"ab{"n":4}\n'
                #   read() -> [{'n':1},{'n':2}]  ← n=4 消失
                # 半截行的来源：_truncate_locked 的 os.replace 期间被 kill
                # （writelines 与 replace 之间不是原子的），或磁盘写满只写了
                # 一半。生产当前 4666 行全部可解析（实测 0 坏行），
                # 但这是一次断电/一次 OOM 就能触发的事，代价却是**之后
                # 所有流水不可见**，所以在此加固。
                try:
                    _sz = os.lseek(fd, 0, os.SEEK_END)
                    if _sz:
                        with open(path, "rb") as _f:
                            _f.seek(_sz - 1)
                            if _f.read(1) != b"\n":
                                os.write(fd, b"\n")
                except Exception:
                    pass
                try:
                    # ⚠️ 2026-10-16 复核（P0）：正文含**孤立代理码点**（Cs，
                    # 如 `\ud800`）时，`line.encode("utf-8")` 抛
                    # UnicodeEncodeError → 落入外层 except → 记「埋点写失败」
                    # 并返回 False → **整条流水丢失**，而这条记录本该照常落盘
                    # （代理码点只是编码不了，流水内容有效）。
                    # 后果：pushes.jsonl 少一条该轮记录，面板与
                    # 【推送计数口径】等对账项全部对不上。
                    # ⚠️⚠️ 2026-10-27 修：`backslashreplace` 把 `\ud800`
                    # 写成 **6 个 ASCII 字符** `\ud800` —— 而这 6 个字符在
                    # JSON 字符串字面量里恰好是**合法的转义序列**，于是
                    # `json.loads` 读回时**又还原成真代理码点**。下游
                    # `webui._send` 的 `body.encode("utf-8")` 再抛
                    # UnicodeEncodeError，被兜成 `b"{}"` —— **HTTP 200 +
                    # 空 JSON**，该条推送在面板上永久不可见，且零告警
                    # （`[实测]` 端到端复现：落盘→json.loads→dumps→encode
                    # 四步，最后一步失败）。
                    # 改为**替换成 U+FFFD**（与任何文本编辑器/浏览器显示
                    # 一致）：内容语义不变、行仍是合法 JSON、读回是普通字符，
                    # 下游任何 UTF-8 编码都不再失败。丢失的只是那个**本来
                    # 就无法表示的码点**，不是整条记录。
                    try:
                        _lb = line.encode("utf-8")
                    except UnicodeEncodeError:
                        _line2 = line.encode("utf-8", "replace").decode(
                            "utf-8", "replace")
                        _lb = _line2.encode("utf-8")
                    # ⚠⚠ 2026-11-10（Codex P2）：`os.write` 可能**短写**（返回字节数 <
                    # 请求数）且**不抛异常** —— 原写法丢弃返回值，
                    # 于是半截的 JSON 落盘后仍返回 True、不写 .appendfail标记。
                    # 实测：注入半写后文件留下 120 字节残行，
                    # **0 条可解析记录**，而 `_append` 返回 True。
                    # 循环写到写完为止；真写不完就抛，交给外层留痕。
                    _mv = memoryview(_lb)
                    _wtot = 0
                    while _wtot < len(_lb):
                        _n = os.write(fd, _mv[_wtot:])
                        if not _n:
                            raise OSError(
                                f"写入短缺（{_wtot}/{len(_lb)} 字节）")
                        _wtot += _n
                except UnicodeEncodeError:      # 理论上到不了（上面已兜）
                    pass
            finally:
                os.close(fd)
            # 截尾需排他锁。不能在同一 fd 上把 LOCK_SH 升级为 LOCK_EX
            # （POSIX 下行为不确定），故先释放再重新获取。
            _unlock(lk); lk = None
            lk2 = _lock(path, exclusive=True)
            # ⚠️ 文件大一点无所谓（下次写入再截），丢流水不可接受 ——
            # 所以拿不到排他锁就**跳过本轮截尾**，不能无锁硬调
            # `_truncate_locked`：它的契约是「调用方必须已持排他锁」，
            # 无锁调用会让 read→writelines→os.replace 三步暴露在并发写入
            # 之下：窗口内别的进程 O_APPEND 进来的行会被整段覆盖掉。
            # 实测：强制排他锁返回 None 后，50 行的文件被截到 3 行，
            # 且期间追加的记录丢失 —— 审计流水丢了就找不回来。
            try:
                if lk2 is not None:
                    _truncate_locked(path)
            finally:
                _unlock(lk2)
        finally:
            _unlock(lk)
        return True
    except Exception as _e_w:
        # ⚠️ 写失败**必须留痕**（2026-10-12 审查 P2-36）：原先是裸
        # `except Exception: return False` —— 磁盘满 / 权限变化 / 目录被删时
        # 流水**静默丢失**，而面板、自检、"那一轮到底跑没跑"的全部证据都
        # 在这份文件里。本函数另有两种**同型**留痕（`.lockfail` 锁超时 /
        # `.truncfail` 截尾失败，都按日去重 + stderr），唯独最要紧的
        # "写失败"（数据真的没了）反而无声，不一致。
        # ⚠️ 留痕本身**必须与失败解耦**：`_write600` 在"目录都不存在"时同样
        # 会抛，若整个留痕块再被 `except: pass` 包住，就退化成"什么都没留"
        # （实测：把 path 指向不存在的子目录 -> 返回 False 且零痕迹）。
        # 故 stderr 那一步单独兜，保证**至少有一条 stderr**。
        try:
            import sys as _sys_w
            print(f"[runlog] {path} 埋点写失败"
                  f"（{type(_e_w).__name__}: {_e_w}）—— 该轮流水未落盘",
                  file=_sys_w.stderr, flush=True)
        except Exception:
            pass
        try:
            _d_w = time.strftime("%Y-%m-%d")
            _w_w = path + ".appendfail"
            _last_w = ""
            try:
                with open(_w_w, encoding="utf-8") as _wf_w:
                    _last_w = _wf_w.read().strip()
            except Exception:
                pass
            if _last_w != _d_w:
                _write600(_w_w, _d_w)
        except Exception:
            pass
        return False


class RunRecord(TypedDict, total=False):
    """一行 runs.jsonl。ts/t/script/phase/status/pid 必有，其余按调用参数写。
    events 是 list[str]（事件键），写成裸 dict 会让 list(events) 赋给
    float|str|int 被报 reportArgumentType（实测 1 条）。
    """
    ts: float
    t: str
    script: str
    phase: str
    status: str
    pid: int
    dur_ms: int
    slot: str
    kind: str
    events: list[str]
    note: str
    starved_slots: int
    data_ms: int
    ai_ms: int
    gap_s: int


def _last_ts(exclude_pid=None):
    """读 runs.jsonl 中**上一条（可排除本进程）**记录的 ts；读不出返回 None。

    ⚠️ 为什么是末行而不是"上一行"：`run()` 是唯一的写入者，本函数在
    `_append` **之前**调用，所以此刻的末行就是"上一条记录"。
    ⚠️ 必须容忍并发：两个进程可能同时读、同时写（collect 与 analyze 各自
    独立跑，300s 周期大概率错开但会撞）。这里只是**读**一个近似值，
    撞了最多把 gap 算大一点点，不影响判据方向。
    ⚠️ 只读尾部 8KB 而不是整个文件：runs.jsonl 可达 4MB，每条记录都要
    全量读一遍会让每轮多几十毫秒 —— 那是白白付给 288 次/天的空转轮次。

    ⚠️ `exclude_pid`（2026-10-25 审查修）：**必须**能跳过本进程刚写的
    start 记录。原实现无条件取末行，而 analyze 是「先写 start 再写 end」，
    于是 end 的 `gap_s` 读到的"上一条"就是**本轮自己的 start** →
    gap_s 恒等于本轮耗时。`[实测]` 生产 8575 条 analyze end 记录里
    98.8% 的 gap_s ≈ dur_ms/1000、众数为 0；而同文件 collect（不写 start）
    的 gap_s 中位数 229s 正常。后果：自检的「流水断档」判据吃 gap_s，
    对 analyze 侧**永久失明** —— 正是 gap_s 当初要解决的"launchd 没触发"
    那类盲区，在 analyze 上完全没被覆盖。
    """
    try:
        sz = os.path.getsize(RUNS)
        if sz <= 0:
            return None
        with open(RUNS, "rb") as f:
            f.seek(max(0, sz - 8192))
            tail = f.read().decode("utf-8", "replace")
        for ln in reversed(tail.splitlines()):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue          # 尾部可能是被截断的半行
            # 跳过本进程写的记录（本轮 start）—— 否则 gap_s 会算成
            # 「start 到 end 的时长」，见 docstring 的实测数据。
            if exclude_pid is not None and d.get("pid") == exclude_pid:
                continue
            v = d.get("ts")
            return float(v) if isinstance(v, (int, float)) else None
        return None
    except Exception:
        return None


def run(script: str, status: Status, phase: str = "end", t0=None,
        dur_ms=None, slot: str | None = None, kind: str | None = None,
        events: Sequence[str] | None = None, note: str | None = None,
        starved: int | None = None,
        data_ms: int | None = None, ai_ms: int | None = None,
        ai_fallback: bool | None = None,
        struct_partial: bool | None = None,
        ai_stall: str | None = None):
    # 每一轮都落一次心跳（不分 phase）—— 它只回答“这个任务最近一次真正
    # 跑起来是什么时候”。主链路**停摆**是唯一没有任何信号的故障形态
    # （见 BEAT_FILE 处的 11-08 事故）。放在这里是因为 analyze/collect 都必经过它。
    beat(script)

    """写一条执行流水。

    ⚠️ slot/kind/note 允许 None，而不是 str：调用点写的是
    `_st.get("slot")` / `_st.get("kind")`，键缺失时就是 None（analyze 走
    push() 的默认参数 slot=None 时也是 None）。函数体里 `if slot:` 与
    `slot or ""` 正是为它写的 —— 标注成 str 等于在说谎，且会把
    "调用点传了 None" 这个真实情况从静态检查里藏掉。

    script: "analyze" / "collect" / "webui"
    status: Status 里的一个值（Literal 类型，拼错会被静态检查拦下）
    phase:  "start" / "end"
    t0:     time.time() 起点；给了就自动算 dur_ms

    ⚠️ status 既是静态 Literal，进来时**再运行时校验一次**：
    analyze/collect 里有些 status 来自被调用方的返回值（如 `_st["status"]=r`），
    静态标注管不到那条路径。非法值收敛成 "error" 并留痕，绝不抛异常。
    """
    status = _coerce_status(status, "error")
    now = time.time()
    if dur_ms is None and t0 is not None:
        try:
            dur_ms = int((now - t0) * 1000)
        except Exception:
            dur_ms = None
    rec: RunRecord = {
        "ts": round(now, 3),
        "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": script,
        "phase": phase,
        "status": status,
        "pid": os.getpid(),
    }
    if dur_ms is not None:
        rec["dur_ms"] = dur_ms
    if slot:
        rec["slot"] = slot
    if kind:
        rec["kind"] = kind
    if events:
        rec["events"] = list(events)
    if note:
        rec["note"] = str(note)[:300]
    # `starved`：本轮耗时超过调度间隔时，期间被 launchd 静默丢弃的触发次数
    # （analyze 侧算出，见其 finally）。被丢的轮次自己没有流水记录，
    # 只有这个字段能让面板回答「今天丢了几轮」。0 不写（省体积、语义即无）。
    if starved:
        try:
            rec["starved_slots"] = int(starved)
        except (TypeError, ValueError):
            pass
    # `data_ms` / `ai_ms`：慢轮的**耗时归因**（2026-10-21）。
    # `dur_ms` 只说明"这轮很慢"，却不说是取数慢还是 AI 慢 —— 两者处置
    # 完全不同（前者查网络/配额，后者查网关/模型）。`[实测]` 2026-10-21
    # 11:08 那轮 563s 而 AI 一次都没调用（CLI 缺失 6s 就返回了），
    # 557s 全在取数上 —— 这一点是靠翻 runs.jsonl 的时间戳逐条反推出来的，
    # watch.log 里一个字都没有。
    # 缺省不写：只在 analyze 的 end 记录里给（collect 没有 AI 阶段）。
    for _k, _v in (("data_ms", data_ms), ("ai_ms", ai_ms)):
        if _v is None:
            continue
        try:
            rec[_k] = int(_v)
        except (TypeError, ValueError):
            pass
    # `ai_fallback`：本轮推送文本是**模板兜底**（AI 零产出/不合规）而非
    # AI 产出（2026-11-03 加，P1 观测缺口）。
    #
    # ⚠️ 存在的理由：AI **成功时一行日志都不打**，只有失败留痕。于是
    # 「今天 AI 挂了 3/7」这件事在日志里只能看到 3 条失败，**看不到分母**，
    # 自检也没有任何"当日兜底率"的结果性判据 —— 读者收到的是模板文案，
    # 系统却说一切正常。`[实测]` 2026-11-03：7 轮真调 AI 的轮次里 3 轮
    # 零产出退模板（09:24/09:44/10:45），与 4 轮成功（35~65s）混在一起
    # 无从区分。落这个字段后，`[投递结果]` 段的 `_ai_fallback_verdict`
    # 能按 (兜底轮数 / 真调 AI 轮数) 报警。
    # 只在 True 时写（False/None 都省略）—— 与 starved 同理：省体积，
    # 且"没这个键"就等于"非兜底"，语义即无。
    if ai_fallback:
        rec["ai_fallback"] = True
    # `struct_partial`：本轮推送走的是「**回退首版但结构仍缺段**」的稿子（F8 缺口②）。
    #
    # 存在的理由：重写空产出时，代码会回退首版「因为它比兜底模板有信息量」
    # —— 这是**刻意的降级取舍**，不是缺陷。但它有一个隐形代价：
    # 读者看到的是一条缺了必带段的消息，而系统和面板都显示「已完成」。
    # 只有日志里一行「回退首版（结构仍缺：…）」，无法统计发生率。
    # 落字段后，`[投递结果]` 能像 `ai_fallback` 一样报「多少条推送带结构缺陷」。
    # 不改读者看到的内容 —— 只加可观测性。
    # 只在 True 时写（与 starved/ai_fallback 同理）：省体积，且“没这个键”就是“结构完整”。
    if struct_partial:
        rec["struct_partial"] = True
    # `ai_stall`：本轮 AI 被终止的原因 token（2026-11-20 加，P1 归因缺口）。
    #
    # ⚠️ 存在的理由：`ai_fallback` 只说"退了模板"，**不说为什么**。而
    # 「我方预算掐的」与「通道挂了」的处置方向相反 —— 前者该调预算，
    # 后者该查网关。`[实测]` 2026-11-19 当日 5 轮兜底里 4 轮是我方预算
    # 掐的（watch.log 自己写着「不是通道故障」），但流水里全是
    # `ai_fallback=True`，`_ai_fallback_verdict` 只能合并成
    # 「AI 通道实质不可用 —— 查 docker logs new-api」，把排查方向引错。
    # token 取值与判据见 `analyze._stall_kind`（单一真值，此处不复制）。
    # 只在非 None 时写（与 starved/ai_fallback/struct_partial 同理）。
    if ai_stall:
        rec["ai_stall"] = str(ai_stall)[:20]
    # `gap_s`：距**上一条流水**的秒数（2026-10-21）。
    #
    # ⚠️ 存在的理由：系统此前**无法区分**「launchd 没触发」与「触发了但
    # 进程没跑起来」—— 两者的痕迹都是"文件里什么都没有"。
    # `[实测]` 2026-10-21 13:27–15:04：97 分钟里只留下 3 条记录，
    # 按 300s 间隔本该有 19 轮；那些轮次卡在 launchd 的 `xpcproxy`
    # 阶段（Python 根本没启动），一个字节都没写。而同一时段
    # `launchctl print` 的 runs 计数照常递增 —— 唯一的证据就是**间隔**。
    #
    # 为什么不用「补 start 记录」：start 是 Python 启动后才写的，而
    # 卡在 xpcproxy 时 Python 压根没起来 —— 补了也照样是空白。
    # 间隔是**进程外部**的客观事实，不依赖进程自己争气。
    #
    # 只在 phase=="end" 时算：start 紧接着上一轮 end，间隔恒为几十秒，
    # 没有信息量（analyze 每轮两条，掺进来会把 300s 的间隔减半）。
    if phase == "end":
        # 排除本进程自己的记录（本轮 start）——理由见 _last_ts 的 docstring。
        _prev = _last_ts(exclude_pid=os.getpid())
        if _prev is not None:
            try:
                _g = int(now - _prev)
                # 负值（时钟回拨/NTP 校正）与超大值（隔夜）都无意义：
                # 前者是脏数据，后者由自检按"交易时段内"过滤，不需在此截断。
                if _g >= 0:
                    rec["gap_s"] = _g
            except (TypeError, ValueError):
                pass
    return _append(RUNS, rec)


def push(slot: str | None, kind: str | None, text: str, ok: bool,
         resp: str = "", note=None):
    """写一条推送全文记录。

    ⚠️ **两类调用点的时序不同，这是刻意的**（2026-10-28 澄清）：
      · **没推出去**的路径（DRY / BLOCKED / 缺 webhook / curl 超时）——
        在 `subprocess.run` **之前**写，因为那四种情况下 curl 根本不会
        执行、也没有 `ok` 可判。
      · **正常推送**路径 —— 在 curl **之后**写，因为 `ok` 取决于企微的
        响应（`errcode == 0`），写在之前就无从得知成败。
    原 docstring 写「调用点必须在推送**之前**」，与 analyze.push 的实际
    实现（第 762 行在 `subprocess.run` 之后）矛盾，且 webui 的交叉校验
    注释引用了这条不成立的断言。此处按实际契约重写。
    `text` 必须是**最终上线的那份文本**，所以要在 _safe/_strip_fh/字节
    截断全部处理完之后再取。
    """
    rec = {
        "ts": round(time.time(), 3),
        "t": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "slot": slot or "",
        "kind": kind or "push",
        # 用 bool() 而非 `ok`：调用点传进来的可能是任意真值（如 None/0/""），
        # 面板用 `p.get("ok")` 做真值判断，落盘时统一成真正的布尔，
        # 免得 JSON 里出现 "ok": null 这种既不是 true 也不是 false 的态。
        "ok": bool(ok),
        "text": text or "",
    }
    if resp:
        rec["resp"] = str(resp)[:200]
    if note:
        rec["note"] = str(note)[:300]
    return _append(PUSHES, rec)


def read(path=None, limit=None):
    """读流水（供 webui 用）。坏行跳过，不抛。"""
    path = path or RUNS
    out = []
    # ⚠️ 必须带 errors="replace"。默认的严格模式在**半截多字节字符**上抛
    # UnicodeDecodeError，被下面那个 `except Exception: return []` 吞成
    # **空列表** —— 一条坏字节就让整个流水/整个面板变空白，而不是只跳过
    # 那一行。半截字符的来源同 _append 的注释（截尾被 kill、磁盘写满）。
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if limit:
        out = out[-limit:]
    return out

# ---------- 存活心跳（2026-11-10）----------
# ⚠⚠ 存在理由：主链路**停摆**是唯一没有任何信号的故障形态。
# `[事故]` 2026-11-08 20:23:47~21:08:49 一次 import 清理写坏了 analyze，
# `依赖导入失败，本轮未执行` 连续打了 **45 分钟** ——
# 零推送、零通知，是人工翻日志才发现的。
# 现有防线都覆盖不到它：
#   · 推送   —— 挂了就不推，正是没信号的地方
#   · 自检   —— StartInterval=86400 + RunAtLoad，一天一次且跑在开机时刻
#   · 面板   —— 要人主动去看
# 每轮写一个时间戳，不带任何业务内容 —— 它只回答一个问题：
# "这个任务最近一次真正跑起来是什么时候"。
BEAT_FILE = os.path.join(HIST_DIR, "beat.json")


def beat(job):
    """记一次活着的证据。任何异常都吞掉 —— 心跳不能反过来影响主链路。

    ⚠⚠ 2026-11-11（审查，P2）：**读改写必须跨进程互斥**。
    原写法是裸的 read → modify → replace，且临时文件名固定为 `beat.json.tmp`。
    analyze 与 collect 是**两个独立进程**、各自每 300s 一轮，两个真实后果：
      ① 丢更新：collect 读到旧的 analyze 心跳 → analyze 写入新心跳 →
         collect 把自己那份旧值写回去 → analyze 心跳被逆回。
         两个任务刚跑完，却判定 analyze 已停摆 1800s，可触发误报。
      ② 共用同一个 `.tmp`：A 写到一半、B 截断同一文件，内容互相污染。
    改法：锁内重读并合并；`mkstemp` 取**唯一**临时文件。锁失败时退回无锁写（心跳不能因锁而丢）。
    """
    try:
        d = {}
        _lk = None
        try:
            import fcntl
            os.makedirs(os.path.dirname(BEAT_FILE), exist_ok=True)
            # ⚠ 2026-11-11：必须显式 0o600。`open(..., "a+")` 按 umask 创建
            # （launchd 下 umask 022 → 644），自检的「状态/日志/锁文件权限」
            # 项当场抓住了 —— 与上方 beat.json 同一条历史教训，我又犯了一遍。
            _lk_fd = os.open(BEAT_FILE + ".lock",
                             os.O_CREAT | os.O_RDWR, 0o600)
            _lk = os.fdopen(_lk_fd, "a+")
            fcntl.flock(_lk.fileno(), fcntl.LOCK_EX)
        except Exception:
            _lk = None
        try:
            with open(BEAT_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d[str(job)] = time.time()
        try:
            os.makedirs(os.path.dirname(BEAT_FILE), exist_ok=True)
        except Exception:
            pass
        # ⚠ 权限必须显式 0o600：裸 `open(..., "w")` 按 **umask** 创建（launchd 下
        # umask 022 → 644）。`state/` 是 700 才没泄漏，但那是纵深防御的最后一层 ——
        # 同目录下 `_open_lockfile` / runlog 写入都用 0o600。
        # `[实测]` 上一版写出 `beat.json 0o644`，自检的
        # 「状态/日志/锁文件权限」项直接抓住了它。
        # ⚠ 唯一临时文件：固定名 `.tmp` 在多进程下会互相截断。
        import tempfile as _tf
        fd, tmp = _tf.mkstemp(prefix=".beat.", suffix=".tmp",
                              dir=os.path.dirname(BEAT_FILE))
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, BEAT_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass
    finally:
        try:
            if _lk is not None:
                _lk.close()          # 关闭即释放 flock
        except Exception:
            pass


def is_stale(job, max_age_s):
    """该任务是否**停摆**（超过 max_age_s 没有心跳，或从未跳）。

    从未跳也算停摆 —— "从来没跑起来" 与 "跑着跑着停了"对读者是同一件事。
    """
    try:
        with open(BEAT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        ts = d.get(str(job)) if isinstance(d, dict) else None
        if not isinstance(ts, (int, float)):
            return True
        return (time.time() - float(ts)) > max_age_s
    except Exception:
        return True
