# Fusion 21K-Word Stress Test — Result Report

**测试时间**: 2026-04-26 凌晨
**测试目标**: 验证 fusion 流水线在 21,000 词 vault 上的健壮性

## 跑了什么

3 类测试，全部本地跑（无 LLM API key 在线，用 mock 替代 + 静态分析）：

1. **静态 vault 分析** — 全 21K 词扫一遍，找结构性问题
2. **流水线鲁棒性** — 500 个 mock fusion 调用，5% 注入对抗性 LLM 输出
3. **多代复合压测** — 30 个 fusion 链各跑 8 代，跑出 power 增长上限

## 测试前发现的 3 个 HIGH，已全部修复

### HIGH-1: fusion.py 不验证 LLM 输出 → 23/29 坏输出沉默通过 ✅ 已修

**问题**: 旧 `evaluate()` 直接 `float(result_json["compatibility"])` + `LANG_MAP.get(sl, 0)`，意味着：
- `compatibility = 1.5` → 直接当 1.5 用（合约层会出乱子）
- `suggested_language = "klingon"` → 沉默 fallback 到 en（lang_id=0）
- `suggested_word = "bad||word\n"` → 直接进 cache，污染 pair_key 分隔符
- `suggested_word = "a" × 200` → 上链时合约虽然不会爆但是 gas 浪费 + UX 烂
- `suggested_word = ["list", "instead"]` → 直接当字符串处理崩

**修复**：新增 `_validate_llm_output()` + `FusionValidationError`，在缓存写入和签名授权之前严格校验：
- compatibility ∈ [0, 1]，必须是 numeric（拒绝 bool）
- suggested_word：非空字符串，长度 ≤ 32，无 `\n\r\t\x00` 和 `||`
- suggested_language：必须是 `{en, zh, ja, ko, fr, de}` 之一，**不再沉默 fallback**
- rationale：截断到 512 字符

**修复后压测结果**：500 次中 29 次坏输出，**全部被 validator 抓到，0 滑过**。

---

### HIGH-2: 多代 fusion 6 代后 uint16 溢出 ✅ 已修

**问题**: 合约里 `power` 是 `uint16`（max 65535）。Worst-case 增长路径：
- 起始 35 power
- comp=0.1 → multiplier=3.0
- gen0=35, gen1=261, gen2=969, gen3=3081, gen4=9399, gen5=28353, **gen6=85230 🔥**

第 6 代必然溢出。30/30 链都会在 8 代内炸。如果 Coordinator 算出 85230 然后传给 web3.py 编码 uint16，要么报错要么截断成 `85230 % 65536 = 19694`（一个完全错的数），UX 很差。

**修复**：新增 `_capped_new_power()`，超过 65535 直接 cap 在 65535 + log warning。

**修复后压测结果**：第 6 代触发 cap，power 被钉在 65535。30/30 链都触发 cap，**0 个 overflow**。

经济角度：用户已接受"fusion EV>1 → 持续套利 fusion pool"风险，这里只修了**类型安全**，没改经济模型。

---

### HIGH-3: pair_key 含 `||` 分隔符可被污染 ✅ 间接修

由 HIGH-1 的输入校验同时解决 —— `suggested_word` 含 `||` 直接 reject，所以 fusion product 的 word 永远不会污染下次 fusion 的 pair_key。

vault 里 21K 个原始词扫了一遍，**0 个**含 `||` 或控制字符（结果在 `pair_key_collisions_in_500_sample = 0`）。

---

## 剩 1 个 MEDIUM（决策性，未修）

### MEDIUM-1: fusion_cache 无 model/version pinning

**问题**: 缓存 key = `pair_key(word_a, lang_a, word_b, lang_b)`。如果 fire+water 第一次喂 Claude 3.7 出来 "steam"，半年后切到 Claude 4.7 想重测，得手动删 cache 行。第一次幻觉永久生效。

**为什么没修**: 这是治理决策 —— 是否要在 model 升级时强制重算所有 cache？决策面：
- 强制重算 → 老 fusion 结果会变，已经 mint 的 NFT lore 不一致
- 不重算 → cache 永久绑定第一次的 model
- 折中 → cached_at 加 30 天 staleness 阈值

让用户决策。代码里加了 TODO 注释。

---

## 静态 vault 分析结果（参考）

| 指标 | 值 | 备注 |
|---|---|---|
| 总词数 | 21,000 | OK |
| 词长分布 | min=1, p50=3, p95=9, p99=11, max=25 | LLM 上限 32 安全 |
| 跨语言同词碰撞 | 578 对 | 例: "nft" 在 en 和 zh 都有；正常多语义 |
| 含可疑字符的词 | 0 | vault 干净 |
| pair_key 碰撞（500 抽样） | 0 | 算法正常 |

按 rarity 的 power 分布：
- legendary: 1117 个，power 74-100，均值 79.5
- rare: 3790 个，power 62-73，均值 66.8
- uncommon: 4790 个，power 52-61，均值 55.5
- common: 11303 个，power 3-51，均值 36.7

金字塔分布合理。

---

## 修复同时新增的测试

`coordinator/tests/test_fusion_validation.py` — 13 个测试：
- compatibility 越界 / 非 numeric 拒绝
- suggested_word 太长 / 空 / 含 `||` / 含控制字符 拒绝
- suggested_language 不在白名单拒绝（含 `lang_id` 直传 path）
- rationale 自动截断
- `_capped_new_power` 不会 overflow（20 代复合）
- multiplier 桶值 spot check

加上现有 60 个 coordinator 测试和 76 forge 测试，**149/149 全绿**。

---

## 没测的（需要 LLM API key）

- 真实 LLM 输出的 compatibility 分布（mock 是确定性 hash）
- LLM 在不同语言对上的判断稳定性
- 多 model 跨版本的输出漂移
- 实际 latency / cost per fusion call

要测这些得有 ANTHROPIC_API_KEY，等你起床给。

---

## 一句话总结

跑下来发现 3 个 HIGH，全是流水线鲁棒性的明显 bug，全修了 + 写了 13 个回归测试。
经济模型层面的事（fusion EV、power 硬顶到底定多少、cache 版本策略）按你说的"决策性留着"。
