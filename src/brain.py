"""Gemini: отбор сюжетов + черновики постов.
Free tier Google AI Studio. Модель задаётся через GEMINI_MODEL.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# GEMINI_MODEL можно задать списком через запятую — пробуем по очереди.
# Актуальные имена для своего ключа: .\run.ps1 models
_DEFAULT_MODELS = "gemini-3.5-flash,gemini-3.6-flash,gemini-3.1-flash-lite"
MODELS = [m.strip() for m in os.getenv("GEMINI_MODEL", _DEFAULT_MODELS).split(",") if m.strip()]

_requests_made = 0
_successful_calls = 0    # реально вернувшие пригодный текст
_quota_refusals = 0      # HTTP 429 с "PerDay" - дневной лимит модели исчерпан
_day_exhausted: set[str] = set()   # модели с исчерпанной дневной квотой в этом прогоне
_last_model: str | None = None     # какая модель ответила на последний успешный _call (T9d)
_last_rank_degraded = False        # rank() отработал без модели, эвристическим top-N (T13a)


def rank_degraded() -> bool:
    """True, если последний rank() не смог достучаться до модели вообще (все модели
    исчерпаны/недоступны) и отдал эвристический top-N вместо ранжирования Gemini.
    main.py читает это сразу после brain.rank(), чтобы пропустить черновики
    целиком (T13a: строка матрицы "rank падает") и добавить пометку в сводку."""
    return _last_rank_degraded


def last_model_used() -> str | None:
    """Модель, реально вернувшая текст в последнем успешном _call(). main.py читает
    это сразу после brain.draft() - до того, как verify.py сделает свой запрос
    и перезапишет значение своим ответом."""
    return _last_model


def requests_made() -> int:
    """Сколько HTTP-запросов реально ушло в Gemini за этот прогон (для контроля квоты).
    Считает КАЖДУЮ попытку, включая повторы после 429/500/503 - это и есть расход
    против дневного лимита, не только успешные ответы."""
    return _requests_made


def quota_summary() -> dict:
    """Разбивка для лога: всего HTTP-попыток, сколько из них реально вернули текст,
    сколько получили отказ по дневному лимиту (429 "PerDay")."""
    return {"total": _requests_made, "successful": _successful_calls,
           "quota_refused": _quota_refusals}


def _call(prompt: str, as_json: bool = False, temperature: float = 0.7,
          max_tokens: int = 32768, retries: int = 3, no_thinking: bool = False) -> str:
    """Перебирает модели из MODELS. На 400 не долбит одним и тем же телом, а упрощает запрос.

    no_thinking: у thinking-моделей токены размышлений тратятся из maxOutputTokens,
    из-за чего JSON обрывается. Но Gemini 3 может запрещать полное отключение —
    тогда параметр снимается автоматически.
    """
    global _requests_made, _successful_calls, _quota_refusals, _last_model
    if os.environ.get("NEWSBOT_ALLOW_LIVE") != "1":
        raise RuntimeError(
            "живой вызов Gemini заблокирован: переменная NEWSBOT_ALLOW_LIVE не выставлена "
            "в 1. Её ставит только run.ps1 в режимах verify/models/dry/send - если ты "
            "видишь это в тестах или ручном прогоне, живые вызовы Gemini сейчас не разрешены.")
    key = os.environ["GEMINI_API_KEY"]
    last = "неизвестно"

    for model in MODELS:
        if model in _day_exhausted:
            log.warning("%s: дневная квота уже исчерпана в этом прогоне — пропускаю", model)
            last = f"{model}: дневная квота исчерпана"
            continue

        gen: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if as_json:
            gen["responseMimeType"] = "application/json"
        if no_thinking and not model.startswith("gemini-3"):
            gen["thinkingConfig"] = {"thinkingBudget": 0}

        attempt = 0
        while attempt < retries:
            attempt += 1
            body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
            try:
                r = requests.post(API.format(model=model),
                                  headers={"x-goog-api-key": key,
                                           "Content-Type": "application/json"},
                                  json=body, timeout=180)
                _requests_made += 1

                if r.status_code == 404:
                    log.warning("%s: модели нет — следующая", model)
                    break

                if r.status_code == 400:
                    detail = r.text[:300].replace("\n", " ")
                    # 400 детерминирована: упрощаем запрос, а не повторяем то же самое
                    if "thinkingConfig" in gen:
                        log.warning("%s: 400, снимаю thinkingConfig. Ответ: %s", model, detail)
                        gen.pop("thinkingConfig")
                        attempt -= 1          # это не потраченная попытка
                        continue
                    if "responseMimeType" in gen:
                        log.warning("%s: 400, снимаю responseMimeType. Ответ: %s", model, detail)
                        gen.pop("responseMimeType")
                        attempt -= 1
                        continue
                    log.warning("%s: 400 без вариантов упрощения. Ответ: %s", model, detail)
                    last = f"{model}: 400 {detail[:120]}"
                    break                      # к следующей модели

                if r.status_code == 429:
                    # "PerDay" в quotaId значит дневной лимит модели, а не всплеск
                    # запросов в минуту. Ждать бессмысленно - у следующей модели своя
                    # дневная квота, пробуем её сразу.
                    if "PerDay" in r.text:
                        log.warning("%s: дневная квота исчерпана, следующая модель", model)
                        last = f"{model}: дневная квота исчерпана"
                        _quota_refusals += 1
                        _day_exhausted.add(model)
                        break
                    wait = min(20 * (2 ** (attempt - 1)), 120)
                    log.warning("%s: HTTP 429 (в минуту), ждём %ss", model, wait)
                    last = f"{model}: HTTP 429"
                    time.sleep(wait)
                    continue

                if r.status_code in (500, 503):
                    wait = min(20 * (2 ** (attempt - 1)), 120)
                    log.warning("%s: HTTP %s, ждём %ss", model, r.status_code, wait)
                    last = f"{model}: HTTP {r.status_code}"
                    time.sleep(wait)
                    continue

                r.raise_for_status()
                cand = r.json().get("candidates", [{}])[0]
                text = "".join(x.get("text", "")
                               for x in cand.get("content", {}).get("parts", []))
                if cand.get("finishReason") == "MAX_TOKENS":
                    log.warning("%s: ответ обрезан по лимиту токенов", model)
                if text.strip():
                    _successful_calls += 1
                    _last_model = model
                    log.info("%s: ответ получен (%d симв.)", model, len(text))
                    return text
                last = f"{model}: пустой ответ ({cand.get('finishReason')})"
                log.warning(last)

            except Exception as exc:
                last = f"{model}: {exc}"
                log.warning("%s попытка %d: %s", model, attempt, str(exc)[:160])
                time.sleep(5 * attempt)

    if _day_exhausted >= set(MODELS):
        raise RuntimeError(
            f"дневная квота Gemini исчерпана на всех моделях ({', '.join(MODELS)}). "
            "Free tier сбрасывается около полуночи по тихоокеанскому времени "
            "(~09:00-10:00 UTC) - раньше запросы не пройдут.")
    raise RuntimeError(f"Gemini недоступен. Последнее: {last}")


def _parse_json(raw: str):
    """Парсит JSON, а если он обрезан — вытаскивает объекты, которые успели дописаться."""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    m = re.search(r"[\[{].*[\]}]", raw, re.S)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # Ответ оборвался. Собираем целые объекты верхнего уровня по балансу скобок.
    objs, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    objs.append(json.loads(raw[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None

    if objs:
        log.warning("JSON был обрезан — спасено объектов: %d", len(objs))
        return objs
    raise json.JSONDecodeError("не удалось спасти ничего", raw[:200], 0)


# ------------------------------------------------------------------
#  ЭТАП 1: отбор. Здесь главное — жёсткие критерии отбраковки.
# ------------------------------------------------------------------
RANK_PROMPT = """You are a ruthless editor helping a 2nd-year Finance & Accounting student
in Warsaw find material worth posting about on LinkedIn. He targets investment banking and
asset management, so credibility matters far more than reach.

