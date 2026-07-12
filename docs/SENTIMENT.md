# SENTIMENT — 社媒情绪信号面板（宝妈指数）

> Status: **M2-prep skeleton** · 设计契约 + 第一版骨架接口。
> 本文档定义的是**可解析、可证伪的情绪分档契约**，不是写作指南。
> 写作/分类指南在 `prompts/sentiment_panel.md`。改动动机：
> - 改这份文档要问"解析器/评分器会不会炸"
> - 改那份要问"分类质量够不够"

## 这是什么

**宝妈指数**是一个散户情绪温度计：度量零售侧 FOMO / FUD 的强弱，
语义上是 VIX 的"人气镜像"——不是波动率，而是"普通人有多上头 / 有多恐慌"。

用途只有一条链：

**样本可溯源 → 分档封闭可选 → 触发条件可判定 → 可被 brief 引用 → 日后可评分**

它**不是**买卖信号，也**不是**仓位建议。分析优先、执行永不（见
`docs/DESIGN.md`）。每日 brief 可以引用一个结构化的情绪分档结论；是否
把该分档与技术底叠加入场，是人的判断，不是本面板的输出。

## 与 BRIEF_SPEC 的对齐

本面板复用 brief 体系已经验证过的纪律（见 `docs/BRIEF_SPEC.md`）：

| 纪律 | 在情绪面板中的对应 |
|---|---|
| 封闭词表 | `index_level` 只能从固定分档中选，禁同义替换 |
| 逃生舱 | 信息不足时唯一合法输出是 `信息不足以分级` |
| 可证伪 | 每档绑定可观察触发条件，不是"感觉偏强" |
| 双层标注 | 证据句标注 `[来源: source-id]` 或 `[推断: 基于 …]` |
| 隐私 D2 | 只用抽象 source-id；真实频道/账号/组合 ID 永不入库 |

`scripts/build_brief.py` 本期**不**自动注入情绪分档；接口先稳定，
M2 再决定 brief 的引用字段与 section 落点。

## 封闭词表：`index_level`

`index_level` **只能**从以下取值中选择，不得同义替换或自造：

```
冰点 | 低迷 | 中性 | 亢奋 | 狂热 | 信息不足以分级
```

| 分档 | 语义（零售侧） | 可判定触发条件（满足 ≥2 条独立信号才可出该档；否则降级或逃生） |
|---|---|---|
| `冰点` | 极端 FUD / 放弃式离场 | (a) 样本中投降/割肉/永久离场话术占主导；(b) 讨论量相对近窗显著萎缩且剩余内容偏绝望；(c) 几乎无人讨论反弹或加仓 |
| `低迷` | 兴趣淡、轻度悲观 | (a) 观望/不敢做多话术占主导；(b) 讨论量偏低但未到冰点；(c) 偶有抄底讨论但无扩散 |
| `中性` | 有讨论、无极端倾斜 | (a) 多空话术并存且无单侧压倒；(b) 讨论量处于近窗常态；(c) 缺少明确 FOMO 或投降集群 |
| `亢奋` | 明显 FOMO、追涨话术抬头 | (a) 追涨/怕踏空/加杠杆话术占主导；(b) 讨论量高于近窗常态；(c) 反方谨慎声音被压过但仍可见 |
| `狂热` | 极端 FOMO / 叙事宗教化 | (a) 必涨/人生翻身/不看估值话术占主导；(b) 讨论量异常放大；(c) 几乎听不到反方或反方被嘲讽 |
| `信息不足以分级` | 逃生舱 | 样本为空、过短、过旧、来源单一且噪声过高、或独立信号不足 2 条——**唯一**合法兜底，禁止用"偏强/分化/情绪复杂"等模糊词代替 |

解析器必须把词表外的任何取值一律视为**格式错误**，不做模糊匹配。

### 辅助封闭字段

除 `index_level` 外，骨架输出还固定以下枚举，便于日后评分与 brief 引用：

- `dominant_mode`（情绪主模态，单选）：
  ```
  FUD | FOMO | 观望 | 混合 | 信息不足
  ```
- `confidence`（分档置信度，单选）：
  ```
  低 | 中 | 高
  ```
  规则：样本薄 / 信号冲突 → `低`；信号一致但覆盖窄 → `中`；多源独立信号同向 → `高`。
- `sample_quality`（输入质量，单选）：
  ```
  可用 | 偏薄 | 不可用
  ```

禁止输出具体价格点位、仓位建议、买卖指令（与 `prompts/daily_brief.md`
同一条硬约束）。

## 输出协议

