"""金额闸门与状态归一的补充断言 —— 2026-11-08 优化批次。

四件事，各自独立：

1. **文字货币代码**：`USD 5000` / `RMB 5000` / `CNY 5000` 原来一律放行。
   要求「代码紧贴数字」——`USD 走强` 这类没数字的正常表述必须放行。
2. **inflight 载入去重**：它表达的是集合语义，重复键没有意义。而各处的
   摘除写法不统一（三处 `list.remove()` 只摘一个、两处列表推导删全部），
   于是盘上只要有重复键，`remove()` 路径就会留下孤儿键 → 进 `busy`
   → 该事件当天永不再触发、消息从未发出。
3. **_priv_in_clause 必须查所有出现位置**：原实现 `pre.find(_w)` 只看第一次，
   于是「融资余额大增……余额只剩 5000 元」里第二个「余额」根本没被看到。
   当时被 12 字窗口 + 切句符 + ctx 后有数字三道约束兜住，但那是
   **守住了**而非**判对了**。
4. **runlog.struct_partial**：把「回退首版但结构仍缺段」变成可统计的字段。
   不改读者看到的内容（缺一段的真实内容优于机械模板），只加可观测性。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_gate_and_state_hygiene.py`
"""
import os, sys, json, time, datetime as dt, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M
import runlog
import analyze


def check_currency_codes():
    _leak = ["USD 5000", "RMB 5000", "CNY 5000", "usd 5000", "usd5000",
             "USD 12,000", "CNY 3.5万"]
    for _s in _leak:
        assert not M.safe(_s)[0], f"文字货币代码未被拦：{_s!r}"

    _ok = ["USD 走强", "美元指数 USD 走高", "人民币汇率 RMB 升值",
           "CNY 汇率稳定", "USD/CNY 中间价 7.12", "美联储 USD 流动性收紧"]
    for _s in _ok:
        _r = M.safe(_s)
        assert _r[0], f"正常表述被误拦：{_s!r} -> hit={_r[1]!r}"


def check_inflight_dedup():
    _p = os.path.join(os.environ["MW_DIR"], "state")
    os.makedirs(_p, exist_ok=True)
    _f = os.path.join(_p, "state.json")
    # ⚠️ 日期必须是**今天**：跨日的 state 会走「新的一天重置」分支，
    # inflight 被整体清空，看不到去重效果（第一版就踩了这个，
    # 得到 inflight=[] 还以为是去重没生效）。
    json.dump({"date": dt.date.today().isoformat(), "fired": [],
               "inflight": ["K", "K", "J"], "inflight_sent": [],
               "pending_idx": [], "hourly_retry": {}, "count": 0,
               "pending": {}, "inflight_ts": time.time()},
              open(_f, "w", encoding="utf-8"))
    _st = analyze.load_st()
    assert _st.get("inflight") == ["K", "J"], (
        f"inflight 重复键未去重：{_st.get('inflight')}（应为 ['K','J']）")


def check_priv_all_occurrences():
    # 第一个「余额」带市场限定语，第二个没有 → 必须判为个人口径
    assert M._priv_in_clause("融资余额大增，规模创纪录，余额只剩 ") is True, (
        "只查第一次出现 → 第二个「余额」被忽略，个人口径没被判出来")
    # 全部带限定语 → 不是个人钱
    assert M._priv_in_clause("融资余额增至") is False
    assert M._priv_in_clause("两融余额创新高") is False
    # 单个无限定语 → 个人钱
    assert M._priv_in_clause("余额只剩") is True


def check_runlog_struct_partial():
    _p = os.path.join(os.environ["MW_DIR"], "state", "history")
    os.makedirs(_p, exist_ok=True)
    _f = os.path.join(_p, "runs.jsonl")
    if os.path.exists(_f):
        os.remove(_f)
    runlog.run("analyze.py", "ok", struct_partial=True)
    runlog.run("analyze.py", "ok")
    _rows = [json.loads(l) for l in open(_f, encoding="utf-8") if l.strip()]
    assert _rows[0].get("struct_partial") is True, (
        "传 True 时必须落 struct_partial")
    assert _rows[1].get("struct_partial") is None, (
        "不传时必须省略该键（『没这个键』才是『结构完整』）")


def check():
    check_currency_codes()
    check_inflight_dedup()
    check_priv_all_occurrences()
    check_runlog_struct_partial()
    print("ALL PASS (货币代码/去重/私人口径/struct_partial 共 22 个断言)")


if __name__ == "__main__":
    check()
