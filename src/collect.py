"""Сбор сырых материалов: RSS + Hacker News + Reddit.
Каждый источник обёрнут в try/except — один упавший фид не ломает прогон.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

import feedparser
import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


@dataclass
class Item:
    title: str
    url: str
    source: str
    tag: str
    published: str          # ISO8601
    summary: str = ""
    weight: float = 1.0
    social: int = 0         # очки HN/Reddit, если есть
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Стабильный id для дедупа между прогонами."""
        base = self.url.split("?")[0].rstrip("/").lower()
        return hashlib.sha1(base.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(struct) -> str:
    if not struct:
        return datetime.now(timezone.utc).isoformat()
    try:
        return datetime(*struct[:6], tzinfo=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def fetch_rss(feeds: list[dict], hours: int) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[Item] = []
    for f in feeds:
        url = f["url"]
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                log.warning("пусто: %s", url)
                continue
            host = url.split("/")[2]
            for e in parsed.entries[:40]:
                pub = _iso(e.get("published_parsed") or e.get("updated_parsed"))
                if datetime.fromisoformat(pub) < cutoff:
                    continue
                out.append(Item(
                    title=(e.get("title") or "").strip(),
                    url=e.get("link") or "",
                    source=host,
                    tag=f.get("tag", "misc"),
                    published=pub,
                    summary=(e.get("summary") or "")[:800],
                    weight=float(f.get("weight", 1.0)),
                ))
            log.info("ok %-45s %3d", host, len(parsed.entries))
        except Exception as exc:
            log.warning("фид упал %s: %s", url, exc)
        time.sleep(0.3)
    return out


def fetch_hackernews(cfg: dict, hours: int) -> list[Item]:
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    seen, out = set(), []
    for q in cfg.get("queries", []):
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": q, "tags": "story",
                        "numericFilters": f"created_at_i>{since},points>{cfg['min_points']}",
                        "hitsPerPage": 15},
                headers=HEADERS, timeout=20)
            r.raise_for_status()
            for h in r.json().get("hits", []):
                link = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
                if link in seen:
                    continue
                seen.add(link)
                out.append(Item(
                    title=h.get("title", ""), url=link, source="news.ycombinator.com",
                    tag="hn", published=h.get("created_at", ""),
                    weight=1.1, social=int(h.get("points") or 0),
                    extra={"comments": h.get("num_comments"), "hn_query": q},
                ))
        except Exception as exc:
            log.warning("HN '%s' упал: %s", q, exc)
        time.sleep(0.3)
    log.info("ok hackernews %d", len(out))
    return out


def fetch_reddit(cfg: dict) -> list[Item]:
    """Reddit иногда режет IP дата-центров (в т.ч. GitHub). Падение здесь не критично."""
    out = []
    for sub in cfg.get("subs", []):
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json",
                             params={"t": "day", "limit": 25},
                             headers=HEADERS, timeout=20)
            if r.status_code != 200:
                log.warning("reddit r/%s -> HTTP %s (норм, пропускаем)", sub, r.status_code)
                continue
            for c in r.json()["data"]["children"]:
                d = c["data"]
                if d.get("score", 0) < cfg["min_score"] or d.get("stickied"):
                    continue
                out.append(Item(
                    title=d.get("title", ""),
                    url="https://reddit.com" + d.get("permalink", ""),
                    source=f"reddit/r/{sub}", tag="reddit",
                    published=datetime.fromtimestamp(d["created_utc"], timezone.utc).isoformat(),
                    summary=(d.get("selftext") or "")[:600],
                    weight=0.85, social=int(d.get("score") or 0),
                ))
        except Exception as exc:
            log.warning("reddit r/%s упал: %s", sub, exc)
        time.sleep(1.0)
    log.info("ok reddit %d", len(out))
    return out


def _norm_title(t: str) -> str:
    """Заголовок к сравнимому виду: без хвоста издателя, пунктуации и регистра."""
    t = re.split(r"\s+[-–|]\s+", t)[0]          # Google News клеит " - Publisher"
    t = re.sub(r"[^\w\s]", " ", t.lower(), flags=re.UNICODE)
    return " ".join(t.split())


def dedupe_by_title(items: list["Item"], overlap: float = 0.6) -> list["Item"]:
    """Убирает один сюжет, пришедший из разных источников.

    Сравнивает по пересечению значимых слов. Оставляет вариант с большим weight,
    при равном — с большим social. Экономит вызовы LLM: до 40% записей — дубли.
    """
    kept: list[tuple[set, Item]] = []
    for it in sorted(items, key=lambda x: (-x.weight, -x.social)):
        words = {w for w in _norm_title(it.title).split() if len(w) > 3}
        if not words:
            kept.append((words, it))
            continue
        dup = False
        for seen_words, _ in kept:
            if not seen_words:
                continue
            inter = len(words & seen_words)
            if inter / min(len(words), len(seen_words)) >= overlap:
                dup = True
                break
        if not dup:
            kept.append((words, it))
    result = [it for _, it in kept]
    log.info("после схлопывания дублей по заголовку: %d из %d", len(result), len(items))
    return result


def collect_all(cfg: dict, hours: int) -> list[Item]:
    items = fetch_rss(cfg.get("rss", []), hours)
    items += fetch_hackernews(cfg.get("hackernews", {}), hours)
    items += fetch_reddit(cfg.get("reddit", {}))
    # дедуп внутри прогона по нормализованной ссылке
    uniq: dict[str, Item] = {}
    for it in items:
        if not it.title or not it.url:
            continue
        if it.key not in uniq or it.social > uniq[it.key].social:
            uniq[it.key] = it
    log.info("всего уникальных по ссылке: %d", len(uniq))
    return dedupe_by_title(list(uniq.values()))
