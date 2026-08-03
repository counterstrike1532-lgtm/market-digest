"""Общий сброс состояния brain.py между тестами (T13a).

brain.rank()/_call() мутируют модульные счётчики (квота, флаг деградации rank)
напрямую, не через monkeypatch - без сброса между файлами один тест мог
незаметно утечь состояние в другой. Конкретный случай, который это ловит:
test_degraded.py гоняет настоящий brain.rank() и выставляет
_last_rank_degraded=True; test_main.py подменяет саму brain.rank целиком
lambda-моком и никогда не трогает этот флаг явно - без сброса main.main()
в test_main.py читал бы устаревшее True от предыдущего теста другого файла.
"""
from __future__ import annotations

import pytest

from src import brain


@pytest.fixture(autouse=True)
def _reset_brain_module_state():
    brain._requests_made = 0
    brain._successful_calls = 0
    brain._quota_refusals = 0
    brain._day_exhausted = set()
    brain._last_model = None
    brain._last_rank_degraded = False
    yield