Score each item 0-10 on ONE question: would a smart person who already follows markets
learn something non-obvious from a post about this?

Score 0-3 and REJECT if the item is any of these:
- daily price movement with no cause ("stocks rise as investors weigh...")
- a company announcing a partnership, integration, hiring, or rebrand
- funding round with no unusual structure or valuation logic
- a model/product release that is just "X scores better on benchmarks"
- opinion or prediction with no data behind it
- listicle, "top 10", "here's why", clickbait, or an aggregator rewriting another outlet
- a story everyone already posted about two days ago

Score 7-10 only if it has at least one of:
- a hard number that contradicts the common narrative
- a mechanism worth explaining (how a rule, market structure, or incentive actually works)
- a primary source: central bank, statistical office, regulator, filing, research paper
- a Poland or CEE angle international readers would miss
- a second-order consequence nobody is discussing yet

CRITICAL RULES:

1. SPREAD YOUR SCORES. Do not cluster everything at 8. Within any batch, use a real range.
   If two items are not equally good, they must not get the same score. Reserve 9-10 for
   items you would stake your reputation on; most passing items belong at 6 or 7.

2. NEVER INVENT FIGURES. You see only a title and a short snippet, not the article.
   State a specific number in "angle" ONLY if that exact number appears in the text above.
   Otherwise describe the claim without numbers. A fabricated figure is the worst possible
   outcome here, because it would be published under a real person's name.

3. BE TERSE. "angle" max 35 words. "why_nonobvious" max 25 words. This is one call scoring
   everything at once - long fields eat the output token budget and truncate the whole response.

4. NAME THE MECHANISM, NOT THE EFFECT. "angle" must point at a specific line item, rule, or
   process, not a generic label for one.
   BAD: "high operating leverage"
   GOOD: "interest income on uninvested client cash balances"
   If you can only describe the general effect, the score is below 7.

5. AUDIENCE. The audience is Polish finance people first, then people abroad who watch Poland
   and CEE, then AI stories read through an economic lens. Ask directly: would someone working
   in Polish finance raise an eyebrow at this? At equal quality, a story with a genuine Poland
   or CEE angle must score higher than an equally good abstract global story. Score AI stories
   on their economic consequence - capex, margins, the labor market, regulation - not on model
   novelty. Purely domestic US news with no consequence for Europe or Poland is a minus, not
   a neutral.

