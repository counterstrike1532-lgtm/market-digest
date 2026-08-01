"""numbers.py: eurostat_hicp (JSON-stat decode + маркер устаревания) и
stooq_series (валидный CSV / анти-бот HTML). requests полностью замокан (T9a).
"""
from __future__ import annotations

from datetime import date

from src import numbers


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ---------------------------------------------------------------- T10f: _hicp_age_days

def test_hicp_age_days_computes_from_first_of_month():
    today = date.today()
    y, m = today.year, today.month - 2
    while m <= 0:
        m += 12
        y -= 1
    key = f"{y:04d}-{m:02d}"
    expected = (today - date(y, m, 1)).days
    assert numbers._hicp_age_days(key) == expected


def test_hicp_age_days_bad_format_is_none():
    assert numbers._hicp_age_days("not-a-period") is None
    assert numbers._hicp_age_days("") is None


# ---------------------------------------------------------------- eurostat_hicp

def _period_key(months_ago: int) -> str:
    today = date.today()
    y, m = today.year, today.month - months_ago
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def test_eurostat_hicp_single_time_period(monkeypatch):
    """T=1: один период на весь ответ, как обычно с lastTimePeriod=1. Период
    свежий (текущий месяц) - строка попадает в out без плашки-предупреждения
    (T10f: протухшее теперь не печатается вовсе, а не помечается)."""
    key = _period_key(0)
    payload = {
        "dimension": {
            "geo": {"category": {"index": {"PL": 0, "DE": 1}}},
            "time": {"category": {"index": {key: 0}, "label": {key: "2026M05"}}},
        },
        "value": {"0": 3.2, "1": 2.1},
    }
    monkeypatch.setattr(numbers.requests, "get",
                        lambda *a, **kw: FakeResponse(200, json_data=payload))
    out = numbers.eurostat_hicp(["PL", "DE"])
    assert out["HICP PL"]["value"] == 3.2
    assert out["HICP PL"]["as_of"] == "2026M05"
    assert "старше" not in out["HICP PL"]["as_of"]
    assert out["HICP DE"]["value"] == 2.1


def test_eurostat_hicp_two_time_periods_no_crosstalk(monkeypatch):
    """T=2: несколько периодов в ответе - значение и период должны декодироваться
    из одной и той же позиции для каждой geo, без обмена между geo/периодами."""
    key_old, key_new = _period_key(1), _period_key(0)
    payload = {
        "dimension": {
            "geo": {"category": {"index": {"PL": 0, "DE": 1}}},
            "time": {"category": {"index": {key_old: 0, key_new: 1},
                                  "label": {key_old: "OLD", key_new: "2026M05"}}},
        },
        # разреженный ответ: только (PL, key_new) pos=1 и (DE, key_new) pos=3
        "value": {"1": 3.5, "3": 2.2},
    }
    monkeypatch.setattr(numbers.requests, "get",
                        lambda *a, **kw: FakeResponse(200, json_data=payload))
    out = numbers.eurostat_hicp(["PL", "DE"])
    assert out["HICP PL"]["value"] == 3.5
    assert out["HICP PL"]["as_of"] == "2026M05"
    assert out["HICP DE"]["value"] == 2.2
    assert out["HICP DE"]["as_of"] == "2026M05"


