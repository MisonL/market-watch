#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent 持久记忆的**新克隆自举**：仓库里没有 memory.md 时也能跑起来。

## 为什么需要这一道

`memory.md` 是 agent 跨日的持久记忆，由 `append_memory` 工具每天追加。
它在 `agent/.gitignore` 之外（2026-11-19 之前一直入库），每次调研写一行 ——
既是**运行时状态**、又是**这台机器的历史**，不该进版本库。

但只要把它移出版本库，就立刻暴露一个缺陷：**没有任何自举逻辑**。

`memory.md` 只在 `append_memory` 里被创建：

    os.open(MEMORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

而 prompt 第 1 步就要求 agent：

    f"用 Read 读 {PLAN}（市场语境）与 {MEMORY}（跨日记忆）"

新克隆的环境里那个文件**不存在** → `Read` 直接失败 → 「昨日判断验证」段
无从写起。这不是"偶尔少一段"，是**每一天的 ③ 段都缺**（prompt 明写
「读 memory.md 里上一交易日的明日观察点」）。

## 本文件锁住什么

- 目录里没有 memory.md → 导入模块后**自动建出**、带模板头、权限 0600
- 已存在的 memory.md **一个字节都不动**（自举不能覆盖历史）
- 自举出来的文件可以直接被 `append_memory` 追加，格式与历史一致
  （`- YYYY-MM-DD: 内容`），且追加位置在头部之后
- 权限是 0600（与 `append_memory` 的 `0o600` 同口径）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "agent", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

try:
    import claude_agent_sdk  # noqa: F401
except Exception:
    print("SKIP: 当前解释器没有 claude_agent_sdk（请用 agent/.venv/bin/python 跑）")
    raise SystemExit(0)

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


def _load(mod_dir, mw_dir):
    """在指定 MW_DIR 下加载 agent 模块（模拟一次全新克隆）。"""
    import importlib.util
    os.environ["MW_DIR"] = mw_dir
    spec = importlib.util.spec_from_file_location(
        f"mw_agent_boot_{abs(hash(mw_dir))}",
        os.path.join(mod_dir, "mw_agent", "__main__.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_AGENT_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "agent", "src")

# ---------- ① 全新克隆：文件不存在 → 导入后自举出来 ----------
_d1 = tempfile.mkdtemp(prefix="mw_boot1_")
_m1 = _load(_AGENT_SRC, _d1)
_mem1 = _m1.MEMORY
check(os.path.exists(_mem1),
      f"全新环境必须自举出 memory.md，实际不存在：{_mem1}")
_txt1 = open(_mem1, encoding="utf-8").read()
check(_txt1.startswith("#"),
      f"自举文件必须有标题头（否则 agent Read 到的是一个空文件，"
      f"不知道格式）：{_txt1[:40]!r}")
check("- YYYY-MM-DD" in _txt1,
      "自举文件必须说明每行格式（agent 靠这个决定怎么写）")
_mode1 = os.stat(_mem1).st_mode & 0o777
check(_mode1 == 0o600,
      f"自举文件必须是 0600（与 append_memory 的 0o600 同口径），实得 {_mode1:o}")

# ---------- ② 已存在：一个字节都不许动 ----------
_d2 = tempfile.mkdtemp(prefix="mw_boot2_")
os.makedirs(os.path.join(_d2, "agent"), exist_ok=True)
_seed = ("# 调研 Agent 持久记忆\n\n> 说明\n\n"
         "- 2026-01-01: 历史记录，自举绝不能覆盖它\n")
_p2 = os.path.join(_d2, "agent", "memory.md")
with open(_p2, "w", encoding="utf-8") as f:
    f.write(_seed)
_m2 = _load(_AGENT_SRC, _d2)
check(_m2.MEMORY == _p2, f"MEMORY 路径应指向 {_p2}，实得 {_m2.MEMORY}")
check(open(_p2, encoding="utf-8").read() == _seed,
      "自举**不得覆盖**已存在的 memory.md（历史记录会丢）")

# ---------- ③ 自举出来的文件能被 append_memory 正常追加 ----------
import asyncio
_r = asyncio.run(_m1.append_memory.handler({"text": "自举验证：写入一条测试记忆"}))
check("已写入" in str(_r), f"append_memory 应成功，实得 {_r}")
_txt3 = open(_mem1, encoding="utf-8").read()
check("自举验证：写入一条测试记忆" in _txt3, "追加的内容必须落盘")
import time as _t
_today = _t.strftime("%Y-%m-%d")
check(f"- {_today}: 自举验证" in _txt3,
      f"追加行必须是 `- {_today}: …` 格式（与历史 41 行同口径）")
# 头部必须仍在最前（自举模板 + 追加 = 顺序正确，不能把头部挤到后面）
check(_txt3.startswith("# 调研 Agent 持久记忆"),
      "追加不能把头部挤到中间")
check(_txt3.index("# 调研 Agent 持久记忆") < _txt3.index("自举验证"),
      "头部必须在记录之前")

# ---------- ④ 模板文件本身要入库，且不含真实历史 ----------
_tpl = os.path.join(os.path.dirname(_AGENT_SRC), "memory.example.md")
check(os.path.exists(_tpl),
      "仓库里必须有 memory.example.md 模板（否则新克隆的人不知道 memory.md 长什么样）")
_tpl_txt = open(_tpl, encoding="utf-8").read()
check("- YYYY-MM-DD" in _tpl_txt, "模板必须含格式说明")
check("4070" not in _tpl_txt and "hstech" not in _tpl_txt.replace("hstech_up/hstech_down", ""),
      "模板**不得**含真实历史结论（那是这台机器的行情记忆，不是模板）")

for _d in (_d1, _d2):
    import shutil
    shutil.rmtree(_d, ignore_errors=True)

print(f"ALL PASS (agent 记忆自举 {_ok} 个断言)")
