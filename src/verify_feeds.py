"""Проверка живости RSS-фидов. Запуск: .\run.ps1 verify

Показывает HTTP-код, чтобы различать причины:
  404/410  -> фида нет, ищи новый адрес или удаляй
  403      -> сервер режет ботов; иногда лечится, иногда нет
  200 + 0  -> отвечает, но записей нет: не тот URL или пустая лента
"""
import pathlib
import sys

import feedparser
import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
feeds = cfg.get("rss", [])

print(f"Проверяю {len(feeds)} фидов...\n")
print(f"{'СТАТУС':<9} {'КОД':>4} {'ЗАПИСЕЙ':>8}  URL")
print("-" * 100)

alive, problems = [], []
for f in feeds:
    url = f["url"]
    code, n, status = "-", 0, "МЁРТВ"
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        code = r.status_code
        n = len(feedparser.parse(r.content).entries)
        status = "OK" if n else ("БЛОК" if code == 403 else "ПУСТО")
    except requests.exceptions.SSLError:
        status = "SSL"
    except requests.exceptions.Timeout:
        status = "ТАЙМАУТ"
    except Exception as exc:
        status = type(exc).__name__[:8]

    print(f"{status:<9} {str(code):>4} {n:>8}  {url}")
    (alive if n else problems).append((url, status, code))

print("-" * 100)
print(f"\nЖивых: {len(alive)} из {len(feeds)}")

if problems:
    print(f"\nПроблемные ({len(problems)}) — скинь этот список:")
    for url, status, code in problems:
        print(f"  [{status} {code}] {url}")
else:
    print("\nВсе фиды живы, ничего править не надо.")

sys.exit(0)