所有实现必须走仓库统一协议（`docs/DESIGN.md` D4）：

- stdout：仅 JSON
  - 成功：`{"ok": true, "data": {…}}`
  - 失败：`{"ok": false, "error": "…", "hint": "…"}`
- stderr：人类可读诊断

### `data` 字段契约（v0）

```json
{
  "index_level": "中性",
  "dominant_mode": "混合",
  "confidence": "低",
  "sample_quality": "偏薄",
  "as_of": "2026-07-12",
  "method": "rules_v0",
  "sample_count": 3,
  "source_ids": ["sentiment-paste-a"],
  "evidence": [
    {
      "source_id": "sentiment-paste-a",
      "signal": "观望话术出现",
      "quote_span": "先观望吧，不敢追",
      "polarity": "FUD",
      "note": "规则命中：观望/不敢"
    }
  ],
  "triggers_fired": ["观望话术占一定比例"],
  "escape_reason": null,
  "llm_status": "not_invoked"
}
```

字段说明：

| 字段 | 类型 | 约束 |
|---|---|---|
| `index_level` | string | 封闭词表六选一 |
| `dominant_mode` | string | `FUD/FOMO/观望/混合/信息不足` |
| `confidence` | string | `低/中/高` |
| `sample_quality` | string | `可用/偏薄/不可用` |
| `as_of` | string | ISO 日期（样本窗或运行日） |
| `method` | string | 本期骨架：`rules_v0`；未来 LLM 路径：`llm_v0` / `rules+llm_v0` |
| `sample_count` | int | ≥ 0 |
| `source_ids` | string[] | 抽象 id 列表，禁止真实频道/账号名 |
| `evidence` | object[] | 0–N 条；每条至少含 `source_id` + `signal` |
| `triggers_fired` | string[] | 实际命中的可判定条件摘要 |
| `escape_reason` | string\|null | 仅当 `index_level=信息不足以分级` 时非空 |
| `llm_status` | string | `not_invoked` \| `reserved` \| `ok` \| `error` |

`evidence[].polarity` 若出现，只能是 `FUD | FOMO | 观望 | 中性 | 噪声`。

## 输入与适配器模式

### 第一版：手动粘贴文本适配器（唯一启用路径）

操作者把公开可见、自己有权使用的讨论摘录粘贴为本地文件，再交给脚本。
**不**实现任何自动爬取、登录抓取、或绕过平台 ToS 的采集。

推荐落盘位置（均 gitignored，与 D2/D6 一致）：

```
sources/raw/<source-id>/YYYY-MM-DD-sentiment-<slug>.md
```

最小 front-matter（手写或由未来适配器生成）：

```markdown
---
source: sentiment-paste-a
kind: sentiment_sample
observed_at: 2026-07-12
fetched_at: 2026-07-12T12:00:00+00:00
chars: 1234
---

（粘贴的讨论摘录正文……）
```

也支持无 front-matter 的纯文本：此时 `source-id` 取父目录名或 CLI
`--source-id` 参数，`observed_at` 回退为运行日。

CLI 形态（骨架）：

```bash
python3 scripts/sentiment_panel.py --smoke
python3 scripts/sentiment_panel.py --input path/to/sample.md
python3 scripts/sentiment_panel.py --input path/to/dir --source-id sentiment-paste-a
```

### 未来合规数据源预留

适配器接口按"输入样本集合 → 统一 sample 结构"解耦，不绑死采集方式：

```text
Sample = {
  source_id: str,      # 抽象 id
  text: str,
  observed_at: str,    # ISO date 或 datetime
  origin: str,         # paste | api | export | unknown
}
```

未来若存在**官方 API / 用户导出 / 明确允许的只读接口**，只需新增适配器
把数据变成 `Sample[]`，面板核心（分档 + 证据 + 协议）无需改动。
自动浏览器抓取社媒**默认关闭**；任何新采集路径必须先过下一节 ToS
评估并更新本文档，才能启用。

## 数据源 ToS / 风险评估（M2-prep 结论）

评估对象按**抽象类别**描述（D2：不写真实 server / 账号 / 组合 ID）。
结论服务于"第一版做什么 / 不做什么"，不是法律意见。

