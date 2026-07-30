"""Какие модели Gemini доступны твоему ключу. Запуск: .\run.ps1 models"""
import os
import sys

import requests

key = os.environ.get("GEMINI_API_KEY")
if not key:
    print("GEMINI_API_KEY не задан — впиши в secrets.ps1")
    sys.exit(1)

r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                 headers={"x-goog-api-key": key}, timeout=30)
if not r.ok:
    print(f"HTTP {r.status_code}: {r.text[:500]}")
    sys.exit(1)

models = r.json().get("models", [])
usable = [m for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]

print(f"Моделей с generateContent: {len(usable)} из {len(models)}\n")
print(f"{'ИМЯ ДЛЯ GEMINI_MODEL':<42} {'ВХОД':>9} {'ВЫХОД':>7}  ОПИСАНИЕ")
print("-" * 110)

# flash вперёд: они щедрее по лимитам free tier
for m in sorted(usable, key=lambda x: ("flash" not in x["name"], x["name"])):
    name = m["name"].replace("models/", "")
    print(f"{name:<42} {m.get('inputTokenLimit', '?'):>9} "
          f"{m.get('outputTokenLimit', '?'):>7}  {m.get('displayName', '')[:40]}")

print("-" * 110)
print("\nВозьми имя из первого столбца и впиши в secrets.ps1:")
print('  $env:GEMINI_MODEL = "имя-модели"')
print("\nМожно указать несколько через запятую — будут пробоваться по очереди:")
print('  $env:GEMINI_MODEL = "gemini-2.5-flash,gemini-2.0-flash"')
