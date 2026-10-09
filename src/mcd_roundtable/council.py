"""议会编排引擎。

一次完整的"例会"分四段：

1. **提案** —— 每位委员基于真实菜单给出方案
2. **质询** —— 委员看到对手方案后互相反驳（这是"吵起来"的部分）
3. **真实试算** —— 求解器对 Top-K 组合逐个调用官方 `calculate-price`
4. **精算追加** —— 若发现"再花 3 块反而更便宜"，由求解器当场改写决议

大脑有两种：

* ``heuristic`` —— 规则驱动，零 API Key，任何人都能立刻跑出完整效果
* ``llm``       —— 调用任意 OpenAI 兼容端点生成话术，失败自动回退
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .mcp_client import McdData
from .models import Constraint, Proposal, Rebuttal, Verdict
from .optimizer import Optimizer, Plan
from .roles import Role, propose_heuristically

__all__ = [
    "CouncilError",
    "CouncilResult",
    "HeuristicBrain",
    "LLMBrain",
    "build_brain",
    "convene",
    "parse_constraint",
]

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_KEYWORD_RULES = {
    "吃饱": "饱腹",
    "管饱": "饱腹",
    "少油": "少油",
    "不油": "少油",
    "太油": "少油",
    "清淡": "少油",
    "辣": "辣",
    "减脂": "低卡",
    "控卡": "低卡",
    "低卡": "低卡",
    "增肌": "高蛋白",
    "蛋白": "高蛋白",
    "健身": "高蛋白",
    "便宜": "省钱",
    "省钱": "省钱",
    "划算": "省钱",
    "券": "省钱",
}


class CouncilError(RuntimeError):
    """议会无法召开。"""


# --------------------------------------------------------------------------- #
# 需求解析
# --------------------------------------------------------------------------- #

def parse_constraint(query: str, people: int | None = None) -> Constraint:
    """把一句中文需求拆成结构化约束。

    刻意写得"贪心但保守"：宁可少解析，也不要错误解析——
    错误解析出来的预算会把整场会议带偏。
    """
    text = (query or "").strip()
    c = Constraint(raw_query=text)

    explicit_people = False
    if people:
        c.people = max(1, int(people))
        explicit_people = True
    else:
        m = re.search(r"(\d+)\s*(?:个)?人", text)
        if m:
            c.people = max(1, int(m.group(1)))
            explicit_people = True
        else:
            for cn, v in _CN_NUM.items():
                if f"{cn}人" in text or f"{cn}个人" in text:
                    c.people = v
                    explicit_people = True
                    break

    # 只有用户没给具体人数时，才从"团队 / 同事 / 大家"推断
    if not explicit_people and any(w in text for w in ("团队", "同事", "大家")):
        c.people = max(c.people, 4)

    m = re.search(r"人均\s*(?:¥|￥)?\s*(\d+(?:\.\d+)?)", text)
    if m:
        c.budget_per_person = float(m.group(1))
    else:
        m = re.search(r"(?:预算|不超过|最多|控制在)\s*(?:¥|￥)?\s*(\d+(?:\.\d+)?)", text)
        if m:
            c.budget_total = float(m.group(1))
        else:
            # "30 以内" / "25 块以内" / "40 元以下" —— 带不带单位都认。
            # ⚠️ 口径要和下面的兜底分支一致：多人时这个数是**整单预算**。
            # 旧写法无条件写进 budget_per_person，于是
            # 「三个人 100 块以内」= 人均 100（整单 300），
            # 而「三个人 100 块」= 整单 100 —— 同一句话差三倍。
            m = re.search(r"(\d+(?:\.\d+)?)\s*(?:块|元|¥|￥)?\s*(?:以内|以下|之内)", text)
            if m and 5 <= float(m.group(1)) <= 500:
                if c.people > 1:
                    c.budget_total = float(m.group(1))
                else:
                    c.budget_per_person = float(m.group(1))
            else:
                # 兜底："两个人 100 块"
                m = re.search(r"(\d+(?:\.\d+)?)\s*(?:块|元)", text)
                if m and 5 <= float(m.group(1)) <= 2000:
                    if c.people > 1:
                        c.budget_total = float(m.group(1))
                    else:
                        c.budget_per_person = float(m.group(1))

    m = re.search(r"(\d+)\s*(?:kcal|千卡|大卡)", text, re.IGNORECASE)
    if m:
        c.max_kcal_per_person = float(m.group(1))

    m = re.search(r"蛋白(?:质)?\s*(\d+)\s*(?:g|克)?\s*(?:以上|起)", text)
    if m:
        c.min_protein_per_person = float(m.group(1))

    for word, tag in _KEYWORD_RULES.items():
        if word not in text:
            continue
        # 「不辣 / 别辣 / 免辣 / 去辣」说的是**忌口**，不是"要辣"。
        # 按关键字直接命中会把忌口读成需求，方向正好相反，
        # 而这是最危险的一类误解析：用户越强调不要，结果越给辣的。
        # 「少辣 / 微辣」则仍算"要辣"（只是降档），不在此列。
        if word == "辣" and re.search(r"[不别免无去忌]\s*辣", text):
            if "辣" not in c.avoid:
                c.avoid.append("辣")
            continue
        if tag not in c.keywords:
            c.keywords.append(tag)

    # 忌口解析：不要贪心。
    # 旧写法 `([\u4e00-\u9fa5]{1,6})` 对「不要可乐和薯条」会整串抓成
    # 「可乐和薯条」——结果是**两样都没被排除**，因为没有任何商品叫这个名字。
    # 正确做法是先截到标点为止，再按并列连词切开。
    #
    # 中文在这里天然有歧义：「和」既是连词也是词首（和风沙拉、明治醇壹…）。
    # 判据用"切出来的第一段是否为空"：`不要可乐和薯条` → `['可乐','薯条']` 首段非空，
    # 说明「和」是连词；`不要和风沙拉` → `['','风沙拉']` 首段为空，
    # 说明「和」属于品名，此时不切。宁可少排除一样，也不能把品名切坏。
    for m in re.finditer(
        r"(?:不要|不吃|别放|别加|不加|避开|忌)\s*([^，。；！？,.;!?\s]{1,14})", text
    ):
        chunk = m.group(1)
        parts = re.split(r"[、,，/]|以及|[和与跟]", chunk)
        if parts and not parts[0]:
            parts = [chunk]
        for part in parts:
            # 「不要辣的」「不吃油炸的」里的"的"是语气词，不是品名的一部分。
            # 不去掉的话，忌口词会变成"辣的"，而菜单上叫"麦辣鸡腿堡"——
            # 子串匹配永远不成立，忌口就静默失效了。
            part = part.strip().rstrip("的")
            if part and part not in c.avoid:
                c.avoid.append(part)

    if any(w in text for w in ("外送", "外卖", "送到", "配送", "宅急送")):
        c.takeout = False

    return c


# --------------------------------------------------------------------------- #
# 大脑
# --------------------------------------------------------------------------- #

class HeuristicBrain:
    """规则驱动，无需任何外部服务。"""

    label = "规则"

    async def __aenter__(self) -> "HeuristicBrain":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def proposals(self, roles, data: McdData, c: Constraint, ctx: dict) -> list[Proposal]:
        return [propose_heuristically(r, data.menu, data.coupons, c, ctx) for r in roles]

    async def rebuttals(self, proposals, c: Constraint, ctx: dict) -> list[Rebuttal]:
        return _heuristic_rebuttals(proposals, c, ctx)


class LLMBrain:
    """调用任意 OpenAI 兼容端点生成话术；任何异常都回退到规则大脑。"""

    label = "LLM"

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._fallback = HeuristicBrain()
        self._client = None
        self.degraded = False

    async def __aenter__(self) -> "LLMBrain":
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=45.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.close()

    async def _chat(self, system: str, user: str) -> str | None:
        assert self._client is not None
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.85,
                max_tokens=300,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception:  # noqa: BLE001
            self.degraded = True
            return None

    async def proposals(self, roles, data: McdData, c: Constraint, ctx: dict) -> list[Proposal]:
        menu_brief = _menu_brief(data.menu)
        results: list[Proposal] = []
        for role in roles:
            base = propose_heuristically(role, data.menu, data.coupons, c, ctx)
            system = (
                f"{role.persona}\n"
                "你正在参加「麦门圆桌」——一场关于这顿麦当劳怎么点的决策会议。"
                "用中文发言，不超过 55 字，语气贴你人设，"
                "给出你的方案 + 最关键的一个理由。"
                "不要贬低任何人、不要贬低任何品牌、不要劝人多花钱。"
            )
            user = (
                f"用户需求：{c.raw_query or '（无额外说明）'}\n"
                f"人数：{c.people}\n"
                f"可用券：{_coupon_brief(data.coupons)}\n"
                f"门店菜单（**只能从这里选**，不得编造菜品）：\n{menu_brief}\n\n"
                f"你的初步倾向是：{' + '.join(l.name for l in base.line_items)}"
                f"（合计约 ¥{base.amount:.1f}）。"
                "你可以沿用，也可以换成菜单里更符合你立场的组合。"
                "只输出一行发言文本，不要 JSON，不要引号。"
            )
            text = await self._chat(system, user)
            if text:
                base.pitch = text.strip().strip('"').strip("「」")
            results.append(base)
        return results

    async def rebuttals(self, proposals, c: Constraint, ctx: dict) -> list[Rebuttal]:
        return _heuristic_rebuttals(proposals, c, ctx)


def build_brain(mode: str):
    """根据配置构造大脑；缺 Key 自动降级为规则模式（并会在结论里说明）。"""
    if mode == "heuristic":
        return HeuristicBrain()

    api_key = os.environ.get("ROUNDTABLE_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return HeuristicBrain()

    return LLMBrain(
        api_key=api_key,
        base_url=os.environ.get("ROUNDTABLE_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        model=os.environ.get("ROUNDTABLE_LLM_MODEL", "deepseek-chat"),
    )


# --------------------------------------------------------------------------- #
# 质询（规则版）：用「账」说话，而不是用情绪
# --------------------------------------------------------------------------- #

def _heuristic_rebuttals(proposals: list[Proposal], c: Constraint, ctx: dict) -> list[Rebuttal]:
    """质询轮：**用账说话，而不是用情绪**。

    所有数字都来自上一步的真实试算（ctx['quotes']），
    所以这些话不是人格表演，是可以对着账单核对的。
    """
    if len(proposals) < 2:
        return []

    out: list[Rebuttal] = []
    by_key = {p.role_key: p for p in proposals}
    quotes: list = ctx.get("quotes") or []
    chosen_quote = ctx.get("chosen_quote")

    def add(src: str, tgt: Proposal | None, text: str) -> None:
        # 不许对自己开火：委员互相质询才叫质询，自问自答是 bug
        if tgt is not None and tgt.role_key == src:
            tgt = None
        if tgt is None and len(out) and out[-1].role_key == src:
            return  # 连续两条同一人发言，第二条没有信息量
        out.append(
            Rebuttal(src, _ROLE_NAMES.get(src, src), tgt.role_key if tgt else None,
                    tgt.role_name if tgt else None, text)
        )

    cheapest = min(proposals, key=lambda p: p.amount)
    priciest = max(proposals, key=lambda p: p.amount)
    protein_king = max(proposals, key=_sum_protein)
    cheapest_protein = min(proposals, key=_sum_protein)
    saltiest = max(proposals, key=_sum_sodium)
    people = max(1, c.people)

    # ① 省钱部长：拿"官方真实试算"当武器，这是最有杀伤力的一句
    if quotes and chosen_quote is not None:
        cheapest_payable = min(t.payable for _, t in quotes)
        target = priciest if priciest.role_key != "saver" else None
        add(
            "saver",
            target,
            f"官方试算跑了 {len(quotes)} 组，最便宜的方案能到 ¥{cheapest_payable:.1f}"
            f"（暂定采纳的是 ¥{chosen_quote.payable:.1f}）。"
            + (
                f"你这份提案 ¥{priciest.amount:.1f} 明显在上面，"
                if target is not None and priciest.amount > cheapest_payable + 2
                else "各方案差距不大，"
            )
            + "多花的钱买到了什么，请用数字回答，我这边只看结果。",
        )

    # ② 健身总监：蛋白质缺口（同样换算成人均）
    if cheapest_protein.role_key != "macro" and _sum_protein(cheapest_protein) > 0:
        gap = (_sum_protein(protein_king) - _sum_protein(cheapest_protein)) / people
        if gap >= 4:
            add(
                "macro",
                cheapest_protein,
                f"你这套人均蛋白质 {_sum_protein(cheapest_protein) / people:.0f}g，"
                f"换一组能多拿 {gap:.0f}g，价格还未必更贵。"
                "我不谈口味，只谈结构，这一点我不让步。",
            )

    # ③ 养生专员：钠（换算成人均，否则多人场景数字会大到失去意义）
    if _sum_sodium(saltiest) > 0:
        per_sodium = _sum_sodium(saltiest) / people
        add(
            "light",
            saltiest,
            f"你这份人均钠约 {per_sodium:.0f}mg"
            + (f"（整单 {_sum_sodium(saltiest):.0f}mg）" if people > 1 else "")
            + "。一顿别把一天的额度一次交出去——钠是慢慢还的账。",
        )

    # ④ 麦门老饕：反对把成熟搭配拆散
    if "purist" in by_key:
        add(
            "purist",
            None,
            "换配可以谈，但别把一套成熟搭配拆得面目全非——"
            "它被留下来，是因为它本来就成立。",
        )

    # ⑤ 尝鲜委员：挑战老饕
    if "novelty" in by_key and "purist" in by_key:
        add(
            "novelty",
            by_key["purist"],
            "老饕，你上次换口味是什么时候？菜单不是博物馆。"
            "同一份点第四次，点的是惯性不是胃口。",
        )

    # ⑥ 官方给出的"差一点就能用券"——这是真金白银，最有说服力
    #
    # 净收益 = 跨档后能拿到的优惠 − 现在已经拿到的优惠 − 为跨档多花的钱。
    # 三项少减任何一个，都会把"多花 8 块换来 5 块优惠"念成"净降 5 块"——
    # 而这句话是要拿去做购买决策的。口径必须与 card.py / html_report.py /
    # render.py 完全一致：`next_saving - discount - next_gap`，且只在 > 0 时才说。
    if chosen_quote is not None and chosen_quote.next_gap > 0:
        net = round(
            chosen_quote.next_saving - chosen_quote.discount - chosen_quote.next_gap, 2
        )
        if net > 0.01:
            add(
                "saver",
                None,
                f"官方接口显示，再点 ¥{chosen_quote.next_gap:.1f} 就能换更高一档优惠，"
                f"实付净降 ¥{net:.1f}。这不是我算的，是官方试算给的。这一步不补，等于把券退回去。",
            )

    # ⑦ 精算追加生效时，由主持人点名，这里只让老饕表态
    if ctx.get("topped_up") and "purist" in by_key:
        add(
            "purist",
            None,
            "加一份小食反而更便宜——这种账我以前不信，现在我认了。"
            "但前提是官方试算说了算。",
        )

    return out[:5]


_ROLE_NAMES = {
    "saver": "省钱部长",
    "macro": "健身总监",
    "purist": "麦门老饕",
    "light": "养生专员",
    "novelty": "尝鲜委员",
}


def _sum_protein(p: Proposal) -> float:
    return sum(_PROTEIN_HINT.get(line.name, 0.0) * line.qty for line in p.line_items)


def _sum_sodium(p: Proposal) -> float:
    return sum(_SODIUM_HINT.get(line.name, 0.0) * line.qty for line in p.line_items)


_PROTEIN_HINT: dict[str, float] = {}
_SODIUM_HINT: dict[str, float] = {}


def register_nutrition(menu) -> None:
    """把菜单里的营养数据喂给质询话术，避免硬编码。"""
    _PROTEIN_HINT.clear()
    _SODIUM_HINT.clear()
    for item in menu:
        if item.protein:
            _PROTEIN_HINT[item.name] = item.protein
        if item.sodium:
            _SODIUM_HINT[item.name] = item.sodium


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #

@dataclass
class CouncilResult:
    constraint: Constraint
    proposals: list[Proposal]
    rebuttals: list[Rebuttal]
    verdict: Verdict
    data: McdData
    brain_label: str = "规则"
    warnings: list[str] = field(default_factory=list)
    topped_up: str = ""
    brain_degraded: bool = False


async def convene(
    data: McdData,
    constraint: Constraint,
    roles: list[Role],
    *,
    brain,
    rounds: int = 1,
    objective: str = "cost",
) -> CouncilResult:
    """召开一次完整会议。

    关键顺序设计：**真实试算排在质询之前**。
    委员先看到官方算出来的真金白银，再开口吵架——
    这样每一句反驳都能落到一个真实数字上，而不是空对空的人格表演。
    """
    register_nutrition(data.menu)
    ctx: dict = {"coupons": data.coupons}

    # ---- 第 1 轮：提案 ----
    proposals = await brain.proposals(roles, data, constraint, ctx)
    proposals = [p for p in proposals if p.line_items]
    if not proposals:
        # 不要只说"菜单可能是空的"。采集层早就把第一手原因（例如
        # 「门店「麦当劳郑州CCD奥体中心餐厅」不可点单：门店可能已关闭或不在营业时间」）
        # 记进 warnings 了，把它带出来。
        #
        # 否则用户看到"菜单或营养数据可能为空"，会跑去菜单和解析层找 bug，
        # 而真正的原因（那家店 22:00 打烊了）一个字都没提 —— 这正是本项目
        # 最反对的"静默降级"：报了一个错误的原因，比不报错还费时间。
        detail = "；".join(data.warnings[:3]) or "菜单与营养数据均为空，且未记录到具体原因"
        raise CouncilError("所有委员都没能给出方案：" + detail)

    # ---- 真实试算（官方金额，任何话术都不能覆盖它）----
    optimizer = Optimizer(data, constraint, objective=objective)
    pool = optimizer.build_pool(proposals)
    plans = optimizer.enumerate_plans(pool)

    if not plans:
        # 预算或约束过紧：退化为"最接近"的单品，并诚实说明
        if not pool:
            detail = "；".join(data.warnings[:3])
            raise CouncilError(
                "菜单为空，无法给出建议。请检查门店参数，或加 --demo 体验完整流程。"
                + ("（%s）" % detail if detail else "")
            )
        plans = [Plan([(pool[0], constraint.people)], round(pool[0].price * constraint.people, 2), 0.0)]

    verified = await optimizer.verify(plans)  # 已按「本地分 + 真实省下的钱」排序
    plan, quote = verified[0]
    trials = optimizer.to_trials(verified, winner=0)

    # ---- 精算追加：如果"多花一点反而更便宜"，当场改写决议 ----
    plan, quote, extra_trial, topped_up = await optimizer.optimize_topup(plan, quote)
    if extra_trial is not None:
        trials.append(extra_trial)

    # ---- 第 2 轮：质询（此时手里已经有真实金额）----
    ctx.update(
        {
            "quotes": verified,
            "chosen_plan": plan,
            "chosen_quote": quote,
            "topped_up": topped_up,
            "objective": objective,
        }
    )
    rebuttals: list[Rebuttal] = []
    for _ in range(max(1, min(rounds, 3))):
        rebuttals.extend(await brain.rebuttals(proposals, constraint, ctx))

    _check_achievement(quote, data, constraint)

    adopted, rejected, summary = _judge(proposals, quote)
    if topped_up:
        summary = f"{summary} {topped_up}"

    verdict = Verdict(
        quote=quote,
        adopted=adopted,
        rejected=rejected,
        summary=summary,
        per_person=quote.per_person,
        trials=trials,
        headcount_label=headcount_label(constraint),
        why_not_cheapest=_explain_choice(plan, quote, verified, data, constraint.people),
    )

    return CouncilResult(
        constraint=constraint,
        proposals=proposals,
        rebuttals=rebuttals,
        verdict=verdict,
        data=data,
        brain_label=brain.label,
        warnings=list(data.warnings),
        topped_up=topped_up,
        brain_degraded=bool(getattr(brain, "degraded", False)),
    )


def headcount_label(constraint: Constraint) -> str:
    return f"{max(1, constraint.people)} 人"


def _plan_nutrition(plan, data: McdData) -> tuple[float, float, float]:
    """按官方营养表估算一份方案的 (热量, 蛋白质, 钠)。仅用于解释性文案。"""
    kcal = protein = sodium = 0.0
    for item, qty in getattr(plan, "items", []) or []:
        kcal += item.kcal * qty
        protein += item.protein * qty
        sodium += item.sodium * qty
    return kcal, protein, sodium


def _explain_choice(plan, quote, verified, data: McdData, people: int = 1) -> str:
    """如果最终没选最便宜的那组，把理由说清楚。

    一个"什么都说不清就替你选贵的"的工具是没法信的。
    这里只在能给出**量化理由**时才解释，给不出就干脆不说。
    """
    if len(verified) < 2:
        return ""

    cheapest_plan, cheapest_quote = min(verified, key=lambda pq: pq[1].payable)
    # 注意符号：是"本单比最便宜的贵多少"，不是反过来
    gap = round(quote.payable - cheapest_quote.payable, 2)
    if gap <= 0.01:
        return ""  # 选的就是最便宜的，无需解释

    people = max(1, people)
    win_kcal, win_protein, _ = _plan_nutrition(plan, data)
    cheap_kcal, cheap_protein, _ = _plan_nutrition(cheapest_plan, data)
    d_kcal = (win_kcal - cheap_kcal) / people
    d_protein = (win_protein - cheap_protein) / people

    bits: list[str] = []
    if d_kcal >= 60:
        bits.append(f"人均多 {d_kcal:.0f}kcal 饱腹度")
    if d_protein >= 3:
        bits.append(f"人均多 {d_protein:.0f}g 蛋白质")
    if not bits:
        return ""
    return (
        f"本单比最便宜的方案（¥{cheapest_quote.payable:.1f}）多花 ¥{gap:.1f}，"
        f"换来 {'、'.join(bits)}。若你只要最低价，说一声我就换。"
    )


def _check_achievement(quote, data: McdData, constraint: Constraint) -> None:
    """约束达成度自检：没达成就**诚实说出来**，而不是假装满足。"""
    people = max(1, constraint.people)
    protein = kcal = sodium = 0.0
    covered = 0
    missing: list[str] = []
    for line in quote.lines:
        item = data.by_code(line.code)
        if item is None or not item.nutrition:
            missing.append(line.name)
            continue
        covered += 1
        protein += item.protein * line.qty
        kcal += item.kcal * line.qty
        sodium += item.sodium * line.qty

    # 预算校验放在"营养数据缺失"的判断**之前**。
    # 否则只要有一项查不到营养值就 return 了，实付超预算这件事会被一起吞掉——
    # 而超预算是用户最在意、也最不该被静默省略的告警。
    if constraint.effective_budget_total and quote.payable > constraint.effective_budget_total:
        data.warnings.append(
            f"实付 ¥{quote.payable:.1f} 超出预算 ¥{constraint.effective_budget_total:.1f}"
        )

    # 官方营养表并不覆盖全部商品（套餐常常查不到）。
    # 数据不足时说"无法核验"，而不是拿 0 当成真实值去报警。
    if missing and (constraint.min_protein_per_person or constraint.max_kcal_per_person):
        data.warnings.append(
            f"官方营养表未覆盖：{'、'.join(missing[:3])}，本单营养约束无法完整核验"
        )
        return

    per_protein, per_kcal = protein / people, kcal / people

    if constraint.min_protein_per_person and per_protein < constraint.min_protein_per_person:
        data.warnings.append(
            f"蛋白质约 {per_protein:.0f}g/人，未达 {constraint.min_protein_per_person:.0f}g 目标"
            "（在当前热量与预算上限下，这已是菜单里最接近的搭配）"
        )
    if constraint.max_kcal_per_person and per_kcal > constraint.max_kcal_per_person:
        data.warnings.append(
            f"热量约 {per_kcal:.0f}kcal/人，超出 {constraint.max_kcal_per_person:.0f}kcal 上限"
        )


def _judge(proposals: list[Proposal], quote) -> tuple[list[str], list[str], str]:
    """判定哪些委员的方案被采纳。

    ⚠️ 必须按 **餐品编码** 匹配，不能按名字——真机返回的官方品名
    与菜单里的展示名可能不完全一致，按名字匹配会出现"五个人全部未采纳"
    这种看起来像 bug 的结果。
    """
    code_to_name = {line.code: line.name for line in quote.lines}
    adopted, rejected = [], []
    for p in proposals:
        hits = [l.name for l in p.line_items if l.code in code_to_name]
        if hits:
            adopted.append(f"{p.role_name} → {'、'.join(hits)}")
        else:
            rejected.append(p.role_name)

    if not adopted:
        # 精算求解器可能给出一个谁都没提过的更优组合，这很正常，但要说清楚，
        # 而不是留一片空白让人以为结果出错了
        adopted = ["主持人依据精算结果定案（无单一委员方案被完整采纳）"]

    parts: list[str] = []
    total_qty = sum(l.qty for l in quote.lines)
    if len(quote.lines) > 1:
        parts.append(f"{len(quote.lines)} 类商品共 {total_qty} 件")
    if quote.discount > 0:
        parts.append(f"用券省 ¥{quote.discount:.1f}")
    else:
        parts.append("本单未触发优惠")
    parts.append(f"人均 ¥{quote.per_person:.1f}")

    return adopted, rejected, "，".join(parts) + "。"


def _menu_brief(menu) -> str:
    rows = []
    for item in menu[:26]:
        nut = item.nutrition or {}
        detail = ""
        if nut:
            detail = (
                f" | {nut.get('energyKcal', 0):.0f}kcal "
                f"蛋白{nut.get('protein', 0):.0f}g 钠{nut.get('sodium', 0):.0f}mg"
            )
        rows.append(f"- {item.code} {item.name} ¥{item.price:.1f}{detail}")
    return "\n".join(rows)


def _coupon_brief(coupons) -> str:
    if not coupons:
        return "（暂无可用券）"
    return "；".join(c.name for c in coupons)
