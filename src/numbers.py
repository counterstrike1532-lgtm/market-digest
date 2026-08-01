"""Свежие цифры из первоисточников. Это то, что превращает пересказ новостей
в пост, которого больше ни у кого нет. Все API бесплатные, FRED — опциональный ключ.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from datetime import date, timedelta

import requests
import yfinance as yf

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-news-digest/1.0)"}

# stooq отдаёт анти-бот "This site requires JavaScript" на дефолтный/непохожий-на-браузер
# User-Agent. Браузерный UA + Accept как у обычного GET из Chrome.
STOOQ_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "pl,en-US;q=0.9,en;q=0.8",
}


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
                             headers=STOOQ_HEADERS, timeout=20)
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


def yfinance_series(symbols: dict) -> tuple[dict, dict]:
    """Тот же формат вывода, что и stooq_series (T9c) — вызывающий код не видит
    разницы, какой источник в итоге отдал данные. Используется только как
    каскадный фолбэк на символы, которых stooq не дал (см. market_series)."""
    out, series = {}, {}
    for name, ticker in symbols.items():
        try:
            hist = yf.Ticker(ticker).history(period="2mo", interval="1d")
            closes = [round(float(v), 2) for v in hist["Close"].tolist()]
            dates = [d.strftime("%Y-%m-%d") for d in hist.index]
            if len(closes) < 5:
                log.warning("yfinance %s: получили только %d валидных строк "
                           "(нужно >=5) - пропускаю", ticker, len(closes))
                continue
            out[name] = {
                "value": round(closes[-1], 2),
                "chg_1d_pct": round((closes[-1] / closes[-2] - 1) * 100, 2),
                "chg_1m_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
                "as_of": dates[-1],
            }
            series[name] = {"dates": dates, "close": closes}
        except Exception as exc:
            log.warning("yfinance %s упал: %s", ticker, exc)
    return out, series


# "wig20" в конфиге - общий короткий хендл для fetch-слоя (stooq_symbols,
# yfinance_symbols), но фактический инструмент, который сейчас реально отдаёт
# данные (stooq мёртв, см. ГРАБЛИ) - ETFBW20TR.WA, пай TR-ETF, а не уровень
# индекса. Цена пая (~80) и уровень WIG20 (~2500+) - разные по порядку числа;
# оставь ключ "wig20" как есть, и число утечёт в FRESH DATA, откуда модель
# процитирует его как "уровень WIG20" - заведомо неверная цифра под реальным
# источником (T9 fix 2). Переименование - на выходе market_series(), поэтому
# ЦИФРЫ/FRESH DATA/график получают верную подпись одним и тем же способом.
_DISPLAY_NAME = {"wig20": "WIG20 TR (ETF)"}


def _relabel(d: dict) -> dict:
    return {_DISPLAY_NAME.get(k, k): v for k, v in d.items()}


def market_series(data_cfg: dict) -> tuple[dict, dict, dict]:
    """Каскад для индексов: yfinance первым, stooq - только для символов,
    которых yfinance не отдал, как исторический фолбэк (T10g fix 3). Раньше
    было наоборот - stooq первым стабильно отдавал JS-заглушку (антибот
    требует JS, см. ГРАБЛИ) по всем трём тикерам, три впустую потраченных
    запроса и ~3.5с на каждый прогон, прежде чем каскад вообще добирался до
    yfinance. Формат scalars/series идентичен stooq_series - main.py/charts.py
    подмены не видят, кроме переименования по _DISPLAY_NAME. Третье значение -
    source_map {symbol_name: "yfinance"|"stooq"}, для метрик прогона (T9d) и
    подписи источника на графике (T9 fix 1), в сводку/промпт не идёт."""
    yf_symbols = data_cfg.get("yfinance_symbols", {})
    scalars, series = yfinance_series(yf_symbols)
    source_map = {name: "yfinance" for name in scalars}
    missing = [name for name in yf_symbols if name not in scalars]
    if missing:
        stooq_symbols = data_cfg.get("stooq_symbols", {})
        targets = {name: stooq_symbols[name] for name in missing if name in stooq_symbols}
        if targets:
            stooq_scalars, stooq_series_data = stooq_series(targets)
            if stooq_scalars:
                log.info("рынки: stooq (yfinance недоступен) - %s",
                        ", ".join(sorted(stooq_scalars)))
            scalars.update(stooq_scalars)
            series.update(stooq_series_data)
            source_map.update({name: "stooq" for name in stooq_scalars})
    return _relabel(scalars), _relabel(series), _relabel(source_map)


def _hicp_age_days(period_key: str) -> int | None:
    """period_key в формате "YYYY-MM" (первое число месяца - HICP это месячная
    серия, точнее числа тут нет). Не распарсился - None (не утверждаем возраст,
    который не смогли посчитать)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", period_key or "")
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    try:
        period_start = date(y, mo, 1)
    except ValueError:
        return None
    return (date.today() - period_start).days


