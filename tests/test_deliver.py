"""deliver.py: HTML-экранирование и чанкинг под Telegram (T9e, T9 fix 4, T10a). Без сети -
send()/send_photo() не вызываются, тестируем только _to_html/_chunks."""
from __future__ import annotations

import pathlib
import re

from src import deliver

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Реальный длинный Google News URL (обёртка редиректа, ~250+ символов без пробелов) -
# именно такие рвались на границе куска до T9e.
_LONG_GOOGLE_URL = (
    "https://news.google.com/rss/articles/"
    "CBMirwFBVV95cUxQbjNfZmFrZTEyMzQ1Njc4OTBhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5eg"
    "ekw2anlqbGtqYW5kYWx3a2VuYWxrc2R2anVoYXNsa2R2aG5sa2Fza2RsdmphaHNka3ZuYWpza2xkdmpu"
    "Ijc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkw"
    "?oc=5&hl=en-US&gl=US&ceid=US:en"
)


# ---------------------------------------------------------------- _to_html

def test_to_html_uses_domain_as_link_text_not_full_url():
    import html as html_mod

    text = f"source: {_LONG_GOOGLE_URL}"
    out = deliver._to_html(text)
    escaped_url = html_mod.escape(_LONG_GOOGLE_URL)
    assert f'<a href="{escaped_url}">news.google.com</a>' in out
    # текст ссылки короткий - весь длинный путь ушёл только в href, не повторён
    assert out.count(escaped_url) == 1


def test_to_html_escapes_surrounding_text():
    out = deliver._to_html("угол: 5 < 10 & кое-что")
    assert "&lt;" in out and "&amp;" in out


def test_to_html_multiline_with_urls_and_labels():
    """Метка следующего поля не должна прилипать к ссылке - между ними должен
    остаться перевод строки после рендера (T9e)."""
    text = f"   money.pl, 2026-07-30 — {_LONG_GOOGLE_URL}\n   угол: something"
    out = deliver._to_html(text)
    idx_a_close = out.index("</a>")
    idx_label = out.index("угол:")
    between = out[idx_a_close:idx_label]
    assert "\n" in between


# ---------------------------------------------------------------- чанкинг не рвёт URL

def test_split_avoiding_urls_never_cuts_inside_url():
    padding = "x" * 3850
    block = f"{padding}\n{_LONG_GOOGLE_URL} some trailing words"
    head, rest = deliver._split_avoiding_urls(block, deliver.LIMIT)
    assert _LONG_GOOGLE_URL not in head or _LONG_GOOGLE_URL in rest
    # ссылка целиком лежит в ОДНОЙ из двух половин, не размазана по обеим
    assert (head + rest) == block
    assert not (_LONG_GOOGLE_URL[:50] in head and _LONG_GOOGLE_URL[-50:] in rest)


def test_chunks_never_splits_a_url():
    padding_block = "x" * 3800
    text = f"{padding_block}\n\n{_LONG_GOOGLE_URL} — more text after the link"
    parts = deliver._chunks(text)
    # ссылка целиком присутствует в РОВНО одном куске
    hits = sum(1 for p in parts if _LONG_GOOGLE_URL in p)
    assert hits == 1
    # ни в одном куске нет ОБРЫВКА ссылки (начинается на https:// незакрытым)
    for p in parts:
        for m in re.finditer(r"https?://\S+", p):
            assert m.group() == _LONG_GOOGLE_URL or "news.google.com" not in m.group()


def test_chunks_and_html_render_produce_valid_anchor_tags_at_boundaries():
    """Реальная форма боевой сводки: длинный Google-News URL, многострочный
    FIGURES, заголовки DRAFT от рендерера - деление на куски по 3900 не должно
    резать <a>...</a> пополам ни в одном куске (T9e)."""
    story_block = (
        f"1. [8/10] Some story title that is reasonably long for a headline\n"
        f"   money.pl, 2026-07-30 — {_LONG_GOOGLE_URL}\n"
        f"   угол: something non-obvious about this story worth a look\n"
    )
    filler = "\n\n".join(f"filler paragraph number {i} " * 5 for i in range(40))
    draft_block = (
        "DRAFT 1 (digest)\n"
        "SHAPE: digest\n"
        "BODY: some body text.\n"
        "FIGURES: 100,000 PLN -> Statistics Poland\n"
        "- 185 billion euros -> NBP\n"
        f"SOURCE: {_LONG_GOOGLE_URL}\n"
        "WHY_THIS_ONE: reason\n"
        "VERDICT: SKIP\n"
        "WHY: no edge\n"
        "CHECK_FIRST: -\n"
        "ЦИФРЫ: ⚠️ 0/2 found, 2 unparsed - verify those by hand"
    )
    text = "\n\n".join([story_block, filler, draft_block])

    for part in deliver._chunks(text):
        html_part = deliver._to_html(part)
        # каждый <a ...> закрыт своим </a> в пределах ЭТОГО куска
        assert html_part.count("<a href=") == html_part.count("</a>")
        # href всегда указывает на полный, не обрубленный URL
        for m in re.finditer(r'<a href="([^"]*)">', html_part):
            href = m.group(1)
            assert href.startswith("https://") or href.startswith("http://")


