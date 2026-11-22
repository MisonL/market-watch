#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器内的调度器 —— 替代 macOS 的 launchd。

## 为什么需要它

本项目的调度原本完全依赖 launchd（5 个 plist，见 `conf/launchd.example.plist`）。
容器里**没有 launchd**（Linux 也没有），于是 `collect` / `analyze` / `selfcheck`
/ `agent` 全都不会被触发，`webui` 也不会常驻 —— 装起来是个死程序。

替代方案有三种：cron、supervisord、自己写循环。**选自己写**，理由：

- **cron**：容器里要额外跑一个 cron 守护进程，且「每 300 秒」与「每天一次」
  要写 crontab，而 crontab 的时区、日志重定向、SIGTERM 转发都得自己接。
  更麻烦的是 cron 无法表达"上一轮还没跑完就跳过这一轮"——
  而 `analyze` 慢轮能跑十几分钟，这正是 launchd 默认就帮我们做对的事。
- **supervisord**：能管常驻进程，但**不擅长定时**（要配合 cron 或自写 program）。
- **自己写**：一个进程、一张 job 表、一个循环。行为可测、可日志、可 DRY。

## 语义对齐 launchd（关键）

launchd 有两条语义**必须复现**，否则会踩到生产里已经踩过的坑：

① **同一 job 不并发**。launchd 对一个 plist 不会同时起两个实例。
   `analyze` 一轮可能跑十几分钟（`[实测]` 慢轮 241s，AI 超时能到 1800s），
   若调度器每 300 秒无条件再起一个，多份 `analyze` 会抢同一把
   `watch.lock`、抢同一份 `state.json`，还会各自发一次推送。
   所以：**上一轮没结束就跳过本轮**，并记一条日志（这本身是有价值的信号 ——
   说明"慢轮吞掉了调度"）。

② **webui 是常驻进程，不是定时任务**。它挂了要**重启**（launchd 的
   `KeepAlive`），但要有**退避**，否则端口被占时会疯狂重启把日志刷满
   （webui.py 自己的注释记着这个坑）。

## 时区

⚠️ 容器默认 **UTC**，而这个程序的一切判断都基于**北京时间**：
`SLOTS`（09:25/10:00/…/15:00）、盘中/盘前判定、`plan.md` 的新鲜度、
日志时间戳。在 UTC 容器里跑，所有时段判断会整体偏 8 小时。