def test_eurostat_hicp_network_error_returns_empty(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(numbers.requests, "get", boom)
    assert numbers.eurostat_hicp(["PL"]) == {}


def test_eurostat_hicp_stale_period_dropped_entirely(monkeypatch, caplog):
    """T10f: период старше 60 дней (8 месяцев, как в боевом прогоне) - блок
    вообще не попадает в out, факт пропуска виден в логе с периодом."""
    key = _period_key(8)
    payload = {
        "dimension": {
            "geo": {"category": {"index": {"DE": 0, "PL": 1}}},
            "time": {"category": {"index": {key: 0}, "label": {key: "2025M12"}}},
        },
        "value": {"0": 2.0, "1": 2.5},
    }
    monkeypatch.setattr(numbers.requests, "get",
                        lambda *a, **kw: FakeResponse(200, json_data=payload))
    import logging
    with caplog.at_level(logging.INFO):
        out = numbers.eurostat_hicp(["DE", "PL"])
    assert out == {}
    assert key in caplog.text
    assert "DE" in caplog.text


def test_eurostat_hicp_fresh_period_kept_without_marker(monkeypatch):
    """Свежий период (30 дней) - блок печатается, без плашки-предупреждения."""
    key = _period_key(1)
    payload = {
        "dimension": {
            "geo": {"category": {"index": {"PL": 0}}},
            "time": {"category": {"index": {key: 0}, "label": {key: "FRESH"}}},
        },
        "value": {"0": 2.5},
    }
    monkeypatch.setattr(numbers.requests, "get",
                        lambda *a, **kw: FakeResponse(200, json_data=payload))
    out = numbers.eurostat_hicp(["PL"], max_age_days=60)
    assert out["HICP PL"]["value"] == 2.5
    assert out["HICP PL"]["as_of"] == "FRESH"


# ---------------------------------------------------------------- stooq_series

_VALID_CSV = (
    "Data,Otwarcie,Najwyzszy,Najnizszy,Zamkniecie,Wolumen\n"
    "2026-06-01,100.0,101.0,99.0,100.5,1000\n"
    "2026-06-02,100.5,102.0,100.0,101.2,1100\n"
    "2026-06-03,101.2,103.0,101.0,102.0,1050\n"
    "2026-06-04,102.0,102.5,101.5,101.8,900\n"
    "2026-06-05,101.8,103.5,101.5,103.0,1200\n"
)

_ANTIBOT_HTML = "<html><body>This site requires JavaScript to run correctly.</body></html>"


def test_stooq_series_valid_csv(monkeypatch):
    monkeypatch.setattr(numbers.requests, "get",
                        lambda *a, **kw: FakeResponse(200, text=_VALID_CSV))
    scalars, series = numbers.stooq_series({"wig20": "wig20"})
    assert scalars["wig20"]["value"] == 103.0
    assert scalars["wig20"]["as_of"] == "2026-06-05"
    assert round(scalars["wig20"]["chg_1d_pct"], 2) == round((103.0 / 101.8 - 1) * 100, 2)
    assert round(scalars["wig20"]["chg_1m_pct"], 2) == round((103.0 / 100.5 - 1) * 100, 2)
    assert series["wig20"]["close"] == [100.5, 101.2, 102.0, 101.8, 103.0]
    assert len(series["wig20"]["dates"]) == 5


def test_stooq_series_antibot_html_is_skipped_not_crashed(monkeypatch):
    monkeypatch.setattr(numbers.requests, "get",
                        lambda *a, **kw: FakeResponse(200, text=_ANTIBOT_HTML))
    scalars, series = numbers.stooq_series({"wig20": "wig20"})
    assert scalars == {}
    assert series == {}


def test_stooq_series_mixed_symbols_one_fails_one_ok(monkeypatch):
    def fake_get(url, params=None, **kw):
        text = _VALID_CSV if params["s"] == "wig20" else _ANTIBOT_HTML
        return FakeResponse(200, text=text)

    monkeypatch.setattr(numbers.requests, "get", fake_get)
    scalars, series = numbers.stooq_series({"wig20": "wig20", "sp500": "^spx"})
    assert "wig20" in scalars and "sp500" not in scalars


# ---------------------------------------------------------------- T9c: yfinance каскад

class FakeHistory:
    """Достаточно pandas-подобного объекта для yfinance_series: .index с
    .strftime и .tolist() на колонке "Close"."""
    def __init__(self, dates, closes):
        import pandas as pd
        self._df = pd.DataFrame({"Close": closes},
                                index=pd.to_datetime(dates))

    def __getitem__(self, key):
        return self._df[key]

    @property
    def index(self):
        return self._df.index


class FakeTicker:
    def __init__(self, responses):
        self._responses = responses   # {ticker: (dates, closes) | Exception}

    def __call__(self, ticker):
        self._current = ticker
        return self

    def history(self, period=None, interval=None):
        resp = self._responses[self._current]
        if isinstance(resp, Exception):
            raise resp
        dates, closes = resp
        return FakeHistory(dates, closes)


_DATES_5 = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05"]
_CLOSES_5 = [100.5, 101.2, 102.0, 101.8, 103.0]


def test_yfinance_series_valid(monkeypatch):
    fake = FakeTicker({"WIG20.WA": (_DATES_5, _CLOSES_5)})
    monkeypatch.setattr(numbers.yf, "Ticker", fake)
    scalars, series = numbers.yfinance_series({"wig20": "WIG20.WA"})
    assert scalars["wig20"]["value"] == 103.0
    assert scalars["wig20"]["as_of"] == "2026-06-05"
    assert series["wig20"]["close"] == _CLOSES_5


def test_yfinance_series_too_few_rows_is_skipped(monkeypatch):
    fake = FakeTicker({"WIG20.WA": (_DATES_5[:2], _CLOSES_5[:2])})
    monkeypatch.setattr(numbers.yf, "Ticker", fake)
    scalars, series = numbers.yfinance_series({"wig20": "WIG20.WA"})
    assert scalars == {} and series == {}


def test_yfinance_series_exception_is_caught(monkeypatch):
    fake = FakeTicker({"WIG20.WA": RuntimeError("network down")})
    monkeypatch.setattr(numbers.yf, "Ticker", fake)
    scalars, series = numbers.yfinance_series({"wig20": "WIG20.WA"})
    assert scalars == {} and series == {}


def test_market_series_falls_back_to_stooq_for_missing_symbol(monkeypatch, caplog):
    """T10g fix 3: yfinance первым, stooq - только фолбэк на то, чего yfinance
    не отдал (было наоборот - stooq первым стабильно давал JS-заглушку по
    всем тикерам). yfinance отдал wig20, но не sp500 - фолбэк должен добрать
    только sp500 через stooq и явно залогировать источник."""
    def fake_yfinance(symbols):
        return ({"wig20": {"value": 2000.0, "as_of": "2026-06-05"}},
               {"wig20": {"dates": _DATES_5, "close": _CLOSES_5}})

    def fake_stooq(symbols):
        assert symbols == {"sp500": "^spx"}
        return ({"sp500": {"value": 5000.0, "as_of": "2026-06-05"}},
               {"sp500": {"dates": _DATES_5, "close": _CLOSES_5}})

    monkeypatch.setattr(numbers, "yfinance_series", fake_yfinance)
    monkeypatch.setattr(numbers, "stooq_series", fake_stooq)

    with caplog.at_level("INFO"):
        scalars, series, source_map = numbers.market_series({
            "stooq_symbols": {"wig20": "wig20", "sp500": "^spx"},
            "yfinance_symbols": {"wig20": "WIG20.WA", "sp500": "^GSPC"},
        })

    # market_series() переименовывает "wig20" в "WIG20 TR (ETF)" на выходе -
    # это цена пая TR-ETF, а не уровень индекса (T9 fix 2)
    assert scalars["WIG20 TR (ETF)"]["value"] == 2000.0
    assert scalars["sp500"]["value"] == 5000.0
    assert source_map == {"WIG20 TR (ETF)": "yfinance", "sp500": "stooq"}
    assert "stooq" in caplog.text
    assert "yfinance" in caplog.text.lower()


def test_market_series_does_not_call_stooq_when_yfinance_fully_succeeds(monkeypatch):
    def fake_yfinance(symbols):
        return ({"wig20": {"value": 2000.0}}, {"wig20": {"dates": [], "close": []}})

    def boom(symbols):
        raise AssertionError("stooq не должен вызываться, если yfinance отдал всё")

    monkeypatch.setattr(numbers, "yfinance_series", fake_yfinance)
    monkeypatch.setattr(numbers, "stooq_series", boom)

    scalars, series, source_map = numbers.market_series({"yfinance_symbols": {"wig20": "WIG20.WA"}})
    assert scalars == {"WIG20 TR (ETF)": {"value": 2000.0}}
    assert source_map == {"WIG20 TR (ETF)": "yfinance"}


def test_market_series_both_sources_fail_skips_gracefully(monkeypatch):
    monkeypatch.setattr(numbers, "stooq_series", lambda symbols: ({}, {}))
    monkeypatch.setattr(numbers, "yfinance_series", lambda symbols: ({}, {}))

    scalars, series, source_map = numbers.market_series({
        "stooq_symbols": {"wig20": "wig20"},
        "yfinance_symbols": {"wig20": "WIG20.WA"},
    })
    assert scalars == {} and series == {} and source_map == {}
