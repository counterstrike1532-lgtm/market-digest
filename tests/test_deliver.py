"""deliver.py: HTML-экранирование и чанкинг под Telegram (T9e). Без сети -
send()/send_photo() не вызываются, тестируем только _to_html/_chunks."""
from __future__ import annotations

import re

from src import deliver

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