# ---------------------------------------------------------------- T9 fix 4: голый домен в SOURCE

def test_to_html_wraps_bare_domain_known_from_selected():
    """Модель иногда путает "SOURCE:" с голым доменом ("www.cnbc.com") вместо
    URL - раньше это уходило в Telegram как обычный текст, и Telegram сам
    линковал его на корень сайта. Известный домен (из selected) оборачиваем
    в ссылку на конкретную статью явно."""
    domain_urls = {"www.cnbc.com": "https://www.cnbc.com/id/100003114/some-article"}
    text = "SOURCE: www.cnbc.com"
    out = deliver._to_html(text, domain_urls)
    assert '<a href="https://www.cnbc.com/id/100003114/some-article">www.cnbc.com</a>' in out
    # текст ссылки - домен, не превращается в site root
    assert "cnbc.com/id/100003114" in out


def test_to_html_bare_domain_unknown_stays_plain_text():
    """Домен, которого нет среди selected - не трогаем: угадывать URL не из чего."""
    text = "SOURCE: www.somewhere-unrelated.com"
    out = deliver._to_html(text, {"www.cnbc.com": "https://www.cnbc.com/id/1"})
    assert "<a href=" not in out
    assert "www.somewhere-unrelated.com" in out


def test_to_html_without_domain_urls_behaves_as_before():
    text = "SOURCE: www.cnbc.com"
    assert deliver._to_html(text) == deliver._to_html(text, None) == deliver._to_html(text, {})


def test_to_html_full_url_occurrence_not_touched_by_domain_pass():
    """Домен внутри полноценного https://-URL уже обработан URL_RE - доменный
    проход трогает только ГОЛЫЕ вхождения домена в тексте между ссылками, не
    лезет внутрь уже свёрнутых <a href> совпадений."""
    domain_urls = {"www.cnbc.com": "https://www.cnbc.com/id/other-article"}
    text = ("See www.cnbc.com at https://www.cnbc.com/id/100003114/real-article "
            "for details")
    out = deliver._to_html(text, domain_urls)
    assert out.count("<a href=") == 2
    # голое упоминание - ссылка из domain_urls
    assert 'href="https://www.cnbc.com/id/other-article">www.cnbc.com</a>' in out
    # реальный URL в тексте - ссылка на него самого, не подменена доменной
    assert 'href="https://www.cnbc.com/id/100003114/real-article"' in out


# ---------------------------------------------------------------- T10a: чанкер без дублей/потерь
#
# Фикстура - дословный текст реального прогона (владелец прислал три куска,
# скопированных из Телеграма; границы между ними в тексте не отмечены разметкой,
# только естественными абзацами). Прежняя версия этих тестов проверяла "ни одна
# подстрока 200+ символов не встречается в выводе дважды" глобально по всему
# тексту - и ловила ложное срабатывание: DRAFT 1 и DRAFT 2 (два дайджеста по
# одним и тем же трём сюжетам) законно повторяют почти одинаковые строки FIGURES
# в исходном, ЕЩЁ до чанкинга, тексте. Это не баг чанкера. Настоящий баг прогона
# 01.08.2026 был другим: кусок 3 повторял ХВОСТ куска 2 плюс весь DRAFT 3 -
# то есть дублирование возникало НА СТЫКЕ кусков, а не где-то в тексте вообще.
# Тесты ниже проверяют именно стык: каждый кусок обязан быть непрерывным,
# неперекрывающимся срезом исходного текста в порядке следования.

def test_chunks_concatenation_equals_original_on_real_run_fixture():
    """Главный инвариант T10a: "".join(chunks) == исходный текст побайтово."""
    text = (FIXTURES / "digest_20260801.txt").read_text(encoding="utf-8")
    parts = deliver._chunks(text)
    assert len(parts) > 1                      # фикстура заведомо длиннее одного куска
    assert "".join(parts) == text


def test_chunks_are_contiguous_nonoverlapping_slices_in_order():
    """Регрессия на реальный баг прогона 01.08.2026: кусок 3 повторял хвост
    куска 2 плюс весь DRAFT 3 целиком. Проверка стыка: part[i] обязан быть
    ровно text[pos:pos+len(part[i])] - ни один символ не взят из уже отданного
    диапазона (дублирование) и не пропущен (потеря)."""
    text = (FIXTURES / "digest_20260801.txt").read_text(encoding="utf-8")
    parts = deliver._chunks(text)
    pos = 0
    for i, part in enumerate(parts):
        assert text[pos:pos + len(part)] == part, f"кусок {i} не является срезом text[{pos}:]"
        pos += len(part)
    assert pos == len(text)


