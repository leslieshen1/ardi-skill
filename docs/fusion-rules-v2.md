# Fusion Rules V2 — Compat Tier 系统 + 平滑 Multiplier 曲线

## 现状（V1）的 3 个问题

### 问题 1：LLM 自由度过大

LLM 直接输出 `compatibility ∈ [0, 1]`，**没有 rubric**。同样性质的 pair（比如 fire+water 这种"经典对立"）可能这次给 0.85、下次给 0.75，**破坏可重复性 + 可审计性**。

举例（来自我刚做的 100 对评估）：
- `fire + water` → 0.88（我说"经典对立")
- `water + ice` → 0.95（我说"phase change"）
- `fire + earth` → 0.78（我说"yields lava"）

这 3 对都是"元素相关"，但分数差 0.17。LLM 凭感觉，没规则。

### 问题 2：Multiplier 阶梯式跳变

```
compat   multiplier
≤ 0.30   3.0
0.30-0.60 2.5
0.60-0.80 2.0
> 0.80   1.5
```

边界悬崖：compat 0.79 → 2.0×，compat 0.80 → 1.5×。**LLM 给的 0.01 差异导致 33% 倍数差**。这激励 LLM 故意躲避边界（聚集到 0.85 或 0.75 这种"安全区"）。

### 问题 3：Success rate 边界缺设计文档

```python
success_rate = 0.20 + 0.50 × compat
# compat=0:   20% 成功（凭啥不是 0%？）
# compat=1.0: 70% 成功（凭啥不是 100%？）
```

**0.20 floor**：意图大概是"凡尝试都有基础成功率"鼓励奇葩组合。
**0.70 ceiling**：意图大概是"即使完美匹配也有失败可能"保留戏剧性。
但都没文档化。

---

## V2 提案

### Part 1：5-Tier Compat Rubric（强约束）

LLM 必须先选 tier，每个 tier 对应一个 **闭区间**，最终 compat 由 tier + pair_key hash 确定性采样。

| Tier | Range | 触发条件 | 例子 |
|---|---:|---|---|
| **T1: Identical** | `[0.85, 0.99]` | 同一个抽象概念在两边语言里完全等同（含同形同义跨语言、严格同义词） | 文化(zh) + 文化(ja)；fire + 火 |
| **T2: Strong** | `[0.65, 0.84]` | 同一 domain 强相关，自然搭配或互补 | fire + water；sun + moon；king + queen |
| **T3: Loose** | `[0.40, 0.64]` | 同一大类（情感、自然、技术），或可叙事化关联 | love + hope；computer + AI；wind + tree |
| **T4: Weak** | `[0.15, 0.39]` | 弱关联 / 间接联系 / fantasy 联想 | king + sword；dog + 区块链 (memecoin) |
| **T5: Unrelated** | `[0.01, 0.14]` | 没有可识别语义关联，纯粹偶然碰撞 | bitcoin + 土豆；computer + 幸運 |

LLM 输出 schema 改为：

```json
{
  "tier": "T2",         // 必填，5 选 1
  "tier_subscore": 0.6, // [0,1]，在 tier 区间内的相对位置
  "suggested_word": "steam",
  "suggested_language": "en",
  "rationale": "fire and water classically yield steam"
}
```

合约不直接拿 tier_subscore — Coordinator 根据 `(tier, pair_key)` 算最终 compat：

```python
T_RANGES = {
  "T1": (0.85, 0.99),
  "T2": (0.65, 0.84),
  "T3": (0.40, 0.64),
  "T4": (0.15, 0.39),
  "T5": (0.01, 0.14),
}

def compute_compat(tier: str, tier_subscore: float, pair_key: str) -> float:
    lo, hi = T_RANGES[tier]
    # subscore 范围 [0, 1]，平滑映射到 tier 区间
    base = lo + (hi - lo) * tier_subscore
    # 加一点 deterministic jitter（pair_key 决定）防止所有 T2 都聚集到一个点
    h = int.from_bytes(keccak(pair_key.encode())[:4], "big") / 2**32
    jitter = (h - 0.5) * 0.05  # ±0.025
    return clamp(base + jitter, lo, hi)
```

**好处**：
- 同一 pair 始终落在同一 tier（LLM 给的 tier 是离散的，subscore 是 [0,1]）
- 跨 tier 不会因为小数点跳变
- pair_key hash 加 jitter 防止 T2 全 0.7、T3 全 0.5 这种聚集

**LLM prompt 改造**：明确给出 5 tier rubric + 例子，让 LLM "先分类后打分"，更接近人类专家做 rubric 评分的模式。

---

### Part 2：连续 Multiplier 曲线

去掉 4 阶梯，换成单一连续函数：

```
multiplier(c) = 1.5 + 1.5 × (1 - c)^2
```

意思：
- c = 0.0 → 3.0 (最大，奖励奇葩组合)
- c = 0.5 → 2.0 (中间)
- c = 0.85 → 1.5 + 1.5 × 0.0225 = 1.534 (近似 1.5)
- c = 1.0 → 1.5 (最小)

平滑过渡，没有悬崖：

