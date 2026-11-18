"""Тесты для FT Premium IMAP-пайплайна (src/ft_imap.py и src/ft_workflow.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import brain, ft_imap, ft_workflow


def _make_long_ft_body(topic_line: str = "Corporate credit spreads widened 25 bps on $1.8 trillion debt issuance.") -> str:
    para1 = (
        f"{topic_line} Institutional borrowers face higher refinancing hurdles across the US and Europe, "
        "with investment-grade maturities reaching $950 billion over the next 18 months."
    )
    para2 = (
        "Private credit funds absorbed 42% of middle-market buyout volume in Q3, up from 31% a year earlier, "
        "as regional commercial banks pulled back from leveraged term loans due to capital reserve charges."
    )
    para3 = (
        "Interest coverage ratios across software issuers fell from 3.8x to 2.4x while base rates stayed elevated, "
        "leaving sponsors with shrinking cash buffers to absorb lower organic revenue growth."
    )
    return f"{para1}\n\n{para2}\n\n{para3}"


def _build_eml(
    subject: str,
    from_addr: str = "Financial Times <newsletters@ft.com>",
    to_addr: str = "inbox@gmail.com",
    message_id: str = "<ft-msg-1@ft.com>",
    date_dt: datetime | None = None,
    html_body: str | None = None,
    plain_body: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Message-ID"] = message_id
    dt = date_dt or datetime.now(timezone.utc)
    msg["Date"] = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    if plain_body is not None and html_body is not None:
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")
    elif html_body is not None:
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(plain_body or "")

    return msg.as_bytes()


# ------------------------------------------------------------------
# 1. Нормализация тем, белый список рассылок и фильтр отправителей
# ------------------------------------------------------------------

def test_normalize_subject_prefixes():
    assert ft_imap.normalize_subject("FW: Fwd: Re: ПЕРЕСЛ: Unhedged: Chart of the Week") == "Unhedged: Chart of the Week"
    assert ft_imap.normalize_subject("  fw:  [FWD]:  Due   Diligence:   M&A pipeline  ") == "Due Diligence: M&A pipeline"
    assert ft_imap.normalize_subject("пересл: Chris Giles on Central Banks") == "Chris Giles on Central Banks"


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("FW: Unhedged: Why private credit yields diverge", "Unhedged"),
        ("Fwd: Unhedged: Chart of the Week", "Unhedged"),
        ("ПЕРЕСЛ: Due Diligence: Carlyle buyout math", "Due Diligence"),
        ("Re: Europe Express: Brussels fiscal rules", "Europe Express"),
        ("FW: Energy Source: LNG contract pricing", "Energy Source"),
        ("The Lex Newsletter: Big Tech capex", "Lex"),
        ("FW: Lex: European bank capital ratios", "Lex"),
        ("Chris Giles on Central Banks: ECB balance sheet", "Central Banks"),
        ("Central Banks: BoE rate path", "Central Banks"),
    ],
)
def test_identify_all_six_whitelisted_newsletters(subject, expected):
    assert ft_imap.identify_newsletter(subject) == expected


def test_non_whitelisted_newsletter_ignored(caplog):
    caplog.set_level(logging.INFO)
    body = _make_long_ft_body()
    raw = _build_eml(
        subject="FW: FT Weekend Magazine: Best restaurants in London",
        from_addr="student@uni.edu",
        plain_body=f"Financial Times ft.com\n\n{body}",
    )
    nl = ft_imap.parse_raw_email(raw, forwarder_email="student@uni.edu")
    assert nl is None
    assert "белый список" in caplog.text


def test_sender_filter_direct_vs_outlook_forwarder():
    body = _make_long_ft_body()
    # Прямое письмо от ft.com
    raw_ft = _build_eml(
        subject="Unhedged: Rate cut expectations",
        from_addr="FT <unhedged@newsletters.ft.com>",
        plain_body=body,
    )
    nl_ft = ft_imap.parse_raw_email(raw_ft)
    assert nl_ft is not None
    assert nl_ft.newsletter_name == "Unhedged"

    # Пересылка с учебного Outlook-адреса (упоминание Financial Times в теле)
    raw_fwd = _build_eml(
        subject="ПЕРЕСЛ: Due Diligence: Private equity debt",
        from_addr="Uni Student <student@university.edu>",
        plain_body=f"From: FT <dd@ft.com>\nSent: Mon\nTo: student@university.edu\nSubject: Due Diligence\n\n{body}\nFinancial Times",
    )
    nl_fwd = ft_imap.parse_raw_email(raw_fwd, forwarder_email="student@university.edu")
    assert nl_fwd is not None
    assert nl_fwd.newsletter_name == "Due Diligence"

    # Посторонний отправитель без совпадения с forwarder_email и без ft.com
    raw_spam = _build_eml(
        subject="Unhedged: Promo deal",
        from_addr="spammer@random-domain.xyz",
        plain_body=f"Financial Times\n\n{body}",
    )
    assert ft_imap.parse_raw_email(raw_spam, forwarder_email="student@university.edu") is None


# ------------------------------------------------------------------
# 2. Очистка HTML: Outlook-шапка, style/script, preheader, FT-футеры
# ------------------------------------------------------------------

def test_clean_ft_html_strips_outlook_headers_scripts_preheaders_and_footers():
    body_text = _make_long_ft_body()
    p1, p2, p3 = body_text.split("\n\n")
    raw_html = f"""
    <html>
      <head>
        <style>.hidden {{ display: none; }}</style>
        <script>console.log("tracker");</script>
      </head>
      <body>
        <div class="preheader" style="display:none">Hidden inbox preview teaser that must disappear</div>
        <span style="font-size:0px;opacity:0">Zero font preheader</span>
        <div id="divRplyFwdMsg">
          <p>________________________________</p>
          <p><b>From:</b> Financial Times &lt;newsletters@ft.com&gt;<br/>
          <b>Sent:</b> Tuesday, November 17, 2026 7:00 AM<br/>
          <b>To:</b> Student &lt;student@university.edu&gt;<br/>
          <b>Subject:</b> Unhedged: Credit markets</p>
        </div>
        <p>{p1} <a href="https://www.ft.com/content/abc?utm_source=email&segmentId=99&ftcamp=xyz">Read FT</a></p>
        <p>{p2}</p>
        <p>{p3}</p>
        <div class="footer">
          <p>Follow the topics mentioned in this email</p>
          <p>Manage newsletters | Contact preferences</p>
          <p>Download the FT app for iOS and Android</p>
          <p><a href="https://ft.com/unsub">Unsubscribe</a></p>
          <p>Copyright The Financial Times Limited 2026. Bracken House, London.</p>
        </div>
      </body>
    </html>
    """
    cleaned = ft_imap.clean_ft_html(raw_html)
    assert "console.log" not in cleaned
    assert ".hidden" not in cleaned
    assert "Hidden inbox preview teaser" not in cleaned
    assert "Zero font preheader" not in cleaned
    assert "Sent: Tuesday" not in cleaned
    assert "To: Student" not in cleaned
    assert "Follow the topics" not in cleaned
    assert "Manage newsletters" not in cleaned
    assert "Contact preferences" not in cleaned
    assert "Download the FT app" not in cleaned
    assert "Unsubscribe" not in cleaned
    assert "Copyright The Financial Times" not in cleaned
    assert "Bracken House" not in cleaned
    assert "utm_source" not in cleaned
    assert "Private credit funds absorbed 42%" in cleaned
    assert "\n\n" in cleaned


# ------------------------------------------------------------------
# 3. Пропуск коротких писем (< 600 символов)
# ------------------------------------------------------------------

def test_skip_short_newsletter_under_600_chars(caplog):
    caplog.set_level(logging.INFO)
    short_html = "<html><body><p>Unhedged short promo announcement with a link only.</p></body></html>"
    raw = _build_eml(
        subject="Unhedged: Quick link",
        from_addr="newsletters@ft.com",
        html_body=short_html,
    )
    nl = ft_imap.parse_raw_email(raw)
    assert nl is None
    assert "skipped: too short" in caplog.text


# ------------------------------------------------------------------
# 4. Дедупликация через state/ft_seen.json + повторный IMAP-прогон
# ------------------------------------------------------------------

def test_ft_seen_deduplication_and_30d_pruning(tmp_path):
    seen_file = tmp_path / "ft_seen.json"
    now = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(days=31)).isoformat()
    fresh_ts = (now - timedelta(days=5)).isoformat()

    seen_file.write_text(
        json.dumps({"<old@ft.com>": old_ts, "<fresh@ft.com>": fresh_ts}),
        encoding="utf-8",
    )

    loaded = ft_imap.load_ft_seen(path=seen_file, keep_days=30, now=now)
    assert "<old@ft.com>" not in loaded
    assert "<fresh@ft.com>" in loaded

    nl = ft_imap.FTNewsletter(
        message_id="<msg-100@ft.com>",
        newsletter_name="Unhedged",
        subject="Unhedged: Liquidity wall",
        date=now.isoformat(),
        clean_text=_make_long_ft_body(),
    )
    assert not ft_imap.is_newsletter_seen(nl, loaded)
    ft_imap.mark_newsletter_seen(nl, loaded, now=now)
    ft_imap.save_ft_seen(loaded, path=seen_file)

    reloaded = ft_imap.load_ft_seen(path=seen_file, keep_days=30, now=now)
    assert ft_imap.is_newsletter_seen(nl, reloaded)

    # Повторное письмо с другим message_id, но той же нормализованной темой и датой
    nl_re_fwd = ft_imap.FTNewsletter(
        message_id="<msg-101-other@uni.edu>",
        newsletter_name="Unhedged",
        subject="FW: Unhedged: Liquidity wall",
        date=now.isoformat(),
        clean_text=_make_long_ft_body(),
    )
    assert ft_imap.is_newsletter_seen(nl_re_fwd, reloaded)


def test_imap_second_run_skips_already_sent_email(tmp_path, monkeypatch):
    seen_file = tmp_path / "ft_seen.json"
    history_file = tmp_path / "single_topic_history.json"
    ledger_file = tmp_path / "entity_ledger.json"
    now = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)

    raw_eml = _build_eml(
        subject="FW: Unhedged: Private credit stress",
        from_addr="student@uni.edu",
        message_id="<unhedged-42@ft.com>",
        date_dt=now - timedelta(hours=2),
        plain_body=f"Financial Times\n\n{_make_long_ft_body()}",
    )

    def make_imap_mock():
        imap = MagicMock()
        imap.search.return_value = ("OK", [b"1"])
        imap.fetch.return_value = ("OK", [(b"1 (RFC822)", raw_eml)])
        return imap

    monkeypatch.setenv("FT_FORWARDER_EMAIL", "student@uni.edu")

    stage1_resp = (
        "HEADLINE: Private credit absorbs middle-market buyout debt\n"
        "BODY:\n"
        "Private credit funds absorbed 42% of middle-market buyout volume in Q3, up sharply from 31% a year earlier.\n\n"
        "Traditional commercial banks stepped back from leveraged term loans under tighter capital reserve rules, "
        "leaving corporate borrowers with $950 billion in investment-grade maturities over the next 18 months. "
        "Direct lenders stepped into the gap by pricing floating-rate debt at wider spreads while insisting on stricter covenants.\n\n"
        "Across software issuers, interest coverage ratios dropped from 3.8x to 2.4x as base rates held steady. "
        "Thinner cash buffers leave private equity sponsors with much less room if organic revenue growth slows next year.\n"
        "FIGURES: 42% -> FT newsletter; 31% -> FT newsletter; $950 billion -> FT newsletter; 3.8x -> FT newsletter; 2.4x -> FT newsletter"
    )
    stage2_resp = (
        "Private credit funds absorbed 42% of middle-market buyout volume in Q3, up from 31% a year earlier.\n\n"
        "Commercial banks cut back on leveraged loans under tighter reserve rules, while corporate issuers still face "
        "$950 billion in investment-grade maturities over the next 18 months. Direct lenders filled that gap with "
        "floating-rate packages carrying wider spreads and tighter financial covenants.\n\n"
        "Software borrower interest coverage fell from 3.8x to 2.4x while base rates remained elevated. "
        "Thinner operating cash buffers leave buyout sponsors far less margin for error if organic revenue slows next year."
    )

    monkeypatch.setattr(brain, "_call", MagicMock(side_effect=[stage1_resp, stage2_resp, "[]", "[]"]))
    mock_send = MagicMock()
    monkeypatch.setattr("src.deliver.send", mock_send)

    # Первый запуск: отправляет 1 сообщение и сохраняет ft_seen.json
    msgs1 = ft_workflow.run_workflow(
        dry_run=False,
        seen_path=seen_file,
        history_path=history_file,
        ledger_path=ledger_file,
        now=now,
        imap_client=make_imap_mock(),
    )
    assert len(msgs1) == 1
    assert mock_send.call_count == 1
    assert seen_file.exists()

    # Второй запуск с тем же письмом в IMAP: должен пропустить
    msgs2 = ft_workflow.run_workflow(
        dry_run=False,
        seen_path=seen_file,
        history_path=history_file,
        ledger_path=ledger_file,
        now=now,
        imap_client=make_imap_mock(),
    )
    assert msgs2 == []
    assert mock_send.call_count == 1


# ------------------------------------------------------------------
# 5. Drafter -> Editor, fallback-плашка, приоритет и 7d анти-дубль
# ------------------------------------------------------------------

def test_drafter_editor_and_fallback_badge(tmp_path, monkeypatch):
    nl = ft_imap.FTNewsletter(
        message_id="<lex-1@ft.com>",
        newsletter_name="Lex",
        subject="Lex: CoreWeave debt service",
        date="2026-11-17T08:00:00+00:00",
        clean_text=(
            "CoreWeave posted $1.25 billion in Q3 revenue against $17.8 billion in debt obligations. "
            "Borrowing spreads sit at 13% while leasing costs rose 15%. " * 6
        ),
    )

    raw_body = (
        "CoreWeave posted $1.25 billion in Q3 revenue against $17.8 billion in debt obligations. "
        "Borrowing spreads sit at 13% while leasing costs rose 15% across new GPU clusters.\n\n"
        "Cash generation lags debt service by a wide margin. Refinancing terms tighten further if cloud utilization slips."
    )
    stage1_out = (
        "HEADLINE: CoreWeave debt load outpaces Q3 revenue\n"
        f"BODY:\n{raw_body}\n"
        "FIGURES: $1.25 billion -> FT; $17.8 billion -> FT; 13% -> FT; 15% -> FT"
    )

    # Успешный проход Editor
    monkeypatch.setattr(brain, "_call", lambda *args, **kwargs: stage1_out)
    monkeypatch.setattr(brain, "edit_and_humanize_draft", lambda body, draft_type="single": body)
    monkeypatch.setattr(brain, "inspect_and_collect_flagged_phrases", lambda *args, **kwargs: None)

    res_ok = ft_workflow.process_newsletter(
        nl,
        history_path=tmp_path / "hist.json",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
    )
    assert not res_ok["editor_failed"]
    assert "<b>FT PREMIUM · Lex</b>" in res_ok["message"]
    assert "[RAW DRAFT: EDITOR FAILED]" not in res_ok["message"]

    # Сбой Editor через FALLBACK_BADGE
    monkeypatch.setattr(
        brain,
        "edit_and_humanize_draft",
        lambda body, draft_type="single": f"{brain.FALLBACK_BADGE}\n\n{body}",
    )
    res_fb = ft_workflow.process_newsletter(
        nl,
        history_path=tmp_path / "hist.json",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
    )
    assert res_fb["editor_failed"]
    assert "<b>FT PREMIUM · Lex</b> ⚠️ [RAW DRAFT: EDITOR FAILED]" in res_fb["message"]

    # Сбой Editor через исключение
    def _boom(*args, **kwargs):
        raise RuntimeError("Editor timeout")

    monkeypatch.setattr(brain, "edit_and_humanize_draft", _boom)
    res_exc = ft_workflow.process_newsletter(
        nl,
        history_path=tmp_path / "hist.json",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
    )
    assert res_exc["editor_failed"]
    assert "⚠️ [RAW DRAFT: EDITOR FAILED]" in res_exc["message"]


def test_newsletter_prioritization_and_7d_anti_duplicate(tmp_path, monkeypatch):
    newsletters = [
        ft_imap.FTNewsletter("<1>", "Energy Source", "Energy Source: Oil", "2026-11-17", _make_long_ft_body()),
        ft_imap.FTNewsletter("<2>", "Due Diligence", "Due Diligence: M&A", "2026-11-17", _make_long_ft_body()),
        ft_imap.FTNewsletter("<3>", "Europe Express", "Europe Express: EU", "2026-11-17", _make_long_ft_body()),
        ft_imap.FTNewsletter("<4>", "Unhedged", "Unhedged: Macro", "2026-11-17", _make_long_ft_body()),
    ]
    picked = ft_workflow.prioritize_newsletters(newsletters, max_drafts=2)
    assert [n.newsletter_name for n in picked] == ["Unhedged", "Due Diligence"]

    # Проверка 7-дневного анти-дубля компаний из state/single_topic_history.json
    hist_path = tmp_path / "single_topic_history.json"
    now = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
    brain.save_single_topic_history(
        "CoreWeave debt reached $17.8 billion against $1.25 billion revenue.",
        entities=["CoreWeave"],
        headline="CoreWeave debt",
        now=now - timedelta(days=2),
        path=hist_path,
        source_pipeline="hugs",
    )

    nl_cw = ft_imap.FTNewsletter(
        message_id="<cw-1@ft.com>",
        newsletter_name="Unhedged",
        subject="Unhedged: CoreWeave and Nvidia capex",
        date=now.isoformat(),
        clean_text="CoreWeave and Nvidia signed a $12 billion compute lease with 15% spreads. " * 10,
    )
    captured_prompts: list[str] = []

    def fake_call(prompt, **kwargs):
        captured_prompts.append(prompt)
        return (
            "HEADLINE: Nvidia supplier financing shifts\n"
            "BODY:\nNvidia tightened supplier financing terms across $12 billion in leases at 15% spreads.\n"
            "FIGURES: $12 billion -> FT; 15% -> FT"
        )

    monkeypatch.setattr(brain, "_call", fake_call)
    monkeypatch.setattr(brain, "edit_and_humanize_draft", lambda b, draft_type="single": b)

    # Тестируем передачу 7d anti-duplicate списка в Stage 1 и сохранение новой сущности в single_topic_history
    mock_imap = MagicMock()
    raw_cw = _build_eml(
        subject="Unhedged: CoreWeave and Nvidia capex",
        from_addr="newsletters@ft.com",
        message_id=nl_cw.message_id,
        date_dt=now,
        plain_body=nl_cw.clean_text,
    )
    mock_imap.search.return_value = ("OK", [b"1"])
    mock_imap.fetch.return_value = ("OK", [(b"1 (RFC822)", raw_cw)])
    monkeypatch.setattr("src.deliver.send", lambda msg: None)

    ft_workflow.run_workflow(
        dry_run=False,
        seen_path=tmp_path / "ft_seen.json",
        history_path=hist_path,
        ledger_path=tmp_path / "ledger.json",
        now=now,
        imap_client=mock_imap,
    )
    assert "7-DAY ANTI-DUPLICATE BAN" in captured_prompts[0]
    assert "CoreWeave" in captured_prompts[0]
    saved_entities = brain.get_recent_single_topic_entities(days=7, now=now, path=hist_path)
    assert "Nvidia" in saved_entities


# ------------------------------------------------------------------
# 6. Интеграция с verify.verify_drafts и score_draft.lint_style_ticks
# ------------------------------------------------------------------

def test_verify_honest_denominator_cross_day_conflict_and_style_flags(tmp_path, monkeypatch):
    ledger_path = tmp_path / "entity_ledger.json"
    now = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)

    # Вчерашний пост зафиксировал CoreWeave debt = $15 billion
    ledger_path.write_text(
        json.dumps([
            {
                "entity": "CoreWeave",
                "metric": "debt",
                "period": "",
                "value": "$15 billion",
                "numeric_value": 15_000_000_000.0,
                "date": "2026-11-16",
            }
        ]),
        encoding="utf-8",
    )

    nl = ft_imap.FTNewsletter(
        message_id="<ft-ver-1@ft.com>",
        newsletter_name="Unhedged",
        subject="Unhedged: CoreWeave debt repricing",
        date=now.isoformat(),
        clean_text=(
            "CoreWeave debt reached $17.8 billion while spreads sit at 13% across data center leases. "
            "In 2026, refinancing demand accelerated across US private credit lenders as base yields held near 4.8%. "
            + _make_long_ft_body()
        ),
    )

    # Черновик содержит:
    # - верные цифры $17.8 billion и 13% (2/2 ✅)
    # - конфликт по CoreWeave debt ($17.8 billion != $15 billion)
    # - маркеры стиля для lint_style_ticks ("structural", тире "—")
    bad_style_body = (
        "CoreWeave debt hit $17.8 billion while borrowing spreads sit at 13% — creating a structural squeeze. "
        "Private credit lenders now absorb these refinancing risks as base yields stay elevated."
    )
    stage1_out = (
        "HEADLINE: CoreWeave debt squeeze\n"
        f"BODY:\n{bad_style_body}\n"
        "FIGURES: $17.8 billion -> FT; 13% -> FT"
    )

    monkeypatch.setattr(brain, "_call", lambda *args, **kwargs: stage1_out)
    monkeypatch.setattr(brain, "edit_and_humanize_draft", lambda b, draft_type="single": b)
    monkeypatch.setattr(brain, "inspect_and_collect_flagged_phrases", lambda *args, **kwargs: None)

    res = ft_workflow.process_newsletter(
        nl,
        history_path=tmp_path / "hist.json",
        ledger_path=ledger_path,
        now=now,
        dry_run=False,
    )

    msg = res["message"]
    assert "───────────────" in msg
    assert "POST · цифры 2/2 ✅" in msg
    assert "CROSS-DAY CONFLICT: CoreWeave debt differs from 2026-11-16 ($15 billion)" in msg
    assert "⚠️ STYLE FLAGS:" in msg
    assert "structural" in msg
    assert "dash/range formatting" in msg


# ------------------------------------------------------------------
# 7. Режим --from-file + --dry-run (без Telegram и без записи ft_seen.json)
# ------------------------------------------------------------------

def test_cli_from_file_dry_run(tmp_path, monkeypatch, capsys):
    eml_path = tmp_path / "sample_unhedged.eml"
    eml_bytes = _build_eml(
        subject="FW: Unhedged: Treasury buybacks and liquidity",
        from_addr="student@uni.edu",
        message_id="<cli-test-1@ft.com>",
        html_body=(
            "<html><body>"
            f"<p>Financial Times ft.com</p><p>{_make_long_ft_body().replace(chr(10)+chr(10), '</p><p>')}</p>"
            "<p>Unsubscribe | Copyright The Financial Times Limited 2026</p>"
            "</body></html>"
        ),
    )
    eml_path.write_bytes(eml_bytes)

    monkeypatch.setenv("FT_FORWARDER_EMAIL", "student@uni.edu")
    fake_seen = tmp_path / "ft_seen.json"
    monkeypatch.setattr(ft_imap, "FT_SEEN_PATH", fake_seen)

    stage1_out = (
        "HEADLINE: Treasury maturities meet private credit supply\n"
        "BODY:\n"
        "Corporate credit spreads widened 25 bps as investment-grade borrowers face $950 billion in maturities over 18 months.\n\n"
        "Private credit funds absorbed 42% of middle-market buyout volume in Q3, up from 31% a year earlier. "
        "Commercial banks pulled back from leveraged term loans under capital charges.\n\n"
        "Interest coverage across software issuers dropped from 3.8x to 2.4x. Lower revenue growth leaves thin buffers.\n"
        "FIGURES: 25 bps -> FT; $950 billion -> FT; 42% -> FT; 31% -> FT; 3.8x -> FT; 2.4x -> FT"
    )
    edited_out = (
        "Corporate credit spreads widened 25 bps as investment-grade borrowers face $950 billion in maturities over 18 months.\n\n"
        "Private credit funds took 42% of middle-market buyout volume in Q3, up from 31% a year ago. "
        "Commercial banks cut leveraged loan exposure under higher reserve charges.\n\n"
        "Software interest coverage fell from 3.8x to 2.4x. Slower revenue growth leaves thin cash buffers."
    )

    monkeypatch.setattr(brain, "_call", MagicMock(return_value=stage1_out))
    monkeypatch.setattr(brain, "edit_and_humanize_draft", MagicMock(return_value=edited_out))

    mock_send = MagicMock()
    monkeypatch.setattr("src.deliver.send", mock_send)

    rc = ft_workflow.main(["--from-file", str(eml_path), "--dry-run"])
    assert rc == 0

    captured = capsys.readouterr()
    assert "<b>FT PREMIUM · Unhedged</b>" in captured.out
    assert "<i>Unhedged: Treasury buybacks and liquidity</i>" in captured.out
    assert "POST · цифры" in captured.out
    assert "✅" in captured.out
    assert not mock_send.called
    assert not fake_seen.exists()


def test_live_imap_blocked_without_newsbot_allow_live(monkeypatch):
    monkeypatch.delenv("NEWSBOT_ALLOW_LIVE", raising=False)
    monkeypatch.setenv("FT_IMAP_USER", "user@gmail.com")
    monkeypatch.setenv("FT_IMAP_PASSWORD", "app-pass")
    with pytest.raises(RuntimeError, match="NEWSBOT_ALLOW_LIVE"):
        ft_imap.fetch_ft_newsletters()


def test_from_file_html_and_txt_and_verify_maybe_downgrade(tmp_path, monkeypatch):
    html_file = tmp_path / "europe_express.html"
    html_file.write_text(
        "<html><head><title>Europe Express: Sovereign bond supply</title></head>"
        f"<body><p>{_make_long_ft_body().replace(chr(10)+chr(10), '</p><p>')}</p></body></html>",
        encoding="utf-8",
    )
    txt_file = tmp_path / "energy_source.txt"
    txt_file.write_text(f"Subject: Energy Source: LNG spreads\n\n{_make_long_ft_body()}", encoding="utf-8")

    html_items = ft_imap.load_newsletter_from_file(html_file)
    txt_items = ft_imap.load_newsletter_from_file(txt_file)
    assert len(html_items) == 1 and html_items[0].newsletter_name == "Europe Express"
    assert len(txt_items) == 1 and txt_items[0].newsletter_name == "Energy Source"

    # Несовпадающее число (99%) должно понизить POST -> MAYBE с честным знаменателем
    stage1_out = (
        "HEADLINE: Sovereign bond supply tests spreads\n"
        "BODY:\n"
        "Private credit funds absorbed 42% of buyout volume while unverified share reached 99%.\n"
        "FIGURES: 42% -> FT; 99% -> FT"
    )
    monkeypatch.setattr(brain, "_call", MagicMock(return_value=stage1_out))
    monkeypatch.setattr(brain, "edit_and_humanize_draft", lambda b, draft_type="single": b)
    monkeypatch.setattr(brain, "inspect_and_collect_flagged_phrases", lambda *args, **kwargs: None)

    res = ft_workflow.process_newsletter(
        html_items[0],
        history_path=tmp_path / "hist.json",
        ledger_path=tmp_path / "ledger.json",
        dry_run=True,
    )
    assert "MAYBE · цифры 1/2 — сверь 99%" in res["message"]


def test_normalize_ft_units_for_verify_british_per_cent_and_bn_mn(tmp_path):
    raw = (
        "Share buybacks jumped 70 per cent while inflation held at 2.6 Per Cent. "
        "Total debt hit $104bn (or 104bn locally), with cash flow of $53mn and fees of $53m."
    )
    normalized = ft_workflow.normalize_ft_units_for_verify(raw)
    assert "70%" in normalized
    assert "2.6%" in normalized
    assert "$104 billion" in normalized
    assert "104 billion" in normalized
    assert "$53 million" in normalized
    assert normalized.count("$53 million") == 2

    nl = ft_imap.FTNewsletter(
        message_id="<ft-uk-units@ft.com>",
        newsletter_name="Lex",
        subject="Lex: UK capital distribution",
        date="2026-11-18T08:00:00+00:00",
        clean_text=(
            "European utilities raised capital spending by 70 per cent as regulated inflation reached 2.6 per cent. "
            "Combined net debt climbed to $104bn alongside $53mn in quarterly restructuring fees and $12m in advisory charges. "
            * 4
        ),
    )
    draft_body = (
        "European utilities increased capital spending by 70% while inflation ran at 2.6%. "
        "Net debt reached $104 billion against $53 million in restructuring fees and $12 million in advisory charges."
    )
    footer = ft_workflow.verify_ft_draft(
        draft_body,
        newsletter=nl,
        ledger_path=tmp_path / "entity_ledger.json",
        dry_run=True,
    )
    assert footer == "POST · цифры 5/5 ✅"


