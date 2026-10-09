# MCP 接入说明

本项目**真实调用**麦当劳中国官方 MCP Server。本文档记录的是**实测结果**，不是对文档的复述——
文中所有字段名、单位、错误码都在真实调用中验证过。

---

## 1. MCP Server

| 项 | 值 |
| --- | --- |
| 服务提供方 | 麦当劳中国（官方） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 协议版本 | MCP `2025-06-18` |
| Token 申请 | <https://open.mcd.cn/mcp>（手机号登录 → 控制台 → 激活 → 复制） |
| 客户端库 | 官方 `mcp` Python SDK（本项目在 `mcp>=2.0` 与 `mcp<2.0` 上均做了传输层兼容） |

配置文件见仓库根目录 [`mcp-config.example.json`](./mcp-config.example.json)，
其中仅使用环境变量占位符 `${MCD_MCP_TOKEN}`，**不含任何真实凭证**。

> ⚠️ **实测提醒**：MCP Token 与大模型 API Key 是两种东西。
> 把 LLM Key 当 MCP Token 用会返回 `400008 当前authToken不允许超过64位长度`（MCP Token 为 32 位）。

---

## 2. 使用的 Tools

### 2.1 数据采集

| Tool | 用途 | 实测要点 |
| --- | --- | --- |
| `now-time-info` | 当前时间 / 时段 | 返回纯 JSON |
| `query-nearby-stores` | 定位门店 | **必须同时**传 `city` 与 `keyword`，只给一个报 `600058`；`beType=2` 会报 `600046 仅支持到店和得来速` |
| `query-meals` | 当前门店可售餐品 | 议会讨论的**唯一合法菜单来源**。`categories[].meals[]` 只带 `code`/`tags`，名称与价格在独立的 `meals{code:{name,currentPrice}}` 映射里；`currentPrice` 单位是**元** |
| `query-meal-detail` | 套餐组成与换配项 | 返回 `modification.items[].values[]`，含 `code` + `key` |
| `list-nutrition-foods` | 营养成分 | 返回 **toon 紧凑表格**（见 §3.4），且**不覆盖全部商品**（套餐常缺） |

### 2.2 优惠券

| Tool | 用途 | 实测要点 |
| --- | --- | --- |
| `available-coupons` | 可领取的券 | Markdown 文本 |
| `auto-bind-coupons` | 一键领券 | JSON |
| `query-store-coupons` | 门店可用券 | Markdown，**带 `couponId` / `couponCode`** |
| `query-my-coupons` | 账户已有券 | Markdown，**不含任何 ID**（见 §3.5） |

> **实测结论：三类券接口都不返回满减门槛与优惠金额。**
> 所以本项目**不猜优惠**——真实的优惠金额一律以 `calculate-price` 的返回为准。

### 2.3 算价与下单

| Tool | 用途 | 实测要点 |
| --- | --- | --- |
| `calculate-price` | **核心**：候选组合真实试算 | items 元素字段是 **`productCode`**（不是 `code`/`mealCode`）；返回金额单位是**分**；返回 `productList[]`、`takeWayList[]`、`enjoyed`、`enjoyable` |
| `create-order` | 创建订单 | `orderType=1` 时 **`takeWayCode` 必传**（漏传报 `600042`）；返回 `orderId` / `payId` / **`payH5Url`** |
| `cancel-order` | 撤销未支付订单 | `orderId` + **`cancelReasonCode`** 均必填（漏传报 400） |
| `query-order` | 查询订单状态 | 仅需 `orderId` |

### 2.4 账户与其他

| Tool | 用途 |
| --- | --- |
| `query-my-account` | 积分（`availablePoint` 等，**实测为字符串**） |
| `campaign-calendar` | 当月活动日历（Markdown） |

---

## 3. 实测踩到的坑（接入层已全部处理）

这一节是本项目的接入层为什么长这样的原因。

### 3.1 字段名错误不报错，只静默返回 0

```jsonc
// ✗ 不报错，price 静默返回 0
{"items": [{"mealCode": "1440", "quantity": 1}]}
{"items": [{"code":     "1440", "quantity": 1}]}

// ✓ 正确（实测：1440 → price=2350 分，即 ¥23.5）
{"items": [{"productCode": "1440", "quantity": 1}]}
```

没有异常、没有错误码，只有 0 —— 这是本项目排查时间最长的一个坑。

### 3.2 金额单位不统一

| 接口 | 字段 | 单位 |
| --- | --- | --- |
| `query-meals` | `currentPrice` | **元**（字符串） |
| `calculate-price` | `price` / `originalPrice` / `discount` / `productPrice` | **分**（整数） |
| `calculate-price` | `enjoyed.realDiscount` / `enjoyable.realDiscount` | **元**（浮点，例外） |

接入层统一换算为元，内部只流转元。混用会让"29 元套餐"变成"0.29 元"。

### 3.3 到店下单必须带 `takeWayCode`

`takeWayCode` 的值**只能**从 `calculate-price` 的 `takeWayList[].code` 中取：

```jsonc
"takeWayList": [
  {"code": "eat-in",        "title": "堂食", "subtitle": "店内用餐"},
  {"code": "take-in-store", "title": "外带", "subtitle": "店内自提"}
]
```

实测时序：`calculate-price`（拿 `takeWayList`）→ `create-order`（带 `takeWayCode`）→ 得到 `payH5Url`。

### 3.4 返回格式有四种，不能直接 `json.loads`

| 格式 | 例子 |
| --- | --- |
| 纯 JSON | `query-my-account` |
| 说明文字 + JSON | `query-nearby-stores`、`calculate-price` |
| Markdown | `available-coupons`、`campaign-calendar`、`query-my-coupons` |
| toon 紧凑表格 | `list-nutrition-foods` |

