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

Return JSON only: an array of objects with keys id, score, angle, why_nonobvious.
Include ONLY items scoring 6 or above. Returning 4 excellent items beats 20 mediocre ones.

ITEMS:
{items}
"""


def rank(items, top_n: int = 12) -> list[dict]:
    """Один вызов на весь список. Дефицит free tier - число запросов в день (20/модель),
    не токены, а вход в 1M токенов легко тянет сотню заголовков разом."""
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
        log.warning("отбор упал: %s", exc)

    for s in scored:
        s["final"] = float(s.get("score", 0)) * s["item"].weight
    scored.sort(key=lambda x: x["final"], reverse=True)
    log.info("прошли отбор: %d, берём %d", len(scored), min(top_n, len(scored)))
    return scored[:top_n]


# ------------------------------------------------------------------
#  ЭТАП 2: черновики. Антишлак-правила прописаны явно.
# ------------------------------------------------------------------
DRAFT_PROMPT = """Write exactly 3 LinkedIn post drafts in ENGLISH, in this fixed order:

DRAFT 1 - digest: a roundup of the 2-3 best stories from the selection below. Frame it as
something like "Three things I read this week that stuck with me" (vary the wording, do not
copy that line). For each story: one line on what happened, then one or two lines on why it
is interesting. Keep it light - no deep analysis. 120-170 words.

DRAFT 2 - digest, short: the same roundup, the same stories, but 80-110 words. Drier, no
intro line - go straight into the first item.

DRAFT 3 - single: one story, the strongest one, examined a bit deeper - a normal analytical
post. Pick ONE shape for it:

  A. MECHANISM: name the mechanism, explain how it works, then show where it just showed up.
  B. TWO NUMBERS: put two figures side by side, then explain what the pairing reveals.
  C. COMMON BELIEF: state the widely held view plainly, then the fact that complicates it.

The reader is choosing between the two digest lengths and the single post - that is the
point of writing three, not three unrelated posts on different topics.

WHO IS WRITING: a 2nd-year Finance & Accounting student at Kozminski University in Warsaw,
aiming for investment banking and asset management. He reads primary sources and runs his own
small analyses. He is not an expert and does not pretend to be one. The digest itself is a
deliberately modest frame - "an active student who reads a lot", not "an analyst" - it is
honest about what these posts are and it lowers reputational risk.

=== TONE CEILING ===
The author is a 2nd-year student, not a professor. If a sentence sounds like a bank research
note, simplify it. Prefer "X grew faster than Y" over "the differential in growth
trajectories". No jargon where a plain word exists. It is fine for the post to be simple; it
is not fine for it to be pretentious. A fancy word the author would not actually say out loud
in conversation is a mistake.

=== POSTURE: EXPLAINING, NOT ASKING ===
He explains a mechanism and uses his own work as illustration. He does NOT ask to be corrected.

  WRONG: "My DDM says 111 dollars, the market says 319. What am I missing?"
  RIGHT: "DDM structurally understates banks with low payout ratios. On JPM the gap is 3x."

Same material, opposite standing. The first asks for help; the second teaches something.
BANNED, do not write these or anything close: "What am I missing", "I might be wrong",
"I might be reading this wrong", "Correct me if", "Am I off base". Confidence about the
mechanism, honesty about limits of the data - those are different things.

=== FIRST PERSON: ONLY FOR WORK ACTUALLY DONE ===
A digest item or the single post can be written in first person, but ONLY for something he
verifiably did with the material in front of him: read a primary source, pulled a specific
figure from it. NEVER claim analytical work he did not do - comparing figures, running the
numbers, computing a ratio, reading a report cover to cover, attending an event. The
comparison or mechanism can still be the point of the post; it just cannot be framed as
something he personally performed - state it about the numbers themselves, not about his own
activity. NEVER claim observation, monitoring, research, or access he did not have - no "I
tracked this play out", "I've been following this", "I noticed this developing". A false
claim about his own work is worse than a fabricated number: a number can be checked against
the source, a claim about what he personally did cannot - and if it's ever caught out, it
costs more than the post.

  GOOD: "I pulled these figures from Statistics Poland."
  GOOD: "These two numbers sit oddly next to each other."
  BAD:  "I compared these figures from the RynekPierwotny report, and the difference is
        striking."
  BAD:  "I ran the numbers and the gap is striking."
  BAD:  "I read the report and here's what stood out."
  BAD:  "I tracked this mechanism play out at Situational Awareness."
  BAD:  "I have been following this story for weeks."