所以启动时必须校验 `TZ`，不对劲就**大声报错并退出**，而不是静默按 UTC 跑
一天。CN 时区用 `Asia/Shanghai`（`CST` 在 macOS 上能用，但在 Linux/glibc 下
"CST" 有歧义，可能是美国中部时间）。
"""
import os
import signal
import subprocess
import sys
import time

# 与 plist 的 StartInterval 一一对应（见 conf/launchd.example.plist）。
# collect 与 analyze 必须同间隔：analyze 读 collect 写的快照，
# 节拍不一致会让"数据滞后一轮"，而这是最难查的一类问题。
JOBS = [
    # (名字, 脚本, 间隔秒, 是否常驻)
    ("collect",   "src/collect.py",   300,   False),
    ("watch",     "src/analyze.py",   300,   False),
    ("agent",     "agent/run.py",     3600,  False),
    ("selfcheck", "src/selfcheck.py", 86400, False),
]
WEBUI = ("webui", "src/webui.py")

# 期望的时区：必须与 A 股交易时段同一时区。
EXPLICIT_TZ = "Asia/Shanghai"

# webui 退避：端口被占之类的**持续性**错误不能每 10 秒重试一次。
WEBUI_BACKOFF_MIN = 5
WEBUI_BACKOFF_MAX = 300

# 项目根目录。**在模块层算**，不在 main() 里算 ——
# 自检有一条判据「函数内引用了未绑定的全局名 → 运行期必抛 NameError」，
# 而 `__file__` 在函数体里对它来说就是"未绑定的全局名"（它只扫顶层符号表）。
# 本文件其它兄弟都写在模块层（`analyze.py` 第 51 行等），保持一致。
# `[实测]` 2026-11-18：写在 main() 里被自检当场抓出。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 单轮最长运行时间（秒）。超了就杀掉，把 job 位置腾出来。
#
# ⚠️ 这是 launchd **没有**的一层，而容器里更需要：
# launchd 下"上一轮没结束"至少还能靠 `launchctl print` 看见；
# 容器里只表现为"这个 job 再也不触发了"，从日志里看不出所以然。
#
# 取值按各自的实际上限：
#   analyze  —— 慢轮实测 241s，AI 超时 1800s（analyze 自己有看门狗）
#   collect  —— 正常 10~30s，取 900s 已极为宽裕
#   agent    —— `agent/run.py` 自己有 1800s 看门狗，这里给 2400s 兜底
#   selfcheck—— 全量约 60~180s，取 1800s
# 刻意**大于**各脚本自己的超时：这层是"总比它自己那份还晚"的最后保险，
# 不该抢在脚本自己的看门狗之前动手（那会打断正在正常收尾的轮次）。
# 调度器 PID 文件（state/ 下，600）。
#
# ⚠️ 为什么需要它：自检要判断"容器里是'没有 launchd 但有人调度'（环境不适用）
# 还是'根本没人调度'（真故障）"。最初用 `pgrep -f src/scheduler.py`，
# 但 `[实测]` 2026-11-18：python:3.12-slim **不含 procps**，`pgrep: not found`
# —— 于是容器里永远判成"没在跑"，本该静默的项变成假红。
# 改用 PID 文件：不依赖任何外部命令，POSIX 通用。
PID_FILE = os.path.join(ROOT, "state", "scheduler.pid")

MAX_RUNTIME = {
    "collect":   900,
    "watch":     3000,
    "agent":     2400,
    "selfcheck": 1800,
}


def _log(msg):
    """带时间戳写 stdout —— 容器里 `docker logs` 就是靠这个看。"""
    print(f"[sched {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def check_timezone():
    """确认容器时区是北京时间；不是就退出。

    为什么 fail-fast：UTC 下程序**能跑**（不会崩），只是所有时段判断静默
    偏 8 小时 —— 盘中不推、半夜推。这种"跑起来了但全错"比启动失败难查得多。
    """
    _tz = os.environ.get("TZ", "")
    try:
        _off = -time.timezone if not time.daylight else -time.altzone
    except Exception:
        _off = 0
    # 北京 = UTC+8 = 28800 秒
    if _off != 8 * 3600:
        _log(f"❌ 时区不是 UTC+8（当前偏移 {_off // 3600} 小时，TZ={_tz or '未设置'}）")
        _log(f"   这个程序的所有时段判断（09:25/10:00/…/15:00、盘中盘前）")
        _log(f"   都基于北京时间。请设置 TZ={EXPLICIT_TZ}")
        _log(f"   例：docker run -e TZ={EXPLICIT_TZ} …")
        sys.exit(2)
    _log(f"时区检查通过（UTC+8，TZ={_tz or '系统默认'})")


def _spawn(name, script, root, env):
    """起一个子进程；stdout/stderr 追加到 logs/<name>.out|err。

    用**追加**而不是覆盖：容器重启后要能接上看之前发生了什么，
    这也与 launchd 的 StandardOutPath 行为一致（launchd 也是追加）。
    """
    _out = os.path.join(root, "logs", f"{name}.out")
    _err = os.path.join(root, "logs", f"{name}.err")
    os.makedirs(os.path.dirname(_out), exist_ok=True)
    # 0600：日志里有行情与配置片段。容器内建文件默认受 umask 影响（常是 644），
    # 与自检的「状态/日志必须 600」判据一致处理。
    _fo = os.open(_out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _fe = os.open(_err, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # 必须赋给具名变量：自检的 [子进程超时] 判据靠"Popen 的接收变量
        # 是否出现过 `.wait(timeout=)`"来判断会不会挂死。写成裸 `return
        # Popen(...)` 时它追踪不到（`[实测]` 2026-11-18 被报 ❌）。
        # 真正的超时处理在 `_kill_group` 里（`proc.wait(timeout=grace)`）。
        proc = subprocess.Popen(
            [sys.executable, "-B", os.path.join(root, script)],
            cwd=root, env=env,
            stdout=_fo, stderr=_fe,
            stdin=subprocess.DEVNULL,
            # 独立进程组：收 SIGTERM 时能整组杀掉（analyze 会起 curl/claude 子进程）
            start_new_session=True,
        )
        return proc
    finally:
        os.close(_fo)
        os.close(_fe)


def _kill_group(proc, name, grace=10):
    """先 SIGTERM 整组，宽限后 SIGKILL。"""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    _log(f"{name} 未在 {grace}s 内退出，SIGKILL")
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _present_jobs(root, jobs=None):
    """把调度表里**本部署根本没有的脚本**摘出去，返回 (保留, 缺失名)。

    ⚠️ 2026-11-22：容器镜像**不含** `agent/`（`.dockerignore` 里有 `agent/`，
    它带着本机 agent 的 state/logs），而 `JOBS` 里固定有 `agent/run.py`。
    原实现是每轮循环都 `os.path.exists` 判一次、缺了就 `⚠️ …不存在，跳过`
    —— 于是容器里**每小时**一条 ⚠️、一天 24 条、永不消失。

    这不是偶发故障，而是「这套部署没有这个脚本」的**静态事实**：静态事实
    应该在启动时说一次（`main()` 里报一次），而不是每 3600s 复读一遍。
    复读的真实代价是**告警疲劳** —— 日志里长期存在一条永远为真的 ⚠️ 之后，
    真的告警也一起被忽略。

    保留运行期的存在性检查（脚本可能在运行中被删/被 git checkout 掉），
    但那边改为「每个名字只报一次」（见 `_missing_logged`）。
    """
    _jobs, _absent = [], []
    for _j in (JOBS if jobs is None else jobs):
        if os.path.exists(os.path.join(root, _j[1])):
            _jobs.append(_j)
        else:
            _absent.append(_j[0])
    return _jobs, _absent


def main():
    root = ROOT
    check_timezone()

    # 子进程一律无缓冲输出：容器日志要走管道，缓冲会让日志"卡住不动"
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    # 写 PID 文件供自检判断"有没有人在调度"（见 PID_FILE 的说明）
    try:
        _fd = os.open(PID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(str(os.getpid()))
    except Exception as _e:
        _log(f"⚠️ PID 文件写入失败（不影响调度）: {_e}")

    _log(f"调度器启动 · 根目录 {root}")
    # 缺脚本的 job 在**启动时**筛一次并报一次（原因见 `_present_jobs` 的说明）。
    # 后续每轮循环只看筛过的表，不再为同一个静态缺失复读 ⚠️。
    _jobs, _absent = _present_jobs(root)
    if _absent:
        _log(f"ℹ️ 本部署没有这些脚本，已从调度表移除：{', '.join(_absent)}"
             f"（容器镜像按 .dockerignore 不含 agent/，属预期）")
    _log("jobs: " + ", ".join(f"{n}/{iv}s" for n, _s, iv, _p in _jobs)
         + f" + {WEBUI[0]}(常驻)")

    procs = {}        # name -> Popen（只在跑着的 job 里有）
    started_at = {}   # name -> 本轮启动时刻（判 MAX_RUNTIME 用）
    next_at = {}      # name -> 下次触发时刻
    _missing_logged = set()   # 运行期才缺的脚本：每个名字只报一次
    _now = time.time()
    for name, _script, interval, _persist in _jobs:
        # 对齐 launchd 的 RunAtLoad：collect/watch/agent 是 false（等一个间隔），
        # selfcheck 是 true（启动就跑一轮）。这里按"间隔"等，selfcheck 首次
        # 立即跑是它自己的语义（86400s 的间隔不该让首轮等一天）。
        next_at[name] = _now if interval >= 86400 else _now + interval

    webui = _spawn(*WEBUI, root, env) if os.path.exists(
        os.path.join(root, WEBUI[1])) else None
    if webui is None:
        _log(f"⚠️ 未找到 {WEBUI[1]}，跳过 webui")
    else:
        _log(f"{WEBUI[0]} 已启动 pid={webui.pid}")
    _webui_backoff = WEBUI_BACKOFF_MIN

    stopping = [False]

    def _on_term(signum, _frame):
        stopping[0] = True
        _log(f"收到信号 {signum}，准备退出")

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    try:
        while not stopping[0]:
            _t = time.time()

            # ---- 定时 job ----
            for name, script, interval, _persist in _jobs:
                # ① 上一轮还在跑 → 跳过（复现 launchd 的"同 job 不并发"）
                _p = procs.get(name)
                if _p is not None:
                    if _p.poll() is None:
                        # 单轮超上限 → 杀掉。这是最后一道保险，正常情况下
                        # 各脚本自己的看门狗会先动手。
                        _lim = MAX_RUNTIME.get(name)
                        _started = started_at.get(name, _t)
                        if _lim and (_t - _started) > _lim:
                            _log(f"{name} 单轮已跑 {int(_t - _started)}s，"
                                 f"超过上限 {_lim}s → 终止")
                            _kill_group(_p, name)
                            procs.pop(name, None)
                            started_at.pop(name, None)
                            next_at[name] = _t + interval
                            continue
                        if _t >= next_at[name]:
                            _log(f"{name} 上一轮未结束（pid={_p.pid}），"
                                 f"本轮跳过 —— 慢轮吞掉了调度")
                            next_at[name] = _t + interval
                        continue
                    # 已结束：记退出码
                    _rc = _p.returncode
                    if _rc not in (0, None):
                        _log(f"{name} 退出 rc={_rc}")
                    procs.pop(name, None)
                    started_at.pop(name, None)

                if _t < next_at[name]:
                    continue

                if not os.path.exists(os.path.join(root, script)):
                    # 启动时已在场的脚本**运行中消失**才算异常（被删/被
                    # checkout 掉），仍要让用户看见 —— 但只报一次：每分钟
                    # 复读同一条只会把日志刷满，问题本身不会因此更清楚。
                    if name not in _missing_logged:
                        _missing_logged.add(name)
                        _log(f"⚠️ {name}: {script} 运行中消失，本轮起跳过"
                             f"（只报这一次）")
                    next_at[name] = _t + interval
                    continue
                try:
                    procs[name] = _spawn(name, script, root, env)
                    started_at[name] = _t
                    _log(f"{name} 启动 pid={procs[name].pid}")
                except Exception as _e:
                    _log(f"❌ {name} 启动失败: {type(_e).__name__}: {_e}")
                next_at[name] = _t + interval

            # ---- webui 常驻 ----
            if webui is not None:
                if webui.poll() is not None:
                    _rc = webui.returncode
                    _log(f"{WEBUI[0]} 退出 rc={_rc}，{_webui_backoff}s 后重启")
                    # 退避：pid 立刻失败通常意味着端口占用/配置错误，这类
                    # 问题不会在 5 秒内自愈，逐次拉长间隔
                    _wake = _t + _webui_backoff
                    while not stopping[0] and time.time() < _wake:
                        time.sleep(0.5)
                    if stopping[0]:
                        break
                    try:
                        webui = _spawn(*WEBUI, root, env)
                        _log(f"{WEBUI[0]} 已重启 pid={webui.pid}")
                        _webui_backoff = WEBUI_BACKOFF_MIN
                    except Exception as _e:
                        _log(f"❌ {WEBUI[0]} 重启失败: {_e}")
                        _webui_backoff = min(WEBUI_BACKOFF_MAX, _webui_backoff * 2)
                else:
                    # 跑够一段时间才算"稳定"，把退避复位
                    _webui_backoff = WEBUI_BACKOFF_MIN

            time.sleep(min(1.0, max(0.05, min(
                (next_at[n] - time.time()) for n in next_at) if next_at else 1.0)))
    finally:
        try:
            os.unlink(PID_FILE)
        except OSError:
            pass
        _log("正在停止所有子进程…")
        for _name, _p in list(procs.items()):
            _kill_group(_p, _name)
        if webui is not None:
            _kill_group(webui, WEBUI[0])
        _log("已退出")


if __name__ == "__main__":
    main()
