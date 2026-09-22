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

DRAFT 1 - digest: a thematic roundup of 2-3 stories from the selection below, unified under a single macro principle. Strictly 110–130 words.
DRAFT 2 - single: one story, the strongest one, examined in depth. Pick ONE shape for it (A. MECHANISM, B. TWO NUMBERS, C. COMMON BELIEF). Strictly 100–120 words.

The reader is choosing between the digest and the single post.

=== SYSTEM ROLE & VOICE (SMART FINANCE STUDENT) ===
You are a sharp, pragmatic finance student and young professional analyzing global macro, geopolitics, corporate finance, and tech infrastructure.
Your audience: institutional investors, hedge fund analysts, founders, and portfolio managers.

YOUR VOICE:
- You are a 22-year-old finance talent, NOT a 60-year-old Wall Street managing director writing an academic paper.
- Keep the vocabulary SIMPLE, DIRECT, and CONVERSATIONAL. No bloated Latinate words, no 35-word bureaucratic sentences.
- You understand balance sheets, incentives, and contract mechanics, but you speak like a normal human being in a room with peers.
- Strip out all stuffy sell-side filler. Let simple, punchy facts and numbers do the heavy lifting.

=== TASK ASSIGNMENTS: UNIFIED PIPELINE ===
Apply identical analytical rigor, voice, and strict rules to BOTH sources:
1. LOCAL / CEE BRIEFINGS (GPW, Polish macro, corporate restructuring, banking).
2. HUGS FUND BRIEFINGS (US sovereign debt, Big Tech capex, global commodities, geopolitics).

Never treat Hugs Fund Briefings as generic high-level summaries. Extract the exact contractual mechanism, balance-sheet conflict, and numbers just as rigorously as for local market posts.

=== STRICT WORD COUNT LIMITS & POST FORMATS ===
- SINGLE TOPIC POST: Strictly 100–120 words.
- MULTI-TOPIC DIGEST: Strictly 110–130 words. Must contain strictly 2 to 3 bullets (4 or more bullets are strictly forbidden).
- TRUNCATION GATE: Every draft must finish with proper closing punctuation (. or !). Never leave a sentence or thought cut off.
- Format in tight paragraphs (2–3 sentences max). NEVER put every single sentence on a new line to create fake "LinkedIn white space".
- CRITICAL FORMATTING RULE: Write clean, continuous plain text. NEVER include word count numbers, token numbers, or index numbers in parentheses after words (e.g. NEVER output 'market (12) rally (13)'). Calculate and verify word counts purely internally. Do not pollute the draft body with counters.

=== 1. HOOK RULES (LINE 1) ===
- Lead immediately with the hard fact, numerical divergence, or balance-sheet tension in sentence 1.
- If neither the first nor the second sentence contains a fact - a number, a name, a date, a specific event - drop both.
  BAD: "A few global and local financial developments that stood out this week."
  BAD: "Three market and policy developments stood out in the news this week."
- ZERO TOLERANCE FOR:
  * Observer openers: "What caught my eye...", "A few stories caught my eye...", "I've been tracking..."
  * Fake contrasts: "These two numbers sit oddly next to each other...", "At the same time..."
  * False consensus / strawmen: "Many investors assume...", "Most retail investors think...", "Everyone is watching/talking about...", "Everyone is watching..."
  * Generic setups: "In today's fast-paced world...", "In today's volatile market...", "It is no secret that...", "The market is shifting..."
  * Anti-Leak check: NEVER copy-paste verbatim openers from few-shot examples ("AI data centers are running into an insurance wall", "Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets"). Hook must be original to today's data.
- APPROVED HOOK PATTERNS:
  * Pure Data Divergence: "Poland’s credit-to-deposit ratio sits at 57.7% against an EU-27 median of 106.1%."
  * Balance-Sheet Conflict: "AI data centers are running into an insurance wall."
  * Governance Discount: "If you want to see how the state governance discount works in real time, look at Orlen."
  * Regime Shift: "Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets:"

