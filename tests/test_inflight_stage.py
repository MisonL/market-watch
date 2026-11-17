"""认领残迹按「这个键发出去了吗」逐键处理 —— 2026-11-06 全量复核 P1-6。

背景：`state.json` 的 `inflight` 语义是「已认领、结果未知」。它有两种残迹：
  · 进程在 **AI 生成阶段** 就被杀 —— 消息根本没发出去
  · 进程在 **push 成功之后、_commit 之前** 被杀 —— 消息已经发出去了

旧实现 `load_st()` 对两者一视同仁，**无条件转正到 fired**。它的理由
（防重复推送）在第二种残迹里是对的，但把第一种也记成「今天已推过」，
该事件当天再也不补发 —— 群里没收到，面板显示完成。
而 8h 阈值 + 交易日只有 6h10m（09:25~15:35）意味着这条清理在交易时段内
结构上不可能触发。

**为什么是 `inflight_sent: list[str]` 而不是单个状态字符串**
中间实现过 `inflight_stage = "claimed"/"sent"`，两个方向都错：
  · 每轮非 dry 都写 "claimed" → 把上一轮已推送的键**降级** → 重复推送
  · 推送成功时写 "sent"（save_st 写的是整份列表）→ 把本轮**没发出**的旧认领
    一起标成已发 → 漏报
因为 `inflight` 里同时挂着这两类键，一个字段概括不了。改为按 key 记。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_inflight_stage.py`
"""
import os, sys, json, time, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import paths
import analyze

_STATE = paths.p_state("state.json")
# MW_DIR 是全新的空目录时 state/ 还不存在（生产里由首轮写盘创建）。
os.makedirs(os.path.dirname(_STATE), exist_ok=True)


def _write(sent, keys, age_min):
    """造一份只有给定 inflight 键的 state.json。sent=None 表示**不写该键**（旧数据）。"""
    d = {
        "date": time.strftime("%Y-%m-%d"),
        "fired": [],
        "inflight": list(keys),
        "inflight_ts": time.time() - age_min * 60,
        "count": 0,
        "hourly_retry": {},
        "pushed_slots": [],
    }
    if sent is not None:
        d["inflight_sent"] = list(sent)
    with open(_STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def _read_disk():
    with open(_STATE, encoding="utf-8") as f:
        return json.load(f)


def check():
    # ① 已发 + 刚推完（5min）：立即转正 —— 在 load_st 里看到它只可能是
    #    "推完、_commit 之前进程死了"，load_st 的唯一生产调用点持有 flock，
    #    所以不可能有一轮还活着。
    _write(["k1"], ["k1"], 5)
    st = analyze.load_st()
    assert st["inflight"] == [], f"已发残迹应清出 inflight，实得 {st['inflight']}"
    assert "k1" in st["fired"], "已发残迹必须转正到 fired（否则重复推送）"

    # ② 已发 + 陈旧（200min）：同样立即转正，与年龄无关
    _write(["k1"], ["k1"], 200)
    st = analyze.load_st()
    assert "k1" in st["fired"], "已发残迹不论多久都必须转正"

    # ③ 未发 + 5min：仍在跑/刚认领 → 一个字节都不能动
    _write([], ["k2"], 5)
    st = analyze.load_st()
    assert st["inflight"] == ["k2"], (
        f"新鲜的未发认领必须保留（防重复推送），实得 {st['inflight']}")

    # ④ 未发 + 60min：生成阶段就死了 → 只清空**不转正** → 当天可重新触发
    _write([], ["k2"], 60)
    st = analyze.load_st()
    assert st["inflight"] == [], f"未发残迹应清空，实得 {st['inflight']}"
    assert "k2" not in st["fired"], (
        "未发残迹**不得**转正到 fired —— 消息没发出去，转正等于漏报")

    # ⑤ 旧数据（无 inflight_sent 键）：沿用旧行为，LOCK_STALE(8h) 后全转正
    _write(None, ["k3"], 600)
    st = analyze.load_st()
    assert "k3" in st["fired"], "缺失 inflight_sent 的存量状态应沿用旧行为转正"
    _write(None, ["k3"], 60)
    st = analyze.load_st()
    assert st["inflight"] == ["k3"], "未过 LOCK_STALE 的旧数据不应被动"

    # ⑥ 混合：a 已发、b 未发，都超时 → a 转正、b 只清
    _write(["a"], ["a", "b"], 60)
    st = analyze.load_st()
    assert st["inflight"] == [], f"两条都应清出，实得 {st['inflight']}"
    assert "a" in st["fired"] and "b" not in st["fired"], (
        f"应按 key 分别处理，实得 fired={st['fired']}")

    # ⑦ 契约：`inflight_sent` 必须随 load_st 返回。
    #    缺它的后果不是"少一个字段"，而是每次 load_st→save_st 都把盘上的标记
    #    抹掉 —— 新机制形同虚设（这是本轮真实踩过的坑）。
    _write(["k9"], ["k9"], 5)
    st = analyze.load_st()
    assert "inflight_sent" in st, "State 契约必须包含 inflight_sent"
    analyze.save_st(st)
    assert "inflight_sent" in _read_disk(), (
        "load_st→save_st 一轮之后盘上仍须保留 inflight_sent")

    # ⑧ 落盘后再次 load：已发标记仍生效（端到端一轮往返）
    _write(["k4"], ["k4"], 5)
    analyze.save_st(analyze.load_st())
    d = _read_disk()
    assert "k4" in (d.get("fired") or []), (
        f"往返一轮后已发键应已转正，实得 {d.get('fired')}")

    print("ALL PASS (inflight_sent 8 个断言)")


if __name__ == "__main__":
    check()
