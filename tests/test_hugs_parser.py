"""Тесты для hugs_parser и hugs_workflow (на моках, без живой сети и ключей)."""
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import bs4
import pytest

from src.hugs_parser import (
    HugsPost,
    clean_post_text,
    fetch_channel_posts,
    filter_posts,
    get_prev_page_before_id,
    is_promo_post,
    parse_page_posts,
    parse_post_from_html,
)
from src.hugs_workflow import (
    build_final_message,
    format_posts_for_prompt,
    run_llm_analysis,
    run_workflow,
)

# Образец HTML Telegram-канала с разными типами сообщений
SAMPLE_HTML = """
<div class="tgme_channel_history js-message_history">
  <!-- Обычный пост с новостью -->
  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="HugsFund/101">
      <div class="tgme_widget_message_bubble">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          <b>Мінфін США</b> провів аукціон 30-річних облігацій.<br/>
          Дохідність склала 5.216%, що є максимумом з 2001 року.<br/><br/>
          <a href="https://www.bloomberg.com/news/articles/sample">Bloomberg</a>
          <br/><br/>
          Сайт | LifeHUGS | AgentHUGS | YouTUBE Еріка Наймана
        </div>
      </div>
      <div class="tgme_widget_message_footer">
        <div class="tgme_widget_message_info">
          <a class="tgme_widget_message_date" href="https://t.me/HugsFund/101">
            <time class="time" datetime="2026-08-20T14:00:00+00:00">16:00</time>
          </a>
        </div>
      </div>
    </div>
  </div>

  <!-- Пост с видео (где есть тег time для длительности) -->
  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="HugsFund/102">
      <div class="tgme_widget_message_bubble">
        <time class="message_video_duration">0:45</time>
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          Anthropic готує новий клас акцій із посиленим правом голосу.<br/>
          Джерело: The Information.
        </div>
      </div>
      <div class="tgme_widget_message_footer">
        <div class="tgme_widget_message_info">
          <time class="time" datetime="2026-08-20T12:30:00+00:00">14:30</time>
        </div>
      </div>
    </div>
  </div>

  <!-- Рекламный пост (ссылка на Google Forms) -->
  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="HugsFund/103">
      <div class="tgme_widget_message_bubble">
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          Запрошуємо на відкритий вебінар по інвестиціям!<br/>
          Реєстрація за посиланням: <a href="https://forms.gle/sample123">forms.gle</a>
        </div>
      </div>
      <div class="tgme_widget_message_footer">
        <time class="time" datetime="2026-08-20T11:00:00+00:00">13:00</time>
      </div>
    </div>
  </div>

  <!-- Пост без текста (только картинка/стикер) -->
  <div class="tgme_widget_message_wrap js-widget_message_wrap">
    <div class="tgme_widget_message js-widget_message" data-post="HugsFund/104">
      <div class="tgme_widget_message_bubble"></div>
      <div class="tgme_widget_message_footer">
        <time class="time" datetime="2026-08-20T10:00:00+00:00">12:00</time>
      </div>
    </div>
  </div>
</div>
<link rel="prev" href="/s/HugsFund?before=101" />
"""


def test_parse_page_posts():
    posts = parse_page_posts(SAMPLE_HTML)
    # Из 4 элементов 1 без текста -> должно быть 3 поста
    assert len(posts) == 3

    p1 = posts[0]
    assert p1.post_id == "HugsFund/101"
    assert p1.url == "https://t.me/HugsFund/101"
    assert p1.published_at == datetime(2026, 8, 20, 14, 0, 0, tzinfo=timezone.utc)
    assert "Мінфін США" in p1.text
    assert "5.216%" in p1.text
    assert "https://www.bloomberg.com/news/articles/sample" in p1.links
    assert not p1.is_promo
    # Проверка очистки подвала канала
    assert "LifeHUGS" not in p1.clean_text
    assert "Мінфін США" in p1.clean_text

    p2 = posts[1]
    assert p2.post_id == "HugsFund/102"
    # Должен взять правильное время публикации, а не длительность видео (0:45)
    assert p2.published_at == datetime(2026, 8, 20, 12, 30, 0, tzinfo=timezone.utc)
    assert "Anthropic" in p2.text

    p3 = posts[2]
    assert p3.post_id == "HugsFund/103"
    assert p3.is_promo is True


def test_get_prev_page_before_id():
    before_id = get_prev_page_before_id(SAMPLE_HTML)
    assert before_id == 101

    html_no_prev = "<div>no links</div>"
    assert get_prev_page_before_id(html_no_prev) is None


def test_is_promo_post_links():
    assert is_promo_post("Обычный текст", ["https://forms.gle/abc"]) is True
    assert is_promo_post("Обычный текст", ["https://docs.google.com/forms/d/e/123/viewform"]) is True
    assert is_promo_post("Обычный текст", ["https://lu.ma/event-123"]) is True
    assert is_promo_post("Обычный текст", ["https://zoom.us/webinar/register/123"]) is True
    assert is_promo_post("Обычный текст", ["https://bloomberg.com/news/123"]) is False


def test_is_promo_post_text():
    assert is_promo_post("Зареєструватися на вебінар можна тут", []) is True
    assert is_promo_post("Спеціальна ціна та знижка на курс з аналітики", []) is True
    assert is_promo_post("Приєднуйтесь до закритого клубу інвесторів", []) is True
    assert is_promo_post("Купити підписку на аналітику зі знижкою", []) is True
    assert is_promo_post("Реєстрація на інтенсив відкрита", []) is True
    assert is_promo_post("ФРС США зберегла ставку на рівні 5.25-5.50% [Reuters]", []) is False