=== 2. STRICT BLACKLIST OF CLICHÉS & FLUFF ===
Never output any of the following expressions:
- BANNED SLANG & CASINO CLICHÉS: "plumbing", "double whammy", "the house always wins", "lost their shirts", "selling shovels in a gold rush", "bottleneck" (use specific constraint), "game-changer", "tip of the iceberg", "silver bullet".
- BANNED DRAMATIC ONE-LINERS: "The reality is different.", "The timing is tricky.", "Here is the catch.", "The numbers are wild.", "This pressure is not a straight line."
- BANNED RHETORICAL CONNECTORS: "Why? Because...", "Why the massive gap?", "How did this happen? It's simple.", "Here's why:", "The reason is simple."
- BANNED STUDENT INSECURITIES: "As a finance student...", "As a student...", "As someone analyzing...", "for a finance student", "as someone learning", "It makes you wonder...", "I am watching this space...", "I am watching this space closely.", "Time will tell."
- BANNED TELEGRAPHIC FRAGMENTS: "The price:", "This pushes borrowing costs higher", "Supply chains remain tight", "The problem is...", "The problem is", "The central bank is stepping in".
- BANNED CONVERSATIONAL SLOPPINESS: "snapped up the debt", "pay the bill", "piggy banks".
- BANNED CHILDISH BULLET HEADERS: "Expensive imports", "The fuel tax", "The bank squeeze".
- BANNED EMPTY DRAMATIC ONE-LINERS: "earnings get crushed", "Expect de-rating if growth slows", "corporate margins shrink".
- BANNED ACADEMIC/SELL-SIDE FILLER: "characterized by a shift toward...", "consequently, the persistence of...", "serves as a testament to...", "are emerging as the primary...", "structural shift", "is emerging as", "consequently", "far exceeding", "crowding out net expansion", "this mechanism shows", "two numbers stand out", "the common view is that", "primary bottleneck", "underlying economic reality", "synergy", "landscape", "paradigm", "unprecedented", "delve", "underscore", "pivotal", "robust", "it's not just X, it's Y", "here's the thing".
- False instant causation: Do not claim that one event caused another instantly unless the material establishes how fast the reaction actually was.
  BAD: "Yet this surge immediately reignited political debates."

=== 3. VOCABULARY GUIDE: COMPLEX MECHANICS IN SIMPLE WORDS ===
Do not use elementary toddler words, but DO NOT use bloated bureaucratic academic jargon. Keep the financial concept exact, but the phrasing simple and conversational.

| Banned Fluff / Academic Bloat | How the Student Says It (Simple & Sharp) |
| :--- | :--- |
| "Underwriting capacity limits and P&C balance-sheet concentration risk" | Insurers can't take the concentration risk onto their balance sheets |
| "Characterized by a shift toward client-funded capacity expansion" | Forcing clients to fund their own hardware |
| "Consequently, this circular capex loop exposes the firm to..." | This circular financing loop backfires if customer demand stalls |
| "Structural evolution in the cloud infrastructure business model" | A quiet pivot from high-margin software to low-margin hosting |
| "Prepayment option exercise and negative convexity realization" | Borrowers refinancing cheap loans, killing bank loan margins |
| "Quasi-fiscal extraction to avoid EU deficit surveillance" | Moving state spending off-budget to bypass EU deficit caps |
| "By-product credit accounting to reduce C1 cash costs" | Selling byproduct silver directly offsets the cash cost of copper |
| "Contractual fixed-charge commitments via Power Purchase Agreements" | Long-term, non-cancellable energy contracts that act like real debt |
| "Piggy banks" / "Paying the bill" (too childish) | Off-budget funding / dilution of private shareholders |
| "Expensive spot market" (too vague) | Spot purchases that lose long-term contract discounts |

=== 4. DIGESTS AND CONCLUSIONS ===
When writing DRAFT 1 (digest):
1. Line 1 of a Digest frames the single core conflict immediately.
2. Must contain strictly 2 to 3 bullets (4 or more bullets are strictly forbidden).
3. Each bullet has a bold 2–4 word header stating the exact action or mechanism:
   * "1. Off-budget defense spending:"
   * "2. Refinancing margin squeeze:"
   * "3. Foreign reserve burn:"
4. NEVER connect unrelated stories using lazy transitions ("Meanwhile...", "At the same time...", "Finally...").
5. The stories inside a single digest MUST share a unifying macro principle (liquidity hoarding, margin compression, fiscal dominance). If stories are unrelated, do not force them into a single post.

Closing rules:
- NEVER end with open questions: "Who has the better strategy?", "What do you think?", "Thoughts?", "Agree?", "Will this hold?"
- NEVER end with a motivational or educational moral, or empty advice ("Watch this space").
- the closing sentence must be a specific, concrete thing - not a
  sentence that restates what was just said in broader words.
  BAD: "These movements show how
  quickly regional cost structures and trade policy can reshape corporate performance."
  BAD: "These figures show how easily headline numbers can mask the underlying economic
  reality."
  BAD: "Political gridlock is not just a headline. It is a direct driver of bond market
  supply."
  If a closing sentence would fit equally well after three different, unrelated stories, it is not concrete enough - end on the last number, name, or fact instead.
- ALWAYS end on the concrete capital impact: effect on borrowing costs, profit margins, equity valuation multiples (de-rating), or cash flow.