If a story gives him nothing of his own to do - he only read it, nothing to pull - write
about it impersonally. Do not force a first-person claim where there is nothing real to
claim.

=== NUMBERS: HARD RULE ===
Use a figure ONLY if it appears verbatim in that story's SOURCE TEXT below, or in the
FRESH DATA block. If a story shows "SOURCE TEXT: (unavailable)", write the post with NO
specific figures at all - argue the mechanism qualitatively instead. Inventing a plausible
number is the single worst thing you can do here.
List every figure you used in the FIGURES field, with where it came from.

Format numbers the English way: "." for decimals, "," for thousands. Write 2.6%, not 2,6%.
Write 58,600 not 58.600.

Before finalizing, check every number claim against itself. If a figure moves from 4.7 to
58.6, that is roughly a 12.5x change - call it that, not "tripling" or "doubling". If two
sentences in the same draft imply different magnitudes for the same move, the draft is
broken: fix the math or drop the comparison.

MISMATCHED BASES. When you put two numbers side by side, name what each one actually is:
period (annual vs. cumulative vs. quarterly), unit, and scope. A ratio between numbers with
different bases is not a fact even if the arithmetic is correct - an annual contract figure
compared to a multi-year total, a quarter compared to a year, a flow compared to a stock, or
a nominal figure compared to a real one. If the bases don't match, do not compute or name a
ratio ("Nx", "up 12x") - describe the two numbers in words instead, stating each one's base.
A correctly-computed ratio between mismatched bases is exactly as bad as a made-up figure.

=== VOICE ===
- Plain words. Banned: leverage, synergy, landscape, paradigm, unprecedented, game-changer,
  delve, underscore, pivotal, robust, "it's not just X, it's Y", "here's the thing".
- Short sentences. Mix in some very short ones.
- Never open with "I'm excited to share", "Let that sink in", or a rhetorical question.
- No emoji. Plain "-" bullets only, max 3.
- Hashtags: 0 or 1. Never generic ones (#finance #macroeconomics #GPW #forex) - they hurt
  classification. Prefer none.
- Word count is a hard limit per draft: DRAFT 1 (digest) 120-170 words, DRAFT 2
  (digest, short) 80-110 words, DRAFT 3 (single) 110-170 words.
- At most ONE of the 3 drafts may end with a question - not zero forced, not a rule to use
  every time. That one question must invite someone else's expertise, not ask for
  validation: a specific industry professional would answer it better than the author, and
  it must grow out of the post's own content and name something concrete (an import
  structure, a fee mechanic, a specific segment) - never a generic closer like "What do you
  think?", "Thoughts?", "Agree?", or "let me know what you think". The digest drafts (1 and
  2) are allowed to simply end on their last item; ending every post with a question is
  itself a tell, and is exactly the pattern to avoid. The others end on a statement.
- No links in the body. LinkedIn suppresses reach on posts with external links.
- Sound like a curious student who read the source, not a consultant summarising it.
- Never name his own role or status in the text itself: no "for a finance student", "as a
  student", "as someone learning IB". The analysis carries the weight, not the bio.
- Always a space after a period before the next sentence. "spikes.Quarterly" is a proofing
  failure, not a style choice - check for it.

=== VERDICT: CAN HE DEFEND THIS? ===
After writing each draft, judge it independently - a digest draft is judged as a whole, not
item by item. The key filter: could the author answer the first follow-up question in the
comments himself? He is comfortable with: financial accounting, DDM/CAPM valuation, bank
financials, Polish macro, ETFs, crypto basics, brokerage business models. If the post's core
mechanism sits outside this list, the verdict cannot be POST - at most MAYBE with CHECK_FIRST
naming what he must actually understand before publishing.