def eurostat_hicp(geos: list[str], max_age_days: int = 60) -> dict:
    """Годовая инфляция HICP. Официальный Eurostat API, без ключа.

    JSON-stat кодирует "value" плоским индексом по ВСЕМ измерениям сразу;
    freq/unit/coicop здесь размера 1, единственные варьируются geo и time,
    причём time — последнее измерение и меняется быстрее: index = i_geo*T + i_time
    (T = число периодов в ответе). Раньше код молча предполагал T==1 и брал
    период "list(...)[0]" один на все geo - если сервер вернул больше одного
    периода (или под другую geo давность отличается), значения и период тихо
    расходились. Теперь период и значение декодируются из одного и того же pos
    для каждой geo независимо - подмены нет по построению.

    Период старше max_age_days - запись вообще не попадает в out (T10f): декабрьский
    HICP в августе не объясняет ни ставки NBP, ни депозитные ставки банков, а честная
    плашка-предупреждение на бесполезном числе всё равно занимает место в сводке.
    Факт пропуска - в лог, с периодом, чтобы не потерялся молча."""
    out = {}
    try:
        r = requests.get(
            "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr",
            params={"format": "JSON", "coicop": "CP00", "unit": "RCH_A",
                    "lastTimePeriod": 1, "geo": geos}, headers=HEADERS, timeout=25)
        r.raise_for_status()
        js = r.json()
        geo_idx = js["dimension"]["geo"]["category"]["index"]        # geo_code -> i_geo
        time_idx = js["dimension"]["time"]["category"]["index"]      # period_key -> i_time
        time_label = js["dimension"]["time"]["category"].get("label", {})
        n_time = len(time_idx) or 1
        rev_geo = {i: g for g, i in geo_idx.items()}
        rev_time = {i: t for t, i in time_idx.items()}
        for pos, val in js["value"].items():
            i_geo, i_time = divmod(int(pos), n_time)
            geo_code = rev_geo.get(i_geo, "?")
            period_key = rev_time.get(i_time, "?")
            age_days = _hicp_age_days(period_key)
            if age_days is not None and age_days > max_age_days:
                log.info("HICP %s пропущен - период %s старше %d дней (%d)",
                         geo_code, period_key, max_age_days, age_days)
                continue
            period_label = time_label.get(period_key, period_key)
            out[f"HICP {geo_code}"] = {"value": val, "unit": "% г/г", "as_of": period_label}
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
    Служебные ключи "_series" (сырые ряды для графиков) и "_market_source"
    (какой источник отдал каждый индекс, для метрик T9d) main.py забирает
    до того, как data уйдёт в сводку/промпт черновиков."""
    d = cfg.get("data", {})
    snap = {}
    snap.update(nbp_fx(d.get("nbp_currencies", [])))
    market_scalars, market_series_data, market_source = market_series(d)
    snap.update(market_scalars)
    if market_series_data:
        snap["_series"] = market_series_data
    if market_source:
        snap["_market_source"] = market_source
    snap.update(eurostat_hicp(d.get("eurostat_hicp", [])))
    snap.update(fred_series(d.get("fred_series", {})))
    non_service_keys = sum(1 for k in snap if not k.startswith("_"))
    log.info("цифр собрано: %d", non_service_keys)
    return snap
