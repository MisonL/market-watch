# 贡献指南

## 先读这一节：本项目的代码不是「随便改」的

本仓库有两层测试，第二层对**源码文本**有硬依赖。不了解这一点就改代码，
会把测试搞坏而自己毫无察觉。

### 第一层：单元测试 + 自检

```bash
bash tests/run_all.sh        # 48 个测试文件，约 20s（并发度自适应）
python3 src/selfcheck.py     # 契约校验 + 健康检查，1~3 分钟
```

这一层跟普通项目一样，改坏了会红。

### 第二层：变异测试（关键）

```bash
python3 src/selfcheck_mutation.py          # 增量（按基线，只跑变更的）
python3 src/selfcheck_mutation.py          # 全量约 2~6 小时
MW_FULL=1 python3 src/selfcheck_mutation.py
```

它的逻辑是：**往源码里注入一个已知缺陷，看自检能不能抓住**。
379 个变异体，每个都是一条 `(name, file, old, new, desc, must_contain)`：

```python
("hk-calendar-emptied",
 "marketlib.py",
 "    \"2026-10-19\",        ← old：要在源码里**逐字**找到的文本
 ...",
 "    （删掉这一行）",        ← new：替换成什么
 "[节假日表] 港股休市日必须被判休市",   ← desc
 "港股休市日 2026-10-19"),             ← must_contain：报错里必须出现的锚
```

**`old` 是逐字匹配的**。所以：

- 改缩进、改空格、改换行位置 → 锚点失配 → 回归报「锚点未匹配」
- 全库有 **177 条**锚含换行（跨行锚），改相关代码时必然一起动

### 因此：不要直接跑 `black src/`

```bash
black --check src/    # 12 个文件全都要重排
```

跑完 `black` 会让 177 条锚点集体失配。**本项目刻意不做全量格式化**。

要格式化新写的文件，请只对**新文件**用 black，或手工遵守周边代码风格
（两空格缩进、紧凑赋值这类既有习惯优先于 black 默认值）。

### 改源码后的正确流程

```bash
# 1. 改代码
# 2. 单元测试
bash tests/run_all.sh
# 3. 自检
python3 src/selfcheck.py
# 4. 增量变异（只跑目标文件相关的变异体）
MW_ONLY='关键字1|关键字2' python3 src/selfcheck_mutation.py
# 5. 改动较大时跑全量
MW_FULL=1 MW_JOBS=6 python3 src/selfcheck_mutation.py
```

**看到「锚点未匹配」不要急着改 `must_contain`** —— 先确认是代码真的变了
（那就同步更新锚点和 `old`），还是并发/环境导致的假漏网。

---

## 提交前自查

- [ ] `bash tests/run_all.sh` 全绿
- [ ] `python3 src/selfcheck.py` 无 ❌
- [ ] `pyflakes src/*.py` 无输出
- [ ] 没有把 webhook key、内网 IP、个人持仓写进代码或配置
- [ ] 新增的 `must_contain` 在**干净基线输出**里不出现（否则恒真、判别力为零）

## 新增变异体的注意事项

1. `old` 必须是源码里**唯一**出现的片段（超出一处会拒绝注入）
2. `must_contain` 必须只在**缺陷触发时**出现 —— 写一条在正常输出里就有的
   字符串，等于没有判别力（自检的「恒真锚」守卫会拦，但别指望它兜底）
3. 五元组（没有 `must_contain`）必须在 `_MUST_EXTRA` 里登记

## 代码风格

- 中文注释，解释**为什么**而不是**做了什么**
- 重要的坑要带日期和实测证据（仓库里大量注释是这个格式，请沿用）
- 两空格缩进（不是 PEP 8 的四空格）—— 这是历史选择，改它等于重排全库

## 数据源相关的改动

- 板块数据（`90.BKxxxx`）**只有东财提供**，没有替代源
- 资金流字段 `f62`/`f184` 是东财独有加工数据
- 降级链改动请先读 `marketlib.py` 里 `DEAD_TTL` / `_host_dead` / `_fallback_get`
  的长注释 —— 那里记录了 2026-11-17 一次真实事故的教训