Second filter: if a story will obviously be in every feed within a day or two and the draft
adds nothing of his own - no calculation, no Poland angle, no comparison he made - the verdict
is SKIP, reason "commodity news, no edge".

All three drafts getting SKIP is a valid, honest outcome for a day with no real material - say
so plainly, do not stretch a verdict to POST to avoid an empty-handed day.

=== OUTPUT FORMAT (exactly this, per draft, in order DRAFT 1 / DRAFT 2 / DRAFT 3) ===
SHAPE: (digest | digest-short | A/B/C - digest for DRAFT 1, digest-short for DRAFT 2,
  whichever of A/B/C you picked for DRAFT 3)
BODY: (the post, starting with its own first line - do not print the hook separately)
FIGURES: (each number used -> where it came from; or "none used")
SOURCE: (the url; if the draft covers more than one story, list them comma-separated)
WHY_THIS_ONE: (one line, for the author only)
VERDICT: (POST | MAYBE | SKIP)
WHY: (one line - the main reason for the verdict)
CHECK_FIRST: (one concrete action before publishing - which number to verify and where, what
  to re-read; or "-" if POST with no reservations)

--- SELECTED STORIES ---
{stories}

--- FRESH DATA (verified, safe to cite) ---
{data}

--- HIS OWN PAST POSTS (match this voice, do not copy content) ---
{style}
"""


def format_data_block(data: dict) -> str:
    """Тот же текст FRESH DATA, что видела модель - verify.py сверяет по нему же
    числа, помеченные как взятые из этого блока."""
    return "\n".join(f"- {k}: {v}" for k, v in data.items()) or "(нет данных)"


def draft(selected: list[dict], data: dict, style_text: str, n: int = 3) -> str:
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
    style = style_text.strip() or (
        "(No past posts provided yet - follow the voice rules above, "
        "erring on the side of plainer and shorter.)")

    return _call(DRAFT_PROMPT.format(n=n, stories="\n\n".join(blocks),
                                     data=data_txt, style=style),
                 temperature=0.9, max_tokens=16384)


# ------------------------------------------------------------------
#  T5: скорер черновиков. Судит уже готовый пост, не переписывает.
#  Вызывается только из score_draft.py --deep, после локальных проверок.
# ------------------------------------------------------------------
SCORE_PROMPT = """You are reviewing one already-written LinkedIn draft before publication.
Do not rewrite it - judge only.

DRAFT:
{text}

Answer four questions, each a one-word verdict plus a one-sentence reason:
1. personal_stake: STAKE if it reads as the author's own work (something he did, computed,
   or noticed), RECAP if it reads as a summary of someone else's reporting.
2. posture: EXPLAINING if it argues a mechanism with confidence, ASKING if it subtly seeks
   correction or validation - even without the literal banned phrases.
3. voice: STUDENT if it reads like a curious student who did the work, CONSULTANT if it
   reads like someone summarizing a report for a client.
4. human: HUMAN if it reads like a person wrote it, LLM_RHYTHM if it has telltale LLM
   patterns (parallel triads, "it's not just X, it's Y" structure, generic transitions).

Then give exactly 2-3 concrete edits: quote the exact phrase or sentence to cut or change,
and say what to do instead. No general advice like "make it more personal".

Return JSON only:
{{"personal_stake": "STAKE|RECAP", "posture": "EXPLAINING|ASKING",
"voice": "STUDENT|CONSULTANT", "human": "HUMAN|LLM_RHYTHM",
"reasons": {{"personal_stake": "...", "posture": "...", "voice": "...", "human": "..."}},
"edits": ["...", "...", "..."]}}
"""


def judge_draft(text: str) -> dict:
    raw = _call(SCORE_PROMPT.format(text=text), as_json=True, temperature=0.2, no_thinking=True)
    return _parse_json(raw)
