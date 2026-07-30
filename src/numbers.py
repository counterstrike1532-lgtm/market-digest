"""Свежие цифры из первоисточников. Это то, что превращает пересказ новостей
в пост, которого больше ни у кого нет. Все API бесплатные, FRED — опциональный ключ.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-news-digest/1.0)"}


def nbp_fx(codes: list[str]) -> dict:
    """Курс NBP + изменение за 30 дней. api.nbp.pl, без ключа."""
    out = {}
    for c in codes:
        try:
            r = requests.get(f"https://api.nbp.pl/api/exchangerates/rates/a/{c}/last/30/",
                             params={"format": "json"}, headers=HEADERS, timeout=20)
            r.raise_for_status()
            rates = r.json()["rates"]
            first, last = rates[0]["mid"], rates[-1]["mid"]
            out[f"PLN/{c.upper()}"] = {
                "value": round(last, 4),
                "chg_30d_pct": round((last / first - 1) * 100, 2),
                "as_of": rates[-1]["effectiveDate"],
            }
        except Exception as exc:
            log.warning("NBP %s упал: %s", c, exc)
    return out


def stooq_series(symbols: dict) -> tuple[dict, dict]:
    """Индексы со stooq.pl в CSV. Без ключа, покрывает и GPW, и США.

    Возвращает (scalars, series): scalars — как раньше (последнее значение +
    изменения), series — весь ряд дат/close по тем же именам, для графиков.
    CSV и так скачивается целиком, раньше ряд просто выбрасывался.
    """
    out, series = {}, {}
    d1 = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    for name, sym in symbols.items():
        try:
            r = requests.get("https://stooq.pl/q/d/l/",
                             params={"s": sym, "d1": d1, "d2": d2, "i": "d"},
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.text)))
            closes = [float(x["Zamkniecie"]) for x in rows if x.get("Zamkniecie")]
            if len(closes) < 5:
                log.warning(
                    "stooq %s: получили только %d валидных строк (нужно >=5) - "
                    "пропускаю. Начало ответа: %r", sym, len(closes), r.text[:150])
                continue
            out[name] = {
                "value": round(closes[-1], 2),
                "chg_1d_pct": round((closes[-1] / closes[-2] - 1) * 100, 2),
                "chg_1m_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
                "as_of": rows[-1].get("Data"),
            }
            series[name] = {
                "dates": [x["Data"] for x in rows if x.get("Zamkniecie")],
                "close": closes,
            }
        except Exception as exc:
            log.warning("stooq %s упал: %s", sym, exc)
    return out, series


def eurostat_hicp(geos: list[str]) -> dict:
    """Годовая инфляция HICP. Официальный Eurostat API, без ключа."""
    out = {}
    try:
        r = requests.get(
            "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr",
            params={"format": "JSON", "coicop": "CP00", "unit": "RCH_A",
                    "lastTimePeriod": 1, "geo": geos}, headers=HEADERS, timeout=25)
        r.raise_for_status()
        js = r.json()
        geo_idx = js["dimension"]["geo"]["category"]["index"]
        period = list(js["dimension"]["time"]["category"]["label"].values())[0]
        rev = {v: k for k, v in geo_idx.items()}
        for pos, val in js["value"].items():
            out[f"HICP {rev.get(int(pos) % len(rev), '?')}"] = {
                "value": val, "unit": "% г/г", "as_of": period}
    except Exception as exc:
        log.warning("Eurostat упал: %s", exc)
    return out


def fred_series(series: dict) -> dict:
    """Опционально: нужен бесплатный ключ с fred.stlouisfed.org."""
    key = os.getenv("FRED_API_KEY")
    if not key:
        log.info("FRED_API_KEY нет — пропускаем (не критично)")
        return {}
    out = {}
    for name, sid in series.items():
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": sid, "api_key": key, "file_type": "json",
                                     "sort_order": "desc", "limit": 13},
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            obs = [o for o in r.json()["observations"] if o["value"] != "."]
            if not obs:
                continue
            latest = float(obs[0]["value"])
            out[name] = {"value": latest, "as_of": obs[0]["date"]}
            if len(obs) >= 13:
                out[name]["chg_1y_pct"] = round((latest / float(obs[12]["value"]) - 1) * 100, 2)
        except Exception as exc:
            log.warning("FRED %s упал: %s", sid, exc)
    return out


def gather(cfg: dict) -> dict:
    """Формат вывода не меняется: снимок плоский, per-symbol записи как раньше.
    Единственное добавление — ключ "_series" с сырыми рядами stooq для графиков
    (main.py забирает его до того, как data уйдёт в сводку/промпт черновиков)."""
    d = cfg.get("data", {})
    snap = {}
    snap.update(nbp_fx(d.get("nbp_currencies", [])))
    stooq_scalars, stooq_series_data = stooq_series(d.get("stooq_symbols", {}))
    snap.update(stooq_scalars)
    if stooq_series_data:
        snap["_series"] = stooq_series_data
    snap.update(eurostat_hicp(d.get("eurostat_hicp", [])))
    snap.update(fred_series(d.get("fred_series", {})))
    log.info("цифр собрано: %d", len(snap) - (1 if "_series" in snap else 0))
    return snap