def test_chunks_short_text_is_a_single_chunk():
    text = "СВОДКА 01.08.2026\n\nкороткий текст без нужды резать"
    parts = deliver._chunks(text)
    assert parts == [text]


def test_chunks_empty_text_yields_no_chunks():
    assert deliver._chunks("") == []


def test_chunks_block_exactly_on_limit_keeps_last_char():
    """Блок ровно на границе лимита (после блока сразу конец текста) не теряет
    последний символ."""
    block = ("x" * (deliver.LIMIT - 1)) + "!"
    assert len(block) == deliver.LIMIT
    parts = deliver._chunks(block)
    assert "".join(parts) == block
    assert parts[-1].endswith("!")


def test_chunks_preserves_paragraph_breaks_inside_body():
    """Абзацы внутри BODY (одиночный \\n, не блок-граница) не должны терять или
    задваивать перенос строки на стыке кусков."""
    text = (FIXTURES / "digest_20260801.txt").read_text(encoding="utf-8")
    parts = deliver._chunks(text)
    rejoined = "".join(parts)
    # переносы строк по всему документу сохранены 1:1 после дробления на куски
    assert rejoined.count("\n") == text.count("\n")


def test_pieces_within_limit_never_exceeds_limit_except_unbreakable_url():
    """Каждый неделимый кусок не длиннее лимита - фикстура не содержит одиночных
    URL длиннее лимита, так что предел выдерживается без исключений."""
    text = (FIXTURES / "digest_20260801.txt").read_text(encoding="utf-8")
    for piece in deliver._pieces_within_limit(text):
        assert len(piece) <= deliver.LIMIT


def test_chunks_never_exceed_limit_on_fixture():
    text = (FIXTURES / "digest_20260801.txt").read_text(encoding="utf-8")
    for part in deliver._chunks(text):
        assert len(part) <= deliver.LIMIT


# ----------------------------------------------- T10a review: предел после _to_html, не до

_LONG_GOOGLE_URL_2 = (
    "https://news.google.com/rss/articles/"
    "CBMirwFBVV95cUxQbjNfZmFrZTEyMzQ1Njc4OTBhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5eg"
    "ekw2anlqbGtqYW5kYWx3a2VuYWxrc2R2anVoYXNsa2R2aG5sa2Fza2RsdmphaHNka3ZuYWpza2xkdmpu"
    "?oc=5&hl=en-US&gl=US&ceid=US:en"
)


def _url_run_no_split_points(n_urls: int, pad: int = 0) -> str:
    """n_urls длинных ссылок через пробел, без единого \\n и без знака конца
    предложения - ни блок, ни абзац, ни предложение не дают точки дробления,
    только жёсткий разрез (_split_avoiding_urls). pad добавляет обычный ASCII
    "балласт" в конец, чтобы точно попасть сырой длиной в узкий зазор между
    LIMIT (не режется по длине) и TELEGRAM_LIMIT/коэффициент раздутия ссылок
    (уже не помещается после _to_html)."""
    text = " ".join([_LONG_GOOGLE_URL_2] * n_urls)
    if pad:
        text += " " + ("x" * pad)
    return text


def test_chunks_respects_telegram_limit_after_html_even_when_raw_length_fits():
    """Плотный частокол ссылок без единой точки дробления (нет \\n, нет конца
    предложения) - раздутие после _to_html само по себе толкает кусок за
    TELEGRAM_LIMIT, хотя сырая длина ещё меньше LIMIT. Раньше это не
    проверялось вовсе: ранний выход "длина <= limit" в _pieces_within_limit
    срабатывал до того, как код успевал заглянуть внутрь блока, а жёсткий
    разрез резал по сырой длине и не перепроверял результат (T10a review)."""
    text = _url_run_no_split_points(15, pad=150)
    assert len(text) <= deliver.LIMIT                  # сырой текст ещё "помещается"
    assert len(deliver._to_html(text)) > deliver.TELEGRAM_LIMIT  # но не после рендера

    parts = deliver._chunks(text)
    assert "".join(parts) == text                       # инвариант не нарушен
    for part in parts:
        assert len(deliver._to_html(part)) <= deliver.TELEGRAM_LIMIT


def test_chunks_html_length_within_telegram_limit_on_fixture_with_domain_urls():
    """Реалистичный прогон: тот же domain_urls, что main.py передаёт в
    deliver.send() (домен -> URL статьи, T9 fix 4) - проверяем итоговую длину
    ПОСЛЕ _to_html, а не сырую."""
    text = (FIXTURES / "digest_20260801.txt").read_text(encoding="utf-8")
    domain_urls = {
        "bankier.pl": "https://www.bankier.pl/wiadomosc/Wielka-plyta-zagrozi-deweloperom-9012345.html",
        "money.pl": "https://www.money.pl/wiadomosc/Dunaj-wysycha-9023456.html",
    }
    parts = deliver._chunks(text, domain_urls)
    assert "".join(parts) == text
    for part in parts:
        assert len(deliver._to_html(part, domain_urls)) <= deliver.TELEGRAM_LIMIT
