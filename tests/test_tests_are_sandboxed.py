#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试进程不得写穿生产日志 —— 单点判定必须有，且必须真的生效。

## 起因

`tests/run_all.sh` 会给每个用例建沙箱 `MW_DIR`，全量跑与 CI 都干净；
但**直接跑单个用例**（开发时最常用）走的是生产根 —— 而这些用例调的函数
会 `_log(...)`，`_log` 写的是 `MW_DIR/logs/watch.log`。

`[实测]` 单跑一次写入生产 watch.log 的行数：

| 用例 | 行数 |
|---|---|
| `test_split_wall_hang.py` | **751** |
| `test_wrap_no_unit_split.py` | 6 |
| `test_empty_trigger_section.py` | 6 |
| `test_firecrawl_gateway_cooldown.py` | 2 |
| `test_sector_round_share.py` | 1 |

## 为什么不能靠"每个用例自己设 MW_DIR"

我先试过这条路：静态扫描 80 个用例，要求每个都自带 `MW_DIR`。
结果 **52 个不合格** —— 而实测其中只有 5 个真的会写。
要求它们全部改，是为了 5 个真实问题改 52 个文件，而且新写的用例
还会再犯（判据在用法里，不在机制里）。

教训与「闸门在 ≠ 闸门生效」同族：**纪律应该落在机制上，不是落在每个调用点**。

## 所以改成单点判定

`paths.is_writing_through()`：看**主脚本路径里有没有 `/tests/`**、
且 `DIR` 是不是生产根。两者同时成立才算"写穿"。
`marketlib.log()` 在入口处据此拦下，并打到 stderr（不静默丢）。

判定放单点的代价是"魔法"一点，收益是**新增用例自动受保护**。

## 本文件锁住什么

① 判定真的认得出测试进程（用真子进程跑 `tests/` 下的脚本验）
② 判定**不会**误伤生产（生产跑的是 `src/analyze.py`，路径不含 `/tests/`）
③ `log()` 真的拦了：在写穿语境下生产 watch.log 行数不增
④ 不静默丢弃：被拦的日志要出现在 stderr
"""
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_PROD_LOG = os.path.join(os.path.expanduser("~/.market-watch"),
                         "logs", "watch.log")


def _nlines():
    try:
        with open(_PROD_LOG, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return -1


# ---------- ① 判定认得出测试进程 ----------
# 真的建一个 tests/ 下的脚本跑起来，让**被测代码自己**报判定结果。
# 不能用 `python3 -c` —— argv[0] 会是 `-c`，判定必然 False，那是恒真断言。
_probe = os.path.join(_HERE, "_zz_probe_logguard.py")
_before = _nlines()
try:
    with open(_probe, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, {_SRC!r})\n"
            "import paths, marketlib as M\n"
            "print('TESTPROC', paths.IS_TEST_PROCESS, flush=True)\n"
            "print('THROUGH', paths.is_writing_through(), flush=True)\n"
            "M.log('__logguard_probe_marker__')\n")
    _env = {k: v for k, v in os.environ.items() if k != "MW_DIR"}
    _r = subprocess.run([sys.executable, "-B", _probe],
                        capture_output=True, text=True, timeout=120, env=_env)
    check(_r.returncode == 0,
          f"探针脚本跑失败：{(_r.stderr or '')[-300:]}")
    check("TESTPROC True" in _r.stdout,
          f"`IS_TEST_PROCESS` 没认出来（这是最关键的一条，"
          f"认不出就等于闸门常开）：stdout={_r.stdout!r}")
    # ② 不误伤生产：生产跑的是 src/ 下的脚本
    _p2 = subprocess.run([sys.executable, "-B", "-c",
                          f"import sys; sys.path.insert(0, {_SRC!r}); "
                          f"import paths; print(paths.IS_TEST_PROCESS)"],
                         capture_output=True, text=True, timeout=120, env=_env)
    check(_p2.stdout.strip() == "False",
          f"`python3 -c` 被误判成测试进程（会静默关掉生产日志）："
          f"{_p2.stdout.strip()!r}")

    # ---------- ③ 真的没写进生产日志 ----------
    # 探针已调用过 `M.log(...)`。若闸门没生效，这一行会落进生产 watch.log。
    _after = _nlines()
    check(_before >= 0 and _after >= 0, "读不到生产 watch.log，无法验证")
    check(_after == _before,
          f"探针调用 `M.log()` 后生产 watch.log 从 {_before} 涨到 {_after} 行 —— "
          f"写穿防线没生效")

    # ---------- ④ 不静默丢弃 ----------
    check("__logguard_probe_marker__" in (_r.stderr or ""),
          f"被拦下的日志没有出现在 stderr —— 静默丢弃比写穿更难排查："
          f"stderr={( _r.stderr or '')[:300]!r}")
finally:
    try:
        os.unlink(_probe)
    except OSError:
        pass

print(f"ALL PASS (测试不写穿生产日志 {_ok} 个断言)")