For each item, "angle" is what the post would ARGUE - a claim, not a topic. If you cannot
write a real claim, the score is below 7.

Return JSON only: an array of objects with keys id, score, title_en, angle, why_nonobvious.
"title_en" must be a clean, concise English translation/adaptation of the headline (max 10 words).
Include ONLY items scoring 6 or above. Returning 4 excellent items beats 20 mediocre ones.

ITEMS:
{items}
"""


def rank(items, top_n: int = 12) -> list[dict]:
    """Один вызов на весь список. Дефицит free tier - число запросов в день (20/модель),
    не токены, а вход в 1M токенов легко тянет сотню заголовков разом.

    Модель недоступна целиком (все модели исчерпаны/сеть легла) - вместо пустого
    списка отдаём эвристический top-N (T13a): `items` уже приходит отсортированным
    по main.heuristic_prefilter (вес источника, тег, свежесть, social) - это тот
    же порядок, без нового скоринга, просто урезанный до top_n. rank_degraded()
    сообщает вызывающему коду об этом, чтобы пропустить черновики целиком - без
    score/angle от модели строить черновик не на чем."""
    global _last_rank_degraded
    _last_rank_degraded = False
    scored: list[dict] = []
    by_id = {i: it for i, it in enumerate(items)}

    payload = "\n".join(
        f"id={i} | {by_id[i].source} | {by_id[i].tag} | social={by_id[i].social}\n"
        f"  {by_id[i].title}\n  {by_id[i].summary[:280]}"
        for i in by_id)
    try:
        res = _parse_json(_call(RANK_PROMPT.format(items=payload), as_json=True,
                                temperature=0.2, no_thinking=True))
        for row in res:
            idx = int(row["id"])
            if idx in by_id:
                row["item"] = by_id[idx]
                scored.append(row)
    except Exception as exc:
        log.warning("отбор упал: %s - эвристический top-N без модели", exc)
        _last_rank_degraded = True
        fallback = [{"item": it} for it in list(by_id.values())[:top_n]]
        log.info("эвристический top-N: берём %d из %d", len(fallback), len(by_id))
        return fallback

    for s in scored:
        s["final"] = float(s.get("score", 0)) * s["item"].weight
    scored.sort(key=lambda x: x["final"], reverse=True)
    log.info("прошли отбор: %d, берём %d", len(scored), min(top_n, len(scored)))
    return scored[:top_n]


# ------------------------------------------------------------------
#  ЭТАП 2: черновики. Антишлак-правила прописаны явно.
# ------------------------------------------------------------------
DRAFT_PROMPT = """Write exactly 2 LinkedIn post drafts in ENGLISH, in this fixed order:

DRAFT 1 - digest: a thematic roundup of 2-3 stories from the selection below, unified under a single macro principle. 120-170 words.
DRAFT 2 - single: one story, the strongest one, examined in depth - an institutional analytical note. Pick ONE shape for it (A. MECHANISM, B. TWO NUMBERS, C. COMMON BELIEF). 110-170 words.

The reader is choosing between the digest and the single post.

=== SYSTEM ROLE & PERSONA ===
You are an institutional macro and equity research analyst specializing in European banking, energy transition, Big Tech capital allocation, and market microstructure.
Your audience includes institutional asset managers, private equity associates, bank treasury desks, and buy-side analysts.
You write with sharp, cynical clarity, grounded entirely in balance-sheet realities, contract mechanics, and capital flows.
Never adopt an amateur, student, or retail persona. Never ask for correction or validation.

=== 1. HOOK RULES (LINE 1) ===
- Lead directly with the central economic conflict, asset repricing, or balance-sheet anomaly in sentence 1.
- If neither the first nor the second sentence contains a fact - a number, a name, a date, a specific event - drop both.
  BAD: "A few global and local financial developments that stood out this week."
  BAD: "Three market and policy developments stood out in the news this week."
- ABSOLUTELY BANNED OPENERS (Never use under any circumstances):
  * "What caught my eye..." / "A few developments caught my eye..."
  * "These two numbers/trends sit oddly next to each other..."
  * "Many investors assume..." / "Most retail investors think..." / "Everyone is watching..."
  * "In today's volatile market..." / "It is no secret that..."
- APPROVED HOOK PATTERNS:
  * Pure Data Divergence: "Poland’s credit-to-deposit ratio sits at 57.7% against an EU-27 median of 106.1%."
  * Balance-Sheet Conflict: "Hyperscalers don't have a power or chip problem—they have an underwriting concentration bottleneck."
  * Governance Discount: "If you want a live case study in minority shareholder extraction, look at Orlen’s latest capital allocation."
  * Regime Shift: "Quantitative momentum models aren't pricing growth; they are manufacturing endogenous liquidity risk."