| 抽象类别 | 示例 config id 形态 | ToS / 合规风险（摘要） | M2-prep 决策 |
|---|---|---|---|
| 聊天室类频道 | `sentiment-room`（见 `config/sources.example.toml` 的 discord 段，`enabled=false`） | 多数聊天平台 ToS 限制自动化采集与非官方客户端；M0a 已决定接入暂缓 | **不实现**自动采集；可接受用户自行复制的公开/自有权限摘录（paste） |
| 短帖社交 A | `social-a` | 官方 API 有配额与自动化限制；非官方抓取高风险 | **不实现**爬虫；未来仅评估官方 API 或用户导出 |
| 投资论坛类 | `social-forum-a` | 站点条款通常禁止未经授权的批量抓取 | **不实现**爬虫；paste / 官方导出优先 |
| 短视频/图文社区 | `social-short-a` | 反爬与内容版权条款严格；自动化风险高 | **不实现**爬虫；本期不接入 |
| 手动摘录 | `sentiment-paste-a` | 用户对自己可见内容做本地笔记，风险最低 | **第一版唯一启用输入路径** |

硬约束（违反 = review 打回）：

1. 本 issue / 本骨架**不得**实现任何违反平台 ToS 的爬虫或非官方登录抓取。
2. 代码、文档、commit 中不得出现真实 server 名、频道名、账号名、组合 ID。
3. `sources/` 与 `journal/` 保持 gitignored；commit 前 `git check-ignore` 复验。

## 分档引擎：规则占位 + LLM 预留

### `rules_v0`（本期实现）

1. 读取样本 → 规范化空白 → 过短样本标记为噪声。
2. 用**预定义具名模式库**（封闭，不是开放 NLP）扫描 FUD / FOMO / 观望
   线索；模式写在脚本常量或未来独立数据文件中，禁止运行时"自由发挥"
   新标签。
3. 按命中计数映射到 `index_level`。**独立信号** = 模式库中互不相同的
   具名模式条目（例如 `追涨/怕踏空` 与 `投降/割肉` 各算 1 条）。
   文本长度只影响 `sample_quality` / `confidence`，**绝不**计为信号。
4. **硬规则**：任一具体分档（非逃生舱）要求独立信号 ≥ 2；否则
   `信息不足以分级`。样本质量 `不可用` 同样走逃生舱。
5. 组装 `evidence` / `triggers_fired` / `escape_reason`。

`rules_v0` 故意粗糙：目标是把**词表、协议、适配器缝、逃生舱**钉死，
而不是追求分类精度。精度留给后续规则迭代或 LLM 路径。

### `llm_v0`（预留，本期默认不调用）

- System prompt 数据文件：`prompts/sentiment_panel.md`（不硬编码进
  Python，见项目纪律）。
- 模型配置复用 `config/model.local.toml` 的方言缝（与
  `scripts/build_brief.py` 同构）。
- 模型**只能**在封闭词表内选择 `index_level` 等字段；输出必须是可
  `json.loads` 的对象。词表外取值 → 脚本侧拒绝并落入逃生舱或
  `ok:false`（实现可选，但不得静默改写为近义词）。
- CLI 预留 `--method rules|llm|rules+llm`；骨架中 `llm` 路径返回明确的
  `reserved` / 未配置提示，不在本期强制联网。

## 与每日 brief 的引用关系（前瞻，不在本期实现）

建议未来 brief 增加只读引用字段（需另开 issue 改 `BRIEF_SPEC` 与
prompt，**本期不改** brief 七段结构）：

```markdown
## 情绪面板（宝妈指数）
- 分档：`亢奋`（confidence=中, method=rules_v0, as_of=2026-07-12）
- 主模态：FOMO
- 证据：… [来源: sentiment-paste-a]
```

引用纪律：

- 只引用封闭词表内的 `index_level`，不改写为"偏热""情绪升温"等模糊词。
- 若面板输出 `信息不足以分级`，brief 必须原样保留逃生舱，不得脑补分档。
- 情绪分档**不是**交易指令；叠加入场仍属人类决策。

## 测试与验收

| 项 | 标准 |
|---|---|
| 词表封闭 | 单测覆盖六档合法值；非法值被拒绝 |
| 逃生舱 | 空输入 / 过短输入 → `信息不足以分级` 且 `escape_reason` 非空 |
| 边界 | 纯 FUD 模式 → 冰点或低迷；纯 FOMO → 亢奋或狂热；混合 → 中性 |
| 协议 | `--smoke` 打印 `{"ok": true, "data": …}`；失败走 `ok:false` |
| 隐私 | 夹具与文档只用抽象 id |
| ToS | 仓库内无社媒爬虫实现 |

## 非目标（本期明确不做）

- 实时流式采集、浏览器自动翻页抓社媒
- 下单、仓位、点位、止盈止损
- 修改 `prompts/daily_brief.md` 七段结构
- 引入 pip 依赖或检索索引
- 把 `sources/` / `journal/` 纳入版本库