def test_filter_posts():
    now = datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc)
    p_fresh = HugsPost(
        post_id="HugsFund/1",
        url="https://t.me/HugsFund/1",
        published_at=now - timedelta(hours=5),
        text="Свежая новость о рынке акций",
    )
    p_old = HugsPost(
        post_id="HugsFund/2",
        url="https://t.me/HugsFund/2",
        published_at=now - timedelta(hours=35),
        text="Старая новость 35 часов назад",
    )
    p_promo = HugsPost(
        post_id="HugsFund/3",
        url="https://t.me/HugsFund/3",
        published_at=now - timedelta(hours=2),
        text="Реєстрація на новий курс з інвестицій",
        is_promo=True,
    )
    p_empty = HugsPost(
        post_id="HugsFund/4",
        url="https://t.me/HugsFund/4",
        published_at=now - timedelta(hours=1),
        text="Сайт | LifeHUGS | AgentHUGS",  # После clean_text станет пустым
    )

    filtered = filter_posts([p_fresh, p_old, p_promo, p_empty], hours=30, now=now)
    assert len(filtered) == 1
    assert filtered[0].post_id == "HugsFund/1"


def test_fetch_channel_posts_pagination():
    mock_client = MagicMock()
    
    # Страница 1: свежие посты + ссылка на before=50
    p1_html = """
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-post="HugsFund/51">
        <div class="tgme_widget_message_text">Новость 51</div>
        <div class="tgme_widget_message_footer">
          <time class="time" datetime="2026-08-20T16:00:00+00:00"></time>
        </div>
      </div>
    </div>
    <link rel="prev" href="/s/HugsFund?before=50" />
    """
    
    # Страница 2: старые посты за пределами 30 часов
    p2_html = """
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-post="HugsFund/49">
        <div class="tgme_widget_message_text">Новость 49</div>
        <div class="tgme_widget_message_footer">
          <time class="time" datetime="2026-08-18T10:00:00+00:00"></time>
        </div>
      </div>
    </div>
    """

    resp1 = MagicMock()
    resp1.status_code = 200
    resp1.text = p1_html

    resp2 = MagicMock()
    resp2.status_code = 200
    resp2.text = p2_html

    mock_client.get.side_effect = [resp1, resp2]

    posts = fetch_channel_posts(channel="HugsFund", hours=30, max_pages=3, client=mock_client)
    assert len(posts) == 2
    assert posts[0].post_id == "HugsFund/49"
    assert posts[1].post_id == "HugsFund/51"


def test_format_posts_for_prompt():
    p = HugsPost(
        post_id="HugsFund/999",
        url="https://t.me/HugsFund/999",
        published_at=datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc),
        text="S&P 500 вырос на 1.2% на фоне отчетов [WSJ]\nСайт | LifeHUGS",
        links=["https://wsj.com/sample"],
    )
    formatted = format_posts_for_prompt([p])
    assert "POST 1 (ID: HugsFund/999" in formatted
    assert "Links: https://wsj.com/sample" in formatted
    assert "S&P 500 вырос на 1.2%" in formatted
    assert "LifeHUGS" not in formatted  # clean_text


def test_build_final_message():
    msg = build_final_message("Briefing analysis text", posts_count=5, hours=30)
    assert "📊 <b>HUGS FUND BRIEFING |" in msg
    assert "Filtered top stories from 5 posts (30h window)" in msg
    assert "Briefing analysis text" in msg


@patch("src.brain._call")
def test_run_llm_analysis(mock_call):
    mock_call.return_value = "• Treasury buybacks [Bloomberg]\n\n<b>DRAFT 1:</b> Digest text"
    p = HugsPost(
        post_id="HugsFund/1",
        url="https://t.me/HugsFund/1",
        published_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc),
        text="Inflation in eurozone 2.5%",
    )
    result = run_llm_analysis([p])
    assert "Treasury buybacks" in result
    assert "DRAFT 1" in result
    assert mock_call.called


@patch("src.hugs_workflow.fetch_channel_posts")
@patch("src.hugs_workflow.brain._call")
@patch("src.deliver.send")
def test_run_workflow_dry_run(mock_send, mock_brain, mock_fetch):
    now = datetime.now(timezone.utc)
    mock_fetch.return_value = [
        HugsPost(
            post_id="HugsFund/10",
            url="https://t.me/HugsFund/10",
            published_at=now - timedelta(hours=3),
            text="Equities rose [Bloomberg]",
        )
    ]
    mock_brain.return_value = "• Equities rose on tech earnings [Bloomberg]"

    out = run_workflow(hours=30, dry_run=True, send_telegram=False)
    assert out is not None
    assert "HUGS FUND BRIEFING" in out
    assert "Equities rose" in out
    # В dry_run отправка в Telegram не вызывается
    assert not mock_send.called


@patch("src.hugs_workflow.fetch_channel_posts")
@patch("src.hugs_workflow.brain._call")
@patch("src.deliver.send")
def test_run_workflow_send(mock_send, mock_brain, mock_fetch):
    now = datetime.now(timezone.utc)
    mock_fetch.return_value = [
        HugsPost(
            post_id="HugsFund/20",
            url="https://t.me/HugsFund/20",
            published_at=now - timedelta(hours=2),
            text="Brent crude prices dropped to $78 [Reuters]",
        )
    ]
    mock_brain.return_value = "• Brent crude at $78 [Reuters]"

    out = run_workflow(hours=30, dry_run=False, send_telegram=True)
    assert out is not None
    assert mock_send.called