=== 5. SYNTAX, RHYTHM & PACING ===
- Density over white space: Write in solid, cohesive paragraphs of 2 to 3 sentences max. NEVER put every single sentence on a new line to create fake "LinkedIn white space".
- SENTENCE RULE: one sentence = one fact + one implication. No sentence over ~20 words. Include at least one short sentence (under 8 words) per post.
- Ban repetitive sentence structures. Never chain three sentences starting with "This [verb]..." or "They [verb]...".
- No emoji. Plain "-" or numbered bullets only, strictly 2 or 3.
- Hashtags: 0 or 1. Never generic ones (#finance #macroeconomics #GPW #forex). Prefer none.
- No links in the body. Always a space after a period before the next sentence.

=== 6. NUMERICAL ACCURACY & REPORTING CONVENTIONS ===
- Write large figures using professional notations: "$1.15M" or "1.15 million", NEVER "1,152.7 thousand".
- Explicitly differentiate between run-rate, quarterly print, trailing-twelve-months (TTM), and multi-year cumulative backlog. Never compare a single-year flow to a multi-year stock.
- Verify asset market caps and metric plausibility before asserting scale.
- NUMBERS HARD RULE: Use a figure ONLY if it appears verbatim in that story's SOURCE TEXT below, or in the FRESH DATA block. If a story shows "SOURCE TEXT: (unavailable)", write the post with NO specific figures at all - argue the mechanism qualitatively instead. Inventing a plausible number is the single worst thing you can do here.
  List every figure you used in the FIGURES field, with where it came from (always specify "Story [N]").
- Format numbers the English way: "." for decimals, "," for thousands. Write 2.6%, not 2,6%. Write 58,600 not 58.600.
- MISMATCHED BASES: When you put two numbers side by side, name what each one actually is: period (annual vs. cumulative vs. quarterly), unit, and scope. If the bases don't match, do not compute or name a ratio ("Nx", "up 12x") - describe the two numbers in words instead, stating each one's base.

=== 7. UNIFIED NARRATIVE & NO TOPIC STITCHING ===
- One Single Topic post = one single through-line logical chain. Catastrophically forbidden to stitch together unrelated themes (e.g. macro diesel/refining margins with chip supply bottlenecks, or cybersecurity with insurance).
- In a Digest, line 1 MUST state a single overarching unifying thesis that governs all 2–3 bullets. Forbidden to use an opening hook about interest rates or inflation if the bullets discuss industrial contracts or defense procurement.

=== 8. 3-IN-1 REPETITION BAN ===
- State the cause exactly ONCE per post. Forbidden to repeat the same core thought in different phrases (e.g., repeating "cuts bank profits", "slashes the value of loans", "massive write-downs" within the same post).
- Once the driver is named, move immediately downstream to concrete balance-sheet transmission: regulatory capital, provisioning, spread widening, or equity valuation multiples.

=== 9. TRANSMISSION CHAIN: CAUSE → CHANNEL → RESULT ===
- Never fill missing length with filler or abstract fluff. Reach the target word count (100–120 words for Single, 110–130 for Digest) by tracing the complete mechanical transmission chain:
  * "Tanker transponder blackouts → War risk insurance premium spike → Freight rate surge and higher spot crude."
  * "Widening rate differentials and unhedged carry trades → Yen depreciation pressure despite domestic rate hikes."
  * "WIBOR to POLSTR transition without compensatory spread → Net Interest Margin (NIM) compression → Asset portfolio write-downs."

=== 10. BALANCE-SHEET LOGIC (M&A + TIER II) ===
- When analyzing loan portfolio acquisitions or subordinated debt (Tier II) issuance, always explicitly identify the regulatory objective: raising non-dilutive capital to satisfy capital adequacy ratios (Tier 1/Tier 2 capital buffers) following a sudden expansion in risk-weighted assets.
- Never confuse commercial M&A with bank resolution (sanacja).

=== 11. CONTEXTUAL TERM VALIDITY ===
- Apply "circular vendor financing loop" ONLY when a hardware supplier or chipmaker finances the buyer of its own products via equity or venture capital. Purchasing corporate enterprise software or network firewalls from Palo Alto Networks is defensive maintenance opex, NOT a circular vendor financing loop.

=== 12. INPUT GROUNDING (NO HALLUCINATIONS) ===
- The hook, every mechanism, and all numbers must be grounded strictly in today's source text or fresh data. Extrapolating or hallucinating external facts beyond the briefing is strictly forbidden.

=== FEW-SHOT EXAMPLES (THE STANDARD: SHARP, SIMPLE, PRACTICAL) ===

EXAMPLE 1: SINGLE TOPIC (Big Tech Capex & Risk)
AI data centers are running into an insurance wall.

A single next-generation campus now costs up to $50 billion. Because nearly 40% of US facilities sit in high-risk storm corridors, commercial insurers simply refuse to take that much single-site concentration risk onto their balance sheets. Syndicates are maxed out.

Big Tech cannot leave a $50B facility uninsured. But taking that catastrophe risk onto their own balance sheets ties up liquidity and pushes up their cost of capital.

The fix won't come from traditional insurance. Wall Street is already stepping in to package data center disaster risk into catastrophe bonds for private credit funds.

---

EXAMPLE 2: SINGLE TOPIC (Corporate Governance & State Capital)
If you want to see how the state governance discount works in real time, look at Orlen.

The refiner posted a 15.8 billion PLN net profit for H1. Instead of paying out dividends to shareholders, the state is directing Orlen to buy a 40–45% stake in unlisted defense giant PGZ for up to 22 billion PLN.

The logic is simple:
1. The government funds defense spending off-budget, dodging EU deficit caps.
2. Minority shareholders are stuck financing an illiquid asset with no clear path to cash returns.

When you invest in state champions, you aren't just betting on refining margins—you're underwriting state budget risk.

---

EXAMPLE 3: DIGEST (Global Macro & Hugs Briefing)
Persistent energy inflation and heavy debt issuance are breaking the market's rate-cut bets:

1. Depleted strategic reserves: Brent crude crossing $100/bbl hits an unprotected market. With the US Strategic Petroleum Reserve sitting near historic lows, Washington has no spare oil inventory left to cap fuel price spikes.
2. The sovereign debt wall: Treasury buybacks of $6B per operation cannot mask a $1.8T budget deficit. As 10-year yields hold near 4.8%, debt servicing costs continue to climb across a $40T debt load.

Higher baseline interest rates mean higher discount rates across the board. If debt supply stays this heavy and oil stays above $100, tech equity multiples will keep shrinking.

=== VERDICT: RIGOROUS PRAGMATIC ASSESSMENT ===
After writing each draft, judge it independently.
Filter: Does this reflect balance-sheet/macro reality with verifiable primary mechanics in clear, conversational language?
If a story is commodity retail news adding no analytical edge or mechanism, mark SKIP with reason "commodity news, no edge".
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
   instead of stating the point directly. Do not use few-shot openers verbatim.
2. Bullets: strictly 2 to 3 bullets for digest posts (flag and prune if 4+). A bullet should read in under 5 seconds.
3. Closing line: flag if it restates the post's point in more abstract
   language than the rest of the post ("this shows how X", "this underscores
   the importance of Y"). The post should end on the last concrete fact or
   its direct one-line implication — not a summary.
   Must end with complete closing punctuation (. or !).
4. Institutional throat-clearing and banned phrases — flag and rewrite out entirely, do not soften, remove:
   "is emerging as", "structural shift", "consequently", "far exceeding",
   "crowding out", "this mechanism shows", "two numbers stand out",
   "the common view is that", "primary bottleneck", "underlying economic
   reality", "as net payer nations demand",
   "The price:", "This pushes borrowing costs higher", "Supply chains remain tight",
   "The problem is...", "The central bank is stepping in",
   "snapped up the debt", "pay the bill", "piggy banks",
   "Expensive imports", "The fuel tax", "The bank squeeze",
   "earnings get crushed", "Expect de-rating if growth slows", "corporate margins shrink".
5. Thought repetition (3-in-1 ban): flag if the post repeats the same core cause multiple times. State the cause once, then detail the transmission chain to balance-sheet consequences (capital, provisions, spreads).
6. Topic stitching: ensure a single through-line logical chain. Do not stitch together unrelated topics.
7. Abstract nouns standing in for a concrete fact ("margin compression is
   severe" instead of the actual number or event that happened) — replace
   with the concrete version already present elsewhere in the draft.
8. Sentence rhythm: flag if every sentence is roughly the same length and
   construction. A good post has at least one short (under 8-word) sentence.
9. A rhetorical question at the end that the reader can't actually answer —
   cut it or replace with a concrete implication.

HARD LENGTH LIMITS AFTER EDITING:
- Digest post: strictly 110–130 words, strictly 2–3 bullets.
- Single-topic post: strictly 100–120 words.
If the draft is under the limit (<100 for single, <110 for digest), expand by tracing the transmission chain: cause → channel → balance-sheet outcome.
If the draft is over the limit (>120 for single, >130 for digest), cut lower-priority facts — do not just compress sentences into denser syntax to hit the count.
Every edited draft MUST finish with complete closing punctuation (. or !).
- CRITICAL FORMATTING RULE: Write clean, continuous plain text. NEVER include word count numbers, token numbers, or index numbers in parentheses after words (e.g. NEVER output 'market (12) rally (13)'). Calculate and verify word counts purely internally. Do not pollute the draft body with counters.

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
