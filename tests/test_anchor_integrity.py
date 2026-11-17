#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""所有变异体的注入锚点必须能在源码里逐字找到。

⚠️ 为什么必须有这个测试（2026-11-17）：
    `selfcheck_mutation.py` 的锚点是**逐字匹配**。改源码时忘了同步锚点，
    后果不是"测试报错"，而是**回归静默变成假漏网** —— 锚点找不到 →
    注入不生效 → 变异体写成"漏网"，但你不知道是真漏网还是锚点漂了。
    实测当天：改 `cj()` 的熔断键（`host=host` → `host=_bare_host`），
    漏掉了 `fallback-chain-cut` 一条，跑了一小时才发现。

    这个检查把"锚点完整性"从**跑完全量回归才知道**，提前到**秒级**。

用法：
    python3 tests/test_anchor_integrity.py
退出码 0 = 全部匹配；1 = 有失配（打印名单）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def main():
    import selfcheck_mutation as M
    bad, skipped = [], 0
    for m in M.MUTANTS:
        name, fname, old = m[0], m[1], m[2]
        # 特殊注入器用 `__XXX__` 占位，自己找注入位置；不是源码锚
        if not old or str(old).startswith("__"):
            skipped += 1
            continue
        path = os.path.join(ROOT, "src", fname)
        if not os.path.exists(path):
            skipped += 1
            continue
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if old not in src:
            bad.append(name)

    total = len(M.MUTANTS)
    # ⚠️ 输出必须以 `ALL PASS` 结尾 —— `tests/run_all.sh` 靠这个标记判通过，
    # 只打 ✅ 会被记成「rc=0 但输出无 PASS/SKIP 标记」而算失败（2026-11-17 踩过）。
    print(f"锚点完整性：{total} 条变异体 · {skipped} 条特殊注入器跳过 · "
          f"{len(bad)} 条失配")
    if bad:
        print("\n❌ 以下变异体的注入锚在源码里找不到（锚点漂移）：")
        for n in bad:
            print(f"   - {n}")
        print("\n→ 改过对应源码的话，请同步更新 `selfcheck_mutation.py` 里的")
        print("   `old` / `new` 两侧字面量（必须逐字一致，含缩进与换行）。")
        return 1
    print(f"ALL PASS ({total} 条锚点全部匹配，{skipped} 条特殊注入器跳过)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