`calculate-price` 会在 JSON 前贴一大段字段说明，`json.loads` 整串必然失败，
必须用 `json.JSONDecoder().raw_decode()` 从第一个 `{` 开始解析。

toon 表格形如（`[160]` 是条数；表头行不以 `{` 开头，需单独处理）：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
```

### 3.5 `query-my-coupons` 完全没有 `couponId`

它的返回是一份**给人看的清单**：

```markdown
## 9.9元中杯冰美式
- **优惠**: ¥9.9 (用券价格)
- **有效期**: 2026-10-09 00:00-2026-10-15 23:59
- **标签**: 到店专用、外送专用
```

没有 `couponId`，也没有 `couponCode`。只有 `query-store-coupons` 才带标识。
本项目因此把券分成两类：

* `kind="mcp"` —— 拿到真实 ID，可以传回官方接口
* `kind="catalog"` —— 只有名字，**仅用于展示与话术，不作为参数传回**

### 3.6 `list-nutrition-foods` 不覆盖全部商品

套餐类常查不到营养数据。**绝不能把缺失当成 0**：
否则"热量 < 200kcal"这类硬约束会把整类商品误杀，表现为
"菜单里明明有汉堡，求解器却说没有主食可选"。

接入层的处理：数据缺失时跳过热量硬约束、只做轻微扣分，
并在结论中显式说明"官方营养表未覆盖，本单营养约束无法完整核验"。

### 3.7 真机分类名是中文

`query-meals` 的分类名是"人气热卖""超值套餐"这类中文且不稳定的字符串。
早期代码用 `category in ("burger", "combo")` 判断主食，**在真实模式下永远不成立**，
导致求解器认为菜单里没有主食。现已在领域模型里做关键词归一化（`MenuItem.kind`）。

---

## 4. 调用时序

```
你的需求
   │
   ▼
① now-time-info ─────────────── 时段
② query-nearby-stores ───────── 门店（city + keyword 必须同时给）
③ query-meals ───────────────── 菜单全集（角色不得编造餐品）
④ list-nutrition-foods ──────── 营养（可能不覆盖套餐）
⑤ auto-bind-coupons
   → query-my-coupons
   → query-store-coupons ────── 券池（只有 store 券带 ID）
⑥ campaign-calendar ─────────── 当日活动
⑦ query-my-account ──────────── 积分
   │
   ▼
⑧ 五位委员基于以上真实数据提案（第 1 轮）
   │
   ▼
⑨ 求解器枚举候选 → 本地粗排 Top-K
   │
   ▼
⑩ calculate-price × K ───────── 真实试算（同时拿到 takeWayList）
   │
   ▼
⑪ 委员拿着真实金额交叉质询（第 2 轮）
   │
   ▼
⑫ 精算追加：是否存在"多买一件反而更便宜"的解？
   │
   ▼
⑬ 输出决议（终端 + 决议卡 + JSON）
   │
   ▼
⑭ 你确认后 → create-order（带 takeWayCode）→ payH5Url
```

**注意第 ⑩ 步排在 ⑪ 之前**：委员先看到官方算出来的真实金额再吵架，
这样每一句反驳都能落到具体数字上，而不是空对空的人格表演。

---

## 5. 关键设计约束

1. **菜单零幻觉** —— 委员提案中的餐品编码必须存在于 `query-meals` 的返回结果中，
   不在菜单里的会被求解器直接剔除。
2. **金额零估算** —— 展示的应付、优惠、"再凑多少更划算"一律来自 `calculate-price`；
   模型只生成候选组合，不参与算钱。
3. **不代付** —— `create-order` 仅在用户显式加 `--order` 时调用，
   返回的是**麦当劳官方**支付链接；本项目不接触、不保存任何支付凭证。
4. **限流友好** —— 菜单 / 营养 / 券数据在单次会话内本地缓存（TTL 300s），
   同一 tool + 同参数不重复请求；遇 429 做指数退避重试。
5. **降级透明** —— `calculate-price` 失败时退回本地按券规则推算，
   并在输出中**显式标注 `local-fallback`**，绝不以官方价格的名义展示估算值。
6. **约束诚实** —— 目标达不成就直说，不偷偷放宽阈值；
   营养数据缺失就说"无法核验"，不拿 0 当真实值。
7. **失败显式** —— 单个 tool 失败会记录到 warnings 并继续，
   但**不会伪造数据填补**；关键路径（菜单为空）直接报错退出。

---

## 6. 离线演示与真实模式的关系

`--demo` 模式**不返回 Python 对象**，而是返回与真实服务器**同构的原始报文**：
包括那段啰嗦的 `# API Response Information` 前缀、Markdown 券清单、toon 营养表。
金额口径同样照抄真机——菜单是元，算价是分。

这样做的好处是：**解析层的 bug 在离线阶段就会暴露，不会等到配好 Token 才炸**，
而且 README 里的演示输出与真实输出结构完全一致，不存在"演示画大饼"。

---

## 7. 业务价值

- **对用户**：把"今天吃什么"从"翻菜单 + 比价 + 算券"压缩成一句话，
  而且结果可核对（每一分钱都能追溯到官方试算）、可直接下单。
- **对麦当劳**：把菜单、券、营养、活动四类 MCP 能力组合成一个高频刚需场景，
  提升领券使用率与积分兑换率；同时全程不产生越权操作。
- **对生态**：演示了 MCP 从"单工具调用"走向"多角色编排 + 约束求解"的进阶用法，
  以及如何把一个**格式不统一、字段易踩坑**的真实 MCP Server 稳稳接住。
