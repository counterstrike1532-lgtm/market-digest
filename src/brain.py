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


def _call(prompt: str, as_json: bool = False, temperature: float = 0.7,
          max_tokens: int = 32768, retries: int = 3, no_thinking: bool = False) -> str:
    """Перебирает модели из MODELS. На 400 не долбит одним и тем же телом, а упрощает запрос.

    no_thinking: у thinking-моделей токены размышлений тратятся из maxOutputTokens,
    из-за чего JSON обрывается. Но Gemini 3 может запрещать полное отключение —
    тогда параметр снимается автоматически.
    """
    key = os.environ["GEMINI_API_KEY"]
    last = "неизвестно"

    for model in MODELS:
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

                if r.status_code in (429, 500, 503):
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
                    return text
                last = f"{model}: пустой ответ ({cand.get('finishReason')})"
                log.warning(last)

            except Exception as exc:
                last = f"{model}: {exc}"
                log.warning("%s попытка %d: %s", model, attempt, str(exc)[:160])
                time.sleep(5 * attempt)

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

3. BE TERSE. "angle" max 20 words. "why_nonobvious" max 15 words. Long fields get truncated
   and the whole batch is lost.

For each item, "angle" is what the post would ARGUE - a claim, not a topic. If you cannot
write a real claim, the score is below 7.

Return JSON only: an array of objects with keys id, score, angle, why_nonobvious.
Include ONLY items scoring 6 or above. Returning 4 excellent items beats 20 mediocre ones.

ITEMS:
{items}
"""


def rank(items, top_n: int = 12, batch: int = 25) -> list[dict]:
    """Батчим небольшими порциями: короткий ответ реже обрывается по лимиту токенов."""
    scored: list[dict] = []
    by_id = {}
    for i, it in enumerate(items):
        by_id[i] = it

    for start in range(0, len(items), batch):
        chunk = list(range(start, min(start + batch, len(items))))
        payload = "\n".join(
            f"id={i} | {by_id[i].source} | {by_id[i].tag} | social={by_id[i].social}\n"
            f"  {by_id[i].title}\n  {by_id[i].summary[:280]}"
            for i in chunk)
        try:
            res = _parse_json(_call(RANK_PROMPT.format(items=payload), as_json=True,
                                    temperature=0.2, no_thinking=True))
            for row in res:
                idx = int(row["id"])
                if idx in by_id:
                    row["item"] = by_id[idx]
                    scored.append(row)
        except Exception as exc:
            log.warning("батч %d-%d упал: %s", chunk[0], chunk[-1], exc)
        time.sleep(7)   # free tier: держимся ниже лимита запросов в минуту

    for s in scored:
        s["final"] = float(s.get("score", 0)) * s["item"].weight
    scored.sort(key=lambda x: x["final"], reverse=True)
    log.info("прошли отбор: %d, берём %d", len(scored), min(top_n, len(scored)))
    return scored[:top_n]


# ------------------------------------------------------------------
#  ЭТАП 2: черновики. Антишлак-правила прописаны явно.
# ------------------------------------------------------------------
DRAFT_PROMPT = """Write {n} LinkedIn post drafts in ENGLISH.

WHO IS WRITING: a 2nd-year Finance & Accounting student at Kozminski University in Warsaw,
aiming for investment banking and asset management. He reads primary sources and runs his own
small analyses. He is not an expert and does not pretend to be one.

=== POSTURE: EXPLAINING, NOT ASKING ===
He explains a mechanism and uses his own work as illustration. He does NOT ask to be corrected.

  WRONG: "My DDM says 111 dollars, the market says 319. What am I missing?"
  RIGHT: "DDM structurally understates banks with low payout ratios. On JPM the gap is 3x."

Same material, opposite standing. The first asks for help; the second teaches something.
BANNED, do not write these or anything close: "What am I missing", "I might be wrong",
"I might be reading this wrong", "Correct me if", "Am I off base". Confidence about the
mechanism, honesty about limits of the data - those are different things.

=== EACH DRAFT MUST HAVE A DIFFERENT SHAPE ===
Do not reuse one skeleton. Assign a different structure to each draft:

  A. MECHANISM: name the mechanism, explain how it works, then show where it just showed up.
  B. TWO NUMBERS: put two figures side by side, then explain what the pairing reveals.
  C. COMMON BELIEF: state the widely held view plainly, then the fact that complicates it.

No two drafts may open with the same move or close with the same move. If two drafts start
with "I looked at the data" or both end with a question, you have failed this instruction.

=== NUMBERS: HARD RULE ===
Use a figure ONLY if it appears verbatim in that story's SOURCE TEXT below, or in the
FRESH DATA block. If a story shows "SOURCE TEXT: (unavailable)", write the post with NO
specific figures at all - argue the mechanism qualitatively instead. Inventing a plausible
number is the single worst thing you can do here.
List every figure you used in the FIGURES field, with where it came from.

=== VOICE ===
- Plain words. Banned: leverage, synergy, landscape, paradigm, unprecedented, game-changer,
  delve, underscore, pivotal, robust, "it's not just X, it's Y", "here's the thing".
- Short sentences. Mix in some very short ones.
- Never open with "I'm excited to share", "Let that sink in", or a rhetorical question.
- No emoji. Plain "-" bullets only, max 3.
- Hashtags: 0 or 1. Never generic ones (#finance #macroeconomics #GPW #forex) - they hurt
  classification. Prefer none.
- 110-170 words. Hard limit.
- At most ONE of the {n} drafts may end with a question, and it must be a specific technical
  question a professional would actually answer. The others end on a statement.
- No links in the body. LinkedIn suppresses reach on posts with external links.
- Sound like a curious student who read the source, not a consultant summarising it.

=== OUTPUT FORMAT (exactly this, per draft) ===
SHAPE: (A, B or C)
BODY: (the post, starting with its own first line - do not print the hook separately)
FIGURES: (each number used -> where it came from; or "none used")
SOURCE: (the url)
WHY_THIS_ONE: (one line, for the author only)

--- SELECTED STORIES ---
{stories}

--- FRESH DATA (verified, safe to cite) ---
{data}

--- HIS OWN PAST POSTS (match this voice, do not copy content) ---
{style}
"""


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

    data_txt = "\n".join(f"- {k}: {v}" for k, v in data.items()) or "(нет данных)"
    style = style_text.strip() or (
        "(No past posts provided yet - follow the voice rules above, "
        "erring on the side of plainer and shorter.)")

    return _call(DRAFT_PROMPT.format(n=n, stories="\n\n".join(blocks),
                                     data=data_txt, style=style),
                 temperature=0.9, max_tokens=16384)