```
compat:    0.0   0.2   0.4   0.5   0.6   0.7   0.8   0.9   1.0
mult v1:   3.0   3.0   2.5   2.5   2.0   2.0   2.0   1.5   1.5
mult v2:   3.00  2.46  2.04  1.88  1.74  1.64  1.56  1.52  1.50
```

差不多保持 v1 的"低 compat 高倍数"原则，但**单调连续**。

可调参数：

```python
MULT_BASE = 1.5        # 高 compat 最低倍数
MULT_SPREAD = 1.5       # 倍数范围（base + spread = 最高）
MULT_EXPONENT = 2.0     # 曲线陡度（1=线性，2=凸函数偏向高 compat 多奖励，0.5=凹函数偏向低 compat）
```

设计文档里写明每个参数的语义，未来调参不用碰 4 个 if-else。

---

### Part 3：Success Rate 改造（可选，但建议）

V1：`success = 0.20 + 0.50 × compat` → range [0.20, 0.70]

V2 提议：**两层成功率**

```python
def success_rate(c: float) -> float:
    # 主曲线：sigmoid，0.5 处 50%，平滑过渡
    main = 1 / (1 + exp(-8 * (c - 0.5)))   # c=0 → 1.8%, c=0.5 → 50%, c=1.0 → 98.2%
    
    # Floor：极低 compat 也保留少量"奇迹"成功率
    floor = 0.05  # T5 unrelated 仍然 5% 概率成功
    
    return max(main, floor)
```

跟 V1 比：

```
compat:    0.0   0.1   0.2   0.3   0.4   0.5   0.6   0.7   0.8   0.9   1.0
v1 rate:   20%   25%   30%   35%   40%   45%   50%   55%   60%   65%   70%
v2 rate:    5%    5%    8%   12%   23%   50%   77%   88%   95%   97%   98%
```

意义：
- **奇葩组合（T5）从 20-25% → 5%**，更严格；高 power 的"幸运怪物"产物更稀有
- **强匹配（T1-T2）从 60-70% → 95-98%**，几乎稳成功，提升用户体验
- **中等（T3）从 40-45% → 23-50%**，区间最大，最依赖 LLM 判断质量

设计哲学：让 fusion 行为符合直觉 —— "好搭配几乎必成，奇葩搭配偶尔出彩"。

---

### Part 4：Vault False Friend 标记（小补丁）

现在 vault 有 ~5 个 false friend（如 `parole` en/fr 不同义）。

提议：在 vault 数据加可选 `false_friend_of: [other_word_id, ...]` 字段。

```json
{"word": "parole", "language": "en", "power": 42, "rarity": "common",
 "false_friend_of": [其他 lang 的 parole 的 word_id]}
```

LLM prompt 加一条：
> If the input pair is flagged as `false_friend`, assign T4 or T5 even if the spelling matches.

这是 nice-to-have，不是 blocker。LLM 自己也能识别，但显式标记降低误差。

---

## Suggested Language 规则（顺手定）

V1：spec 说"randomly from either parent's language"，但 mock 和我都倾向 en。

V2 三个选项：

| 选项 | 行为 | 优劣 |
|---|---|---|
| **A：自由选 6 种** | LLM 看哪种合适，倾向 en | 简单，但同质化 |
| **B：父词二选一** | 必须从 (lang_a, lang_b) 选 | 严格遵守 spec，但 zh+ja 父词总产 zh/ja 新词 |
| **C：加权随机** | 70% 概率从父词选，30% 从 6 种自由选 | 平衡 |

我推荐 **B**（最国际化、最忠于 spec）。但你定。

---

## 实现影响

如果你拍板做 V2：

| 改动 | 文件 | 工作量 |
|---|---|---|
| 5-tier rubric + LLM prompt 改造 | `fusion.py:SYSTEM_PROMPT` + `_validate_llm_output` | 1h |
| `tier` + `tier_subscore` schema | 同上 | 0.5h |
| `compute_compat()` 函数（jitter via pair_key） | `fusion.py` 新增 | 0.5h |
| 连续 multiplier 曲线 | `_multiplier` 重写 | 5min |
| Sigmoid success_rate | `_success_rate` 重写 | 5min |
| 测试更新 | `test_fusion_validation.py` | 1h |
| Mock LLM 校准 | `fusion_full_traversal.py`、`fusion_llm_eval_100.py` | 30min |
| 文档 | 这份文件 → DESIGN-FUSION-V2.md | done |

总：**3-4 小时**。所有改动是链下的（fusion.py 在 Coordinator），合约不变。

---

## 拍板清单

请回答：

1. **5-Tier rubric + tier_subscore schema** — 接受 / 拒绝 / 调整 tier 数量或区间
2. **连续 multiplier 曲线 `1.5 + 1.5×(1-c)²`** — 接受 / 微调 base/spread/exponent / 保持 V1 阶梯
3. **Sigmoid success_rate（5% floor, ~98% ceiling）** — 接受 / 调 floor / 调陡度 / 保持 V1
4. **Suggested language 规则** — A 自由 / **B 父词二选一**（推荐）/ C 加权
5. **Vault false friend 标记** — 现在做 / 上线后再加 / 不做

回答完我就开始动手。