=== 2. BANNED CLICHÉS & PHRASES (STRICT ZERO-TOLERANCE) ===
Never output any of the following expressions:
- Colloquialisms: "plumbing", "double whammy", "lost their shirts", "the house always wins", "selling shovels in a gold rush", "tip of the iceberg", "game-changer", "silver bullet".
- Dramatic one-line placeholders: "The reality is different.", "The timing is tricky.", "Here is the catch.", "The numbers are wild.", "This pressure is not a straight line."
- Pseudo-reflection & Amateur Persona: "As a student...", "As someone analyzing asset management...", "for a finance student", "as someone learning", "It makes you wonder...", "I am watching this space closely.", "Time will tell."
- Conversational filler: "Why? Because...", "Here's why:", "Why the massive gap?", "How did this happen? It's simple."
- Banned voice buzzwords: "leverage" (as buzzword), "synergy", "landscape", "paradigm", "unprecedented", "delve", "underscore", "pivotal", "robust", "it's not just X, it's Y", "here's the thing".
- False instant causation: Do not claim that one event caused another instantly unless the material establishes how fast the reaction actually was.
  BAD: "Yet this surge immediately reignited political debates."

=== 3. SYNTAX, RHYTHM & PACING ===
- Density over white space: Write in solid, cohesive paragraphs of 2 to 4 sentences. NEVER break every single sentence into a new line. No cheap LinkedIn typography tricks.
- Dynamic pacing: Interlock complex financial explanations (25–35 words) with punchy factual statements (6–10 words). At least one sentence in every draft must be under 8 words.
- Ban repetitive sentence structures. Never chain three sentences starting with "This [verb]..." or "They [verb]...".
- No emoji. Plain "-" bullets only, max 3.
- Hashtags: 0 or 1. Never generic ones (#finance #macroeconomics #GPW #forex). Prefer none.
- Word count: DRAFT 1 (digest) 120-170 words, DRAFT 2 (single) 110-170 words.
- No links in the body. Always a space after a period before the next sentence.

=== 4. TERMINOLOGY & DEPTH (MANDATORY VOCABULARY UPGRADES) ===
Never explain elementary finance definitions to the reader (e.g., do not explain what loan-to-deposit ratio or trade credit means). Replace all layperson descriptions with exact industry vocabulary:
- "Replacing old equipment / worn-out tech" -> Defensive maintenance capex vs net expansion capex
- "Insurance companies can't take data center risk" -> Underwriting capacity limits, P&C balance-sheet concentration risk
- "Startups buying chips with money from the chipmaker" -> Circular capex loop, vendor financing
- "Renting chips instead of buying" -> Take-or-pay compute contracts, off-balance-sheet capacity commitments
- "Exchange profits jump because costs are stable" -> Operational leverage on fixed-cost exchange infrastructure
- "Mining silver lowers the cost of digging copper" -> By-product credit accounting, C1 net cash cost reduction
- "Leveraged ETF losses from daily ups and downs" -> Compounding drag, beta slippage, volatility decay
- "All algorithms trying to exit at the same time" -> Factor crowding, systematic de-grossing, liquidity cascade
- "Government taking money from a public company" -> Quasi-fiscal extraction, governance discount, off-budget spending
- "Insurance claims growing faster than premiums" -> Combined ratio deterioration, loss ratio expansion, claims severity inflation
- "Companies giving long credit to buyers" -> Working capital cycle, cash conversion cycle, supplier-provided trade financing
- "Long-term power and lease deals not on the balance sheet" -> Power Purchase Agreements (PPAs), contractual fixed-charge commitments
- "Refinancing old mortgages with cheaper ones" -> Exercising prepayment options, negative convexity realization
- "Central bank interventions don't work long-term" -> Cross-currency basis arbitrage, structural policy rate differentials
- "Buying off-the-run bonds back" -> Programmatic Treasury buybacks, DV01 short squeeze, long-end term premia

=== 5. DIGEST STRUCTURE & LOGICAL LINKING ===
When writing DRAFT 1 (digest):
1. Frame the overarching thesis in Line 1 immediately (e.g., "Three sovereign balance-sheet interventions distorting capital allocation this week:").
2. Give every item a bold, analytical sub-header that identifies the exact mechanism:
   * "1. Quasi-fiscal extraction via state equity:"
   * "2. Operating leverage across defense procurement:"
   * "3. Foreign reserve burn against carry trade arbitrage:"
3. NEVER connect unrelated stories using lazy transitions ("Meanwhile...", "At the same time...", "Finally...").
4. The stories inside a single digest MUST share a unifying macro principle (liquidity hoarding, margin compression, fiscal dominance). If stories are unrelated, do not force them into a single post.

=== 6. STRUCTURAL CONCLUSIONS & CAPITAL MARKET IMPLICATIONS ===
- NEVER end with a question: "Who has the better strategy?", "What do you think?", "Thoughts?", "Agree?", "Will this hold?"
- NEVER end with a motivational or educational moral.
- the closing sentence must be a specific, concrete thing - not a
  sentence that restates what was just said in broader words.
  BAD: "These movements show how
  quickly regional cost structures and trade policy can reshape corporate performance."
  BAD: "These figures show how easily headline numbers can mask the underlying economic
  reality."
  BAD: "Political gridlock is not just a headline. It is a direct driver of bond market
  supply."
  If a closing sentence would fit equally well after three different, unrelated stories, it is not concrete enough - end on the last number, name, or fact instead.
- ALWAYS terminate the post on the direct capital market consequence:
  * The impact on weighted average cost of capital (WACC) or hurdle rates.
  * Equity multiple de-rating or governance discounts.
  * Sovereign yield curve steepening and duration risk.
  * ROIC cannibalization or structural margin compression.

=== 7. NUMERICAL ACCURACY & REPORTING CONVENTIONS ===
- Write large figures using professional notations: "$1.15M" or "1.15 million", NEVER "1,152.7 thousand".
- Explicitly differentiate between run-rate, quarterly print, trailing-twelve-months (TTM), and multi-year cumulative backlog. Never compare a single-year flow to a multi-year stock.
- Verify asset market caps and metric plausibility before asserting scale.
- NUMBERS HARD RULE: Use a figure ONLY if it appears verbatim in that story's SOURCE TEXT below, or in the FRESH DATA block. If a story shows "SOURCE TEXT: (unavailable)", write the post with NO specific figures at all - argue the mechanism qualitatively instead. Inventing a plausible number is the single worst thing you can do here.
  List every figure you used in the FIGURES field, with where it came from (always specify "Story [N]").
- Format numbers the English way: "." for decimals, "," for thousands. Write 2.6%, not 2,6%. Write 58,600 not 58.600.
- MISMATCHED BASES: When you put two numbers side by side, name what each one actually is: period (annual vs. cumulative vs. quarterly), unit, and scope. If the bases don't match, do not compute or name a ratio ("Nx", "up 12x") - describe the two numbers in words instead, stating each one's base.

=== FEW-SHOT EXAMPLES: BEFORE (WEAK AI DRAFT) VS. AFTER (INSTITUTIONAL REWRITE) ===

EXAMPLE 1: SINGLE TOPIC / CORPORATE GOVERNANCE & SOVEREIGN EXTRACTION
BEFORE (Weak AI Draft):
Many investors assume that record profits at state-controlled companies mean massive dividends. When Orlen reported a net profit of 15.8 billion PLN for the first half of 2026, minority shareholders expected a major payout.
The reality is different.
Instead of a dividend, the state wants Orlen to buy a 40% to 45% stake in PGZ. This transaction will cost the company between 20 and 22 billion PLN.
For minority shareholders, this is a warning. The cash they expected to receive is being redirected to buy defense assets from the government. The state gets its cash, but public investors are left holding a company that just spent over 20 billion PLN on unlisted defense assets.

AFTER (Institutional Rewrite):
The classic CEE governance discount in real time:

Orlen posts a 15.8 billion PLN net profit for H1 2026. Retail and institutional holders expect a normalized payout. Instead, the Polish state directs Orlen to absorb a 40–45% stake in unlisted state defense giant PGZ for 20 to 22 billion PLN.

The mechanics are obvious:
1. Warsaw funds defense procurement off-budget, avoiding EU deficit caps.
2. Minority shareholders absorb an illiquid, unlisted asset with no clear path to cash generation or secondary sale.

When investing in state-controlled champions, you aren't just underwriting commodity margins—you’re underwriting fiscal policy risk.

---

EXAMPLE 2: SINGLE TOPIC / BIG TECH CAPEX & STRUCTURED RISK
BEFORE (Weak AI Draft):
Everyone is watching AI chips, but the real bottleneck might be insurance plumbing. Most people think big tech firms just buy standard insurance for their new data centers.
But they can't. A single modern facility can cost up to $50B. That is way too much risk for any single insurer to hold on its balance sheet. In fact, 40% of these US centers sit in tornado zones.
Since traditional insurers are maxed out, tech giants hold the risk themselves. The fix? Investment banks are packaging this risk into new financial products for institutional investors. This is where the next big structured finance fees are.

AFTER (Institutional Rewrite):
Hyperscalers don't have a power or chip problem. They have an underwriting problem.

Next-generation AI campuses now cost up to $50 billion per facility. With ~40% of US capacity clustered in high-risk weather corridors, traditional commercial insurers cannot take the concentration risk onto their balance sheets. Syndicates are tapped out.

Big Tech cannot afford to leave $50B assets naked or burn liquidity on self-insurance without inflating their weighted average cost of capital.

The endgame isn't traditional insurance—it's securitization. Wall Street is already engineering dedicated catastrophe bonds and balance-sheet carve-outs to offload data center physical risk into private credit.

---

EXAMPLE 3: SINGLE TOPIC / FINANCIAL PRODUCT MATH & VOLATILITY DRAG
BEFORE (Weak AI Draft):
Most retail investors buy leveraged ETFs thinking they can outsmart the daily market swings. The reality is brutal. These products have a median return of minus 38%.
But here is the real kicker. Even though investors lost their shirts, the fund issuers made $506 million in management fees. The plumbing of these funds is designed to win no matter what. High turnover and daily rebalancing mean massive fee generation, even as volatility drags the actual fund value to zero.
In finance, you don't need to predict the next stock rally. Sometimes, just building the toll booth is the best trade on the street.

AFTER (Institutional Rewrite):
Single-stock leveraged and inverse ETFs are a retail meat grinder—and Wall Street’s best annuity.

The numbers are stark: the median leveraged single-stock ETF delivered a -38% return, yet fund issuers harvested $506 million in management fees over the same period.

The vehicle is engineered around compounding math traps:
1. Daily leverage resets trigger severe beta slippage in volatile sideways markets, mathematically guaranteeing capital decay over extended holding periods.
2. Portfolio rebalancing requires constant, high-frequency derivative turnover, creating structural fee drag.

Retail traders treat daily leverage as long-term directional conviction. The issuers don't take market risk—they just sit at the derivative toll booth and collect high fees on decaying equity.

---

EXAMPLE 4: MULTI-TOPIC DIGEST / FISCAL POLICY & RESTRUCTURING
BEFORE (Weak AI Draft):
Three stories caught my eye this week:
- Poland's public deficit is projected to top 7.1% of GDP next year. This is rare. Running a deficit this deep during good economic times is almost unprecedented in the EU, signaling long-term structural debt pressure rather than temporary crisis spending.
- The solidarity tax is set to rise to 5%, alongside a massive drop in the flat-tax threshold to 250,000 EUR. This will push around 43,000 entrepreneurs into higher tax brackets, which should trigger massive demand for corporate restructuring.
- KNF gave the green light for PZU to absorb Link4 by the first quarter of 2027. This consolidation could reduce price competition in the non-life insurance sector, shifting the industry's focus toward protecting underwriting margins.
These shifts point to a busy autumn for Polish corporate advisors.

AFTER (Institutional Rewrite):
Three fiscal and corporate restructuring triggers in Poland:

1. Unanchored pro-cyclical deficits: Poland's budget gap is projected to breach 7.1% of GDP in 2027. Sustaining crisis-era deficit spending during solid GDP growth locks in elevated sovereign risk premia and guarantees heavy primary bond issuance.
2. Restructuring wave from the Solidarity Tax: Raising the solidarity levy to 5% and lowering the threshold to €250,000 hits roughly 43,000 entrepreneurs. This tax friction will accelerate the migration of operating profits into Estonian CIT structures, family foundations, and holding companies.
3. Non-life underwriting consolidation: KNF’s clearance for PZU to fully integrate Link4 removes a key price-cutting competitor in motor insurance, paving the way for synchronized premium increases to defend operating margins against persistent parts inflation.

---

EXAMPLE 5: SINGLE TOPIC / OFF-BALANCE-SHEET CAPEX & DISCLOSURES
BEFORE (Weak AI Draft):
Everyone looks at Big Tech's massive cash piles and thinks they're invincible.
But there's a huge catch. Four tech giants have quietly piled up $2.4 trillion in off-balance-sheet commitments. Why? Because building AI requires insane amounts of power and physical space.
Instead of buying everything outright, they sign massive, long-term lease and energy deals. These don't show up as debt on the main balance sheet. But they're legally binding, long-term cash drains.
As a finance student, this is a great lesson. Cash-rich doesn't mean obligation-free. Always check the footnotes.

AFTER (Institutional Rewrite):
The pristine balance sheets of Big Tech are masking a structural shift from debt capital to off-balance-sheet operating leverage.

Alphabet, Amazon, Meta, and Microsoft currently hold over $2.4 trillion in total contractual commitments. Because these obligations take the form of long-term power purchase agreements (PPAs), colocation capacity contracts, and land reservations, they bypass headline balance-sheet debt metrics.

Yet economically, they carry the exact same credit risk as senior secured debt:
1. They are non-cancellable, multi-decade cash outflow mandates.
2. They subordinate common equity holders by creating a massive, senior fixed-charge burden against future operating cash flow.

When hyperscalers commit trillions off-balance-sheet to lock in physical energy and compute real estate, they are trading operational flexibility for capacity certainty. When modeling terminal tech cash flows, ignoring footnote commitments misprices the true enterprise cost of capital.

=== VERDICT: RIGOROUS INSTITUTIONAL ASSESSMENT ===
After writing each draft, judge it independently.
Filter: Does this reflect institutional-grade balance-sheet/macro reality with verifiable primary mechanics?
If a story is commodity retail news adding no analytical edge or institutional mechanism, mark SKIP with reason "commodity news, no edge".
If core data cannot be verified or requires prior desk confirmation, mark MAYBE with CHECK_FIRST.

=== OUTPUT FORMAT (exactly this, per draft, in order DRAFT 1 / DRAFT 2) ===
SHAPE: (digest | A/B/C - digest for DRAFT 1, whichever of A/B/C you picked for DRAFT 2)
BODY: (the post, starting with its own first line - do not print the hook separately)
FIGURES: (each number used -> Story [N] source text; e.g. "500 billion -> Story [2]"; or "none used")
SOURCE: (the url; if the draft covers more than one story, list them comma-separated)
WHY_THIS_ONE: (one line, for the author only)
VERDICT: (POST | MAYBE | SKIP)
WHY: (one line - the main reason for the verdict)
CHECK_FIRST: (one concrete action before publishing - which number to verify and where, what to re-read; or "-" if POST with no reservations)

--- SELECTED STORIES ---
{stories}

--- FRESH DATA (verified, safe to cite) ---
{data}

--- INSTITUTIONAL RESEARCH BENCHMARKS (match this analytical rigor and voice) ---
{style}

=== MANDATORY CONSTRAINTS (AMENDMENT) ===
HARD LENGTH LIMIT: digest post ≤130 words, single-topic post ≤120 words.
If the story has more facts than fit in the limit, drop the
lower-priority facts — do not just compress sentences into denser syntax to
fit everything in.

SENTENCE RULE: one sentence = one fact + one implication. No sentence
over ~20 words. Include at least one short sentence (under 8 words) per
post.

BANNED PHRASES (do not use in any form): "structural shift", "is
emerging as", "consequently", "far exceeding", "crowding out net
expansion", "this mechanism shows", "two numbers stand out", "the common
view is that", "primary bottleneck", "underlying economic reality".
Full growing list: style/banned_phrases.md — check it before writing.
"""


def format_data_block(data: dict) -> str:
    """Тот же текст FRESH DATA, что видела модель - verify.py сверяет по нему же
    числа, помеченные как взятые из этого блока."""
    return "\n".join(f"- {k}: {v}" for k, v in data.items()) or "(нет данных)"


def is_default_style_template(text: str) -> bool:
    """True, если в style/my_posts.md осталась шаблонная инструкция, а не реальные посты."""
    clean = text.strip()
    if not clean:
        return True
    markers = [
        "# Мои прошлые посты",
        "(пример структуры",
        "Вставь сюда 3–5",
        "Вставь сюда 3-5",
        "Poland's HICP came in at X%",
    ]
    return any(m in clean for m in markers)


def draft(selected: list[dict], data: dict, style_text: str, n: int = 2) -> str:
    blocks = []
    for i, s_ in enumerate(selected[:6], 1):
        body = (s_.get("body") or "").strip()
        src = body[:2500] if body else "(unavailable - use NO specific figures for this story)"
        blocks.append(
            f"[{i}] {s_['item'].title}\n"
            f"    source: {s_['item'].source} | {s_['item'].url}\n"
            f"    angle: {s_.get('angle', '')}\n"
            f"    non-obvious: {s_.get('why_nonobvious', '')}\n"
            f"    SOURCE TEXT: {src}")

    data_txt = format_data_block(data)
    clean_style = style_text.strip()
    if is_default_style_template(clean_style):
        style = ("(No past posts provided yet - follow the voice rules above, "
                 "erring on the side of plainer and shorter.)")
    else:
        style = clean_style

    return _call(DRAFT_PROMPT.format(n=n, stories="\n\n".join(blocks),
                                     data=data_txt, style=style),
                 temperature=0.9, max_tokens=16384)


# ------------------------------------------------------------------
#  ЭТАП 2.5: critique-pass (второй проход). Редактирует готовый пост.
# ------------------------------------------------------------------
CRITIQUE_PROMPT = """You are an editor reviewing a LinkedIn draft written by a finance student
persona — NOT a professional analyst, NOT a sell-side bank note. Your job is
to cut, simplify, and de-robotify. You do not add new facts, numbers, or
claims. You do not soften or change any existing number. Style only.

INPUT: a draft LinkedIn post (digest or single-topic), already fact-checked
upstream. Treat every number and claim in it as fixed and correct.

CHECK THE DRAFT AGAINST THIS LIST, IN ORDER:

1. Opening sentence: flag if it has 3+ participial/gerund clauses, or is
   over 20 words, or is a pointer sentence that announces what's coming
   instead of stating the point directly.
2. Bullets: flag any bullet that packs 3+ distinct concepts into one line.
   A bullet should read in under 5 seconds.
3. Closing line: flag if it restates the post's point in more abstract
   language than the rest of the post ("this shows how X", "this underscores
   the importance of Y"). The post should end on the last concrete fact or
   its direct one-line implication — not a summary.
4. Institutional throat-clearing — flag and rewrite out entirely, do not
   soften, remove:
   "is emerging as", "structural shift", "consequently", "far exceeding",
   "crowding out", "this mechanism shows", "two numbers stand out",
   "the common view is that", "primary bottleneck", "underlying economic
   reality", "as net payer nations demand", any sentence that could open a
   Swiss Re disclosure or an official communiqué.
5. Abstract nouns standing in for a concrete fact ("margin compression is
   severe" instead of the actual number or event that happened) — replace
   with the concrete version already present elsewhere in the draft.
6. Sentence rhythm: flag if every sentence is roughly the same length and
   construction. A good post has at least one short (under 8-word) sentence.
7. A rhetorical question at the end that the reader can't actually answer —
   cut it or replace with a concrete implication.

HARD LENGTH LIMITS AFTER EDITING:
- Digest post: 130 words maximum.
- Single-topic post: 120 words maximum.
If the draft is over the limit, cut lower-priority facts — do not just
compress sentences into denser syntax to hit the count.

CALIBRATION — match this ratio of concrete-to-abstract, this sentence
rhythm, this level of bluntness. These are real before/after pairs from
review of this same pipeline's output:

Example 1
Before: "The structural shift of global capital toward defensive asset
protection and sovereign balance-sheet defense is actively crowding out net
expansion capex and driving up long-end term premia."
After: "Global capital is playing defense: sovereign sellers are dumping US
paper, while corporates burn their capex just staying in place."

Example 2
Before: "Underwriting capacity limits and P&C balance-sheet concentration
risk are emerging as the primary physical bottlenecks to hyperscale AI
infrastructure expansion."
After: "The next hard ceiling on AI infrastructure isn't chip supply or
power grids — it's commercial insurance capacity."

Example 3
Before: "This massive liquidity drain from commercial bank deposits to
state debt forces banks to bid up funding costs."
After: "That retail flight from bank deposits to government debt is
forcing local banks to pay up for liquidity."

Example 4
Before: "To defend the corridor's viability, regional governments are
forced into heavy defensive maintenance capex. Kazakhstan has already
deployed $57 million for dredging at Kuryk..."
After: "Kazakhstan has already sunk $157M into dredging Kuryk and Aktau
ports. None of this is expansion — it's pure defensive capex to stop ships
from scraping bottom."

Full rewrite example (digest, for pacing/length reference):
"Three signs that global capital is shifting from growth to defense:
• Reserve dumping: Japan liquidated a record $88B in foreign assets in
August, while China cut Treasury holdings to a 25-year low. Both are
burning dollar reserves to protect domestic currencies, steepening
long-end yields.
• The maintenance trap: 75% of current US corporate tech capex goes toward
replacing depreciating hardware. Only 25% actually funds business
expansion.
• Uninsurable clusters: With AI campuses costing up to $50B, commercial
insurers are refusing single-site concentration risk. Big Tech is forced
to carry that multi-billion liability on its own books.
When sovereigns sell paper and companies spend just to tread water, real
hurdle rates rise fast."

OUTPUT:
Return ONLY the edited draft, ready to publish. No score, no explanation,
no list of what you changed — unless the calling code explicitly requests
a review instead of a rewrite (separate mode, not this one)."""


def critique_draft(text: str, shape: str = "single") -> str:
    """Второй проход (critique-pass): редактирует готовый черновик (LinkedIn post body).
    Не генерирует заново, не меняет и не выдумывает цифры, только правит стиль,
    вычищает клише и сжимает под лимиты слов (digest <= 130, single <= 120).

    При сбое сети/Gemini возвращает исходный text без падения пайплайна (CLAUDE.md правило 1).
    """
    clean_text = (text or "").strip()
    if not clean_text:
        return ""

    shape_label = "digest post" if shape == "digest" else "single-topic post"
    prompt = (
        f"{CRITIQUE_PROMPT}\n\n"
        f"DRAFT TYPE: {shape_label}\n\n"
        f"DRAFT:\n{clean_text}"
    )

    try:
        edited = _call(prompt, temperature=0.3, max_tokens=4096)
        edited = re.sub(r"^```(?:markdown|text)?\s*|\s*```$", "", edited.strip(), flags=re.MULTILINE).strip()
        if edited:
            return edited
        log.warning("critique_draft: пустой ответ модели — сохраняем исходный текст")
        return clean_text
    except Exception as exc:
        log.warning("critique_draft упал: %s — сохраняем исходный текст", exc)
        return clean_text


# ------------------------------------------------------------------
#  T5: скорер черновиков. Судит уже готовый пост, не переписывает.
#  Вызывается только из score_draft.py --deep, после локальных проверок.
# ------------------------------------------------------------------
SCORE_PROMPT = """You are reviewing one already-written LinkedIn draft before publication.
Do not rewrite it - judge only.

DRAFT:
{text}

Answer four questions, each a one-word verdict plus a one-sentence reason:
1. personal_stake: STAKE if it reads as the author's own analytical synthesis (balance-sheet conflict, mechanism, non-obvious metric), RECAP if it reads as a summary of someone else's reporting.
2. posture: EXPLAINING if it argues a mechanism with cynical institutional authority, ASKING if it subtly seeks correction, validation, or ends on an audience question.
3. voice: INSTITUTIONAL if it reads like an institutional research analyst grounded in balance-sheet realities, AMATEUR_OR_CONSULTANT if it reads like a student, amateur, or generic consultant.
4. human: HUMAN if it reads like a person wrote it, LLM_RHYTHM if it has telltale LLM patterns (parallel triads, "it's not just X, it's Y" structure, generic transitions, dramatic placeholders).

Then give exactly 2-3 concrete edits: quote the exact phrase or sentence to cut or change, and say what to do instead. No general advice.

Return JSON only:
{{"personal_stake": "STAKE|RECAP", "posture": "EXPLAINING|ASKING",
"voice": "INSTITUTIONAL|AMATEUR_OR_CONSULTANT", "human": "HUMAN|LLM_RHYTHM",
"reasons": {{"personal_stake": "...", "posture": "...", "voice": "...", "human": "..."}},
"edits": ["...", "...", "..."]}}
"""


def judge_draft(text: str) -> dict:
    raw = _call(SCORE_PROMPT.format(text=text), as_json=True, temperature=0.2, no_thinking=True)
    return _parse_json(raw)
