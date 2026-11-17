"""Забор, фильтрация и очистка премиум-рассылок Financial Times по IMAP.

Поддерживает как прямые письма от *@ft.com, так и автопересылку из Outlook
(включая префиксы FW:/Fwd:/Re:/ПЕРЕСЛ: и шапки пересылки).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import email
from email import policy
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
import hashlib
import html
import imaplib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import bs4

log = logging.getLogger(__name__)

# Конфигурация по умолчанию из env
FT_IMAP_HOST = os.getenv("FT_IMAP_HOST") or "imap.gmail.com"
FT_IMAP_PORT = int(os.getenv("FT_IMAP_PORT") or 993)
FT_IMAP_USER = os.getenv("FT_IMAP_USER") or ""
FT_IMAP_PASSWORD = os.getenv("FT_IMAP_PASSWORD") or ""
FT_IMAP_FOLDER = os.getenv("FT_IMAP_FOLDER") or "INBOX"
FT_FORWARDER_EMAIL = os.getenv("FT_FORWARDER_EMAIL") or ""
FT_LOOKBACK_HOURS = int(os.getenv("FT_LOOKBACK_HOURS") or 36)

MIN_CLEAN_TEXT_CHARS = 600
FT_SEEN_KEEP_DAYS = 30
FT_SEEN_PATH = Path(__file__).resolve().parent.parent / "state" / "ft_seen.json"

_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# Белый список поддерживаемых рассылок FT (каноническое имя -> regex)
WHITELIST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Unhedged", re.compile(r"\bunhedged\b", re.IGNORECASE)),
    ("Due Diligence", re.compile(r"\bdue\s+diligence\b", re.IGNORECASE)),
    ("Europe Express", re.compile(r"\beurope\s+express\b", re.IGNORECASE)),
    ("Energy Source", re.compile(r"\benergy\s+source\b", re.IGNORECASE)),
    ("Lex", re.compile(r"\b(?:the\s+lex\s+newsletter|lex)\b", re.IGNORECASE)),
    (
        "Central Banks",
        re.compile(r"\b(?:chris\s+giles\s+on\s+central\s+banks|central\s+banks)\b", re.IGNORECASE),
    ),
)

_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:\[?\s*(?:fw|fwd|re|пересл|напр)(?:\[\d+\])?\s*\]?\s*:\s*)+",
    re.IGNORECASE,
)

_HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|"
    r"font-size\s*:\s*0(?:px|pt|em|rem)?(?:\s*;|\s*$)|"
    r"max-height\s*:\s*0(?:px|pt|em|rem)?(?:\s*;|\s*$)|"
    r"opacity\s*:\s*0(?:\.0+)?(?:\s*;|\s*$))",
    re.IGNORECASE,
)

_PREHEADER_ATTR_RE = re.compile(r"preheader", re.IGNORECASE)

_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = {
    "segmentid", "ftcamp", "sharetype", "emailid", "referralcode",
    "mc_cid", "mc_eid", "fbclid", "gclid", "mkt_tok",
}
_URL_RE = re.compile(r"https?://[^\s<>\"')]+")

# Шапка пересылки Outlook / Gmail / Apple Mail (в начале или между абзацами)
_OUTLOOK_FORWARD_HEADER_RE = re.compile(
    r"(?:^|\n)"
    r"(?:[-_=]{4,}\s*(?:Original Message|Forwarded [Mm]essage|Пересылаемое сообщение)[^\n]*\n+)?"
    r"(?:Begin forwarded message:\s*\n+)?"
    r"(?:From|От)\s*:[^\n]+\n+"
    r"(?:(?:Sent|Date|Отправлено|Дата|To|Кому|Cc|Копия|Reply-To)\s*:[^\n]+\n+){1,5}"
    r"(?:Subject|Тема)\s*:[^\n]+"
    r"(?:\n+(?:To|Кому|Cc|Копия)\s*:[^\n]+){0,2}",
    re.IGNORECASE,
)

_OUTLOOK_INLINE_FORWARD_RE = re.compile(
    r"(?:^|\n)\s*(?:From|От)\s*:\s*[^\n]+?\s+"
    r"(?:Sent|Date|Отправлено|Дата)\s*:\s*[^\n]+?\s+"
    r"(?:To|Кому)\s*:\s*[^\n]+?\s+"
    r"(?:Subject|Тема)\s*:\s*[^\n]+",
    re.IGNORECASE,
)

_FORWARD_SEPARATOR_LINE_RE = re.compile(
    r"^\s*(?:[-_=]{5,}(?:\s*(?:Original Message|Forwarded [Mm]essage|Пересылаемое сообщение)\s*[-_=]*)?|"
    r"Begin forwarded message:?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Служебные футеры FT
_FT_FOOTER_MARKERS_RE = re.compile(
    r"(?:\bUnsubscribe\b|"
    r"\bManage\s+(?:your\s+)?newsletters\b|"
    r"\bFollow\s+the\s+topics\b|"
    r"\bDownload\s+the\s+FT\s+app\b|"
    r"(?:©\s*(?:Copyright\s+)?(?:The\s+)?Financial\s+Times|Copyright\s+(?:©\s*)?The\s+Financial\s+Times|The\s+Financial\s+Times\s+Limited)\b|"
    r"\bContact\s+preferences\b|"
    r"\bView\s+(?:this\s+email\s+)?in\s+(?:your\s+)?browser\b)",
    re.IGNORECASE,
)

_FT_TERMINAL_FOOTER_RE = re.compile(
    r"(?:\bUnsubscribe\b|"
    r"\bManage\s+(?:your\s+)?newsletters\b|"
    r"\bFollow\s+the\s+topics\b|"
    r"\bDownload\s+the\s+FT\s+app\b|"
    r"(?:©\s*(?:Copyright\s+)?(?:The\s+)?Financial\s+Times|Copyright\s+(?:©\s*)?The\s+Financial\s+Times|The\s+Financial\s+Times\s+Limited)\b|"
    r"\bContact\s+preferences\b)",
    re.IGNORECASE,
)


@dataclass
class FTNewsletter:
    message_id: str
    newsletter_name: str
    subject: str
    date: str
    clean_text: str


def _env_str(name: str, default: str) -> str:
    val = os.getenv(name)
    return val if val is not None and val != "" else default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _decode_mime_header(value: str | None) -> str:
    """Декодирует MIME-заголовок (RFC 2047) в чистую UTF-8 строку."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def normalize_subject(subject: str) -> str:
    """Срезает префиксы FW:/Fwd:/Re:/ПЕРЕСЛ: в любом регистре и лишние пробелы."""
    decoded = _decode_mime_header(subject)
    text = re.sub(r"\s+", " ", decoded).strip()
    while True:
        updated = _SUBJECT_PREFIX_RE.sub("", text).strip()
        if updated == text:
            break
        text = updated
    return re.sub(r"\s+", " ", text).strip()


def identify_newsletter(subject: str, metadata_text: str = "") -> str | None:
    """Определяет рассылку по нормализованной теме и метаданным из белого списка."""
    norm_subj = normalize_subject(subject)
    for canonical, pat in WHITELIST_PATTERNS:
        if pat.search(norm_subj):
            return canonical

    if metadata_text:
        norm_meta = normalize_subject(metadata_text)
        for canonical, pat in WHITELIST_PATTERNS:
            if pat.search(norm_meta):
                return canonical

    return None


def is_accepted_sender(
    from_header: str,
    raw_content: str = "",
    forwarder_email: str | None = None,
) -> bool:
    """Проверяет отправителя: @ft.com или FT_FORWARDER_EMAIL с упоминанием FT в письме."""
    decoded_from = _decode_mime_header(from_header).strip()
    from_low = decoded_from.lower()
    if "ft.com" in from_low:
        return True

    fwd = (
        forwarder_email
        if forwarder_email is not None
        else _env_str("FT_FORWARDER_EMAIL", FT_FORWARDER_EMAIL)
    ).strip().lower()
    if not fwd:
        return False

    _, addr = parseaddr(decoded_from)
    addr_low = addr.lower()
    sender_matches = (addr_low == fwd) if addr_low else (fwd in from_low)
    if not sender_matches:
        return False

    content_low = (raw_content or "").lower()
    return ("ft.com" in content_low) or ("financial times" in content_low)


def _strip_tracking_from_url(url: str) -> str:
    """Удаляет трекинг-параметры из URL."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept: list[str] = []
    for seg in parts.query.split("&"):
        key = seg.split("=", 1)[0].strip().lower()
        if key in _TRACKING_PARAM_NAMES or any(key.startswith(p) for p in _TRACKING_PARAM_PREFIXES):
            continue
        kept.append(seg)
    if len(kept) == len(parts.query.split("&")):
        return url
    return urlunparse(parts._replace(query="&".join(kept)))


def _strip_tracking_params(text: str) -> str:
    return _URL_RE.sub(lambda m: _strip_tracking_from_url(m.group(0)), text)


def _is_hidden_preheader_tag(tag: bs4.Tag) -> bool:
    if getattr(tag, "decomposed", False) or getattr(tag, "attrs", None) is None:
        return False
    if tag.has_attr("hidden"):
        return True
    if str(tag.get("aria-hidden", "")).strip().lower() == "true":
        return True
    tag_id = str(tag.get("id", "") or "")
    classes = tag.get("class") or []
    class_str = " ".join(classes) if isinstance(classes, list) else str(classes)
    if _PREHEADER_ATTR_RE.search(tag_id) or _PREHEADER_ATTR_RE.search(class_str):
        return True
    style = str(tag.get("style", "") or "")
    if style and _HIDDEN_STYLE_RE.search(style):
        return True
    return False


def clean_ft_html(raw_html: str) -> str:
    """Извлекает читаемый текст по абзацам из HTML-письма FT."""
    if not raw_html or not raw_html.strip():
        return ""

    soup = bs4.BeautifulSoup(raw_html, "html.parser")

    for tag_name in ("style", "script", "head", "noscript", "svg", "meta", "link"):
        for t in soup.find_all(tag_name):
            t.decompose()

    for tag in list(soup.find_all(True)):
        if _is_hidden_preheader_tag(tag):
            tag.decompose()

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for cell in soup.find_all(["td", "th"]):
        cell.append(" ")

    for block in soup.find_all(
        ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "tr", "div", "section", "article"]
    ):
        block.append("\n\n")

    text = html.unescape(soup.get_text())
    return clean_email_text(text)


def clean_email_text(raw_text: str) -> str:
    """Удаляет Outlook-шапки пересылки, трекинг-параметры и служебные футеры FT."""
    if not raw_text:
        return ""

    text = html.unescape(raw_text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = _strip_tracking_params(text)

    # Нормализуем пробелы на строках до поиска шапок и футеров
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(lines)

    # Вырезаем шапки пересылки Outlook / Gmail
    text = _OUTLOOK_FORWARD_HEADER_RE.sub("\n", text)
    text = _OUTLOOK_INLINE_FORWARD_RE.sub("\n", text)
    text = _FORWARD_SEPARATOR_LINE_RE.sub("", text)

    # Если в нижней части письма начинается служебный футер FT, обрезаем хвост
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if text:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        cleaned_paragraphs: list[str] = []
        for idx, para in enumerate(paragraphs):
            # Проверяем построчно внутри абзаца (на случай слипшегося футера)
            kept_lines: list[str] = []
            hit_terminal_footer = False
            for ln in para.splitlines():
                s = ln.strip()
                if not s:
                    continue
                if _FT_FOOTER_MARKERS_RE.search(s):
                    # Если маркер появился во второй половине письма или как короткая служебная строка,
                    # остаток нижнего футера дальше не нужен
                    if idx >= max(1, len(paragraphs) // 2) and _FT_TERMINAL_FOOTER_RE.search(s):
                        hit_terminal_footer = True
                        break
                    continue
                kept_lines.append(s)

            if kept_lines:
                cleaned_paragraphs.append("\n".join(kept_lines))
            if hit_terminal_footer:
                break

        text = "\n\n".join(cleaned_paragraphs)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _extract_email_bodies(msg: email.message.Message) -> tuple[str, str]:
    """Возвращает (html_body, plain_body) из MIME-сообщения."""
    html_parts: list[str] = []
    plain_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition", "") or "").lower()
            if "attachment" in disp:
                continue
            ctype = (part.get_content_type() or "").lower()
            try:
                payload = part.get_content()
            except Exception:
                raw_bytes = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                payload = raw_bytes.decode(charset, errors="replace")
            if not isinstance(payload, str):
                payload = str(payload)
            if ctype == "text/html":
                html_parts.append(payload)
            elif ctype == "text/plain":
                plain_parts.append(payload)
    else:
        ctype = (msg.get_content_type() or "").lower()
        try:
            payload = msg.get_content()
        except Exception:
            raw_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = raw_bytes.decode(charset, errors="replace")
        if not isinstance(payload, str):
            payload = str(payload)
        if ctype == "text/html":
            html_parts.append(payload)
        else:
            plain_parts.append(payload)

    return "\n".join(html_parts), "\n".join(plain_parts)


def _parse_email_date(date_header: str) -> datetime | None:
    if not date_header or not date_header.strip():
        return None
    try:
        dt = parsedate_to_datetime(date_header.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _date_key_for_hash(date_str: str) -> str:
    dt = _parse_email_date(date_str)
    if dt is not None:
        return dt.strftime("%Y-%m-%d")
    m = re.search(r"\d{4}-\d{2}-\d{2}", date_str or "")
    return m.group(0) if m else (date_str or "").strip()[:16]


def compute_subject_date_hash(subject: str, date_str: str) -> str:
    """Хэш нормализованной темы и даты письма для дедупликации."""
    norm = normalize_subject(subject).lower()
    date_key = _date_key_for_hash(date_str)
    digest = hashlib.sha256(f"{norm}|{date_key}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def load_ft_seen(
    path: Path | None = None,
    keep_days: int = FT_SEEN_KEEP_DAYS,
    now: datetime | None = None,
) -> dict[str, str]:
    """Читает state/ft_seen.json и очищает записи старше keep_days (по умолчанию 30 дней)."""
    target = path or FT_SEEN_PATH
    if not target.exists():
        return {}
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        log.warning("Ошибка чтения %s: %s", target, exc)
        return {}
    if not isinstance(data, dict):
        return {}

    ref_now = now or datetime.now(timezone.utc)
    cutoff = ref_now - timedelta(days=keep_days)
    kept: dict[str, str] = {}
    for key, ts_raw in data.items():
        if not isinstance(ts_raw, str):
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                kept[str(key)] = ts_raw
        except ValueError:
            continue
    return kept


def save_ft_seen(seen: dict[str, str], path: Path | None = None) -> None:
    """Сохраняет state/ft_seen.json."""
    target = path or FT_SEEN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def is_newsletter_seen(newsletter: FTNewsletter, seen: dict[str, str]) -> bool:
    """Проверяет, было ли письмо уже обработано (по message_id или хэшу темы+даты)."""
    if newsletter.message_id and newsletter.message_id in seen:
        return True
    sd_hash = compute_subject_date_hash(newsletter.subject, newsletter.date)
    return sd_hash in seen


def mark_newsletter_seen(
    newsletter: FTNewsletter,
    seen: dict[str, str],
    now: datetime | None = None,
) -> None:
    """Отмечает письмо в словаре seen (message_id + хэш нормализованной темы и даты)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    if newsletter.message_id:
        seen[newsletter.message_id] = ts
    sd_hash = compute_subject_date_hash(newsletter.subject, newsletter.date)
    seen[sd_hash] = ts


def parse_raw_email(
    raw_email: bytes | str,
    forwarder_email: str | None = None,
    lookback_hours: int | None = None,
    now: datetime | None = None,
    min_chars: int = MIN_CLEAN_TEXT_CHARS,
) -> FTNewsletter | None:
    """Парсит сырое MIME-письмо, фильтрует по отправителю, белому списку и длине."""
    if isinstance(raw_email, bytes):
        msg = email.message_from_bytes(raw_email, policy=policy.default)
    else:
        msg = email.message_from_string(raw_email, policy=policy.default)

    raw_subject = _decode_mime_header(msg.get("Subject", ""))
    norm_subject = normalize_subject(raw_subject)
    from_header = _decode_mime_header(msg.get("From", ""))
    date_header = _decode_mime_header(msg.get("Date", ""))
    message_id = _decode_mime_header(msg.get("Message-ID", "")).strip()

    dt = _parse_email_date(date_header)
    ref_now = now or datetime.now(timezone.utc)
    if lookback_hours is not None and dt is not None:
        if dt < ref_now - timedelta(hours=lookback_hours):
            log.info("Письмо '%s' старше окна %d ч — пропущено", norm_subject[:80], lookback_hours)
            return None

    html_body, plain_body = _extract_email_bodies(msg)
    header_meta = "\n".join(
        _decode_mime_header(msg.get(h, ""))
        for h in ("From", "Reply-To", "Return-Path", "List-Id", "List-Unsubscribe", "Subject")
        if msg.get(h)
    )
    raw_for_sender = "\n".join(part for part in (header_meta, html_body, plain_body) if part)

    if not is_accepted_sender(from_header, raw_content=raw_for_sender, forwarder_email=forwarder_email):
        log.info("Игнорируем письмо от постороннего отправителя: %s (%s)", from_header, norm_subject[:60])
        return None

    metadata_text = "\n".join(
        part for part in (from_header, _decode_mime_header(msg.get("List-Id", "")), plain_body[:800]) if part
    )
    newsletter_name = identify_newsletter(norm_subject, metadata_text=metadata_text)
    if not newsletter_name:
        log.info("Письмо не входит в белый список FT рассылок: '%s'", norm_subject[:80])
        return None

    if html_body.strip():
        clean_text = clean_ft_html(html_body)
        if not clean_text and plain_body.strip():
            clean_text = clean_email_text(plain_body)
    else:
        clean_text = clean_email_text(plain_body)

    if len(clean_text) < min_chars:
        log.info("Рассылка '%s' (%s) — skipped: too short (%d < %d симв.)",
                 newsletter_name, norm_subject[:60], len(clean_text), min_chars)
        return None

    date_iso = dt.isoformat() if dt is not None else date_header.strip()
    if not message_id:
        message_id = f"<{compute_subject_date_hash(norm_subject, date_iso)}@ft-local>"

    return FTNewsletter(
        message_id=message_id,
        newsletter_name=newsletter_name,
        subject=norm_subject,
        date=date_iso,
        clean_text=clean_text,
    )


def load_newsletter_from_file(
    file_path: str | Path,
    forwarder_email: str | None = None,
    min_chars: int = MIN_CLEAN_TEXT_CHARS,
) -> list[FTNewsletter]:
    """Загружает письмо из локального .eml, .html или .txt файла (--from-file)."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")

    suffix = p.suffix.lower()
    if suffix == ".eml":
        raw_bytes = p.read_bytes()
        nl = parse_raw_email(
            raw_bytes,
            forwarder_email=forwarder_email,
            lookback_hours=None,
            min_chars=min_chars,
        )
        return [nl] if nl is not None else []

    raw_text = p.read_text(encoding="utf-8", errors="replace")
    subject_hint = p.stem.replace("_", " ").replace("-", " ")

    if suffix in (".html", ".htm"):
        soup = bs4.BeautifulSoup(raw_text, "html.parser")
        if soup.title and soup.title.string:
            subject_hint = normalize_subject(soup.title.string)
        clean_text = clean_ft_html(raw_text)
    else:
        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        if lines and lines[0].lower().startswith("subject:"):
            subject_hint = normalize_subject(lines[0].split(":", 1)[1])
        clean_text = clean_email_text(raw_text)

    if len(clean_text) < min_chars:
        log.info("Локальный файл %s — skipped: too short (%d < %d симв.)", p, len(clean_text), min_chars)
        return []

    newsletter_name = identify_newsletter(subject_hint, metadata_text=f"{p.stem}\n{clean_text[:1500]}")
    if not newsletter_name:
        newsletter_name = "FT Premium"

    date_iso = datetime.now(timezone.utc).isoformat()
    msg_id = f"<file-{hashlib.sha256(str(p.resolve()).encode('utf-8')).hexdigest()[:16]}@ft-local>"
    return [
        FTNewsletter(
            message_id=msg_id,
            newsletter_name=newsletter_name,
            subject=normalize_subject(subject_hint) or p.stem,
            date=date_iso,
            clean_text=clean_text,
        )
    ]


def _format_imap_since(dt: datetime) -> str:
    return f"{dt.day:02d}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year:04d}"


def fetch_ft_newsletters(
    host: str | None = None,
    port: int | None = None,
    user: str | None = None,
    password: str | None = None,
    folder: str | None = None,
    forwarder_email: str | None = None,
    lookback_hours: int | None = None,
    seen_path: Path | None = None,
    now: datetime | None = None,
    imap_client: Any = None,
) -> list[FTNewsletter]:
    """Подключается к IMAP (или использует переданный imap_client) и возвращает новые FTNewsletter."""
    if imap_client is None and os.environ.get("NEWSBOT_ALLOW_LIVE") != "1":
        if getattr(imaplib.IMAP4_SSL, "__module__", "") == "imaplib":
            raise RuntimeError(
                "живой вызов IMAP заблокирован: переменная NEWSBOT_ALLOW_LIVE не выставлена в 1."
            )

    eff_host = host or _env_str("FT_IMAP_HOST", FT_IMAP_HOST)
    eff_port = port if port is not None else _env_int("FT_IMAP_PORT", FT_IMAP_PORT)
    eff_user = user if user is not None else _env_str("FT_IMAP_USER", FT_IMAP_USER)
    eff_password = password if password is not None else _env_str("FT_IMAP_PASSWORD", FT_IMAP_PASSWORD)
    eff_folder = folder or _env_str("FT_IMAP_FOLDER", FT_IMAP_FOLDER)
    eff_lookback = lookback_hours if lookback_hours is not None else _env_int("FT_LOOKBACK_HOURS", FT_LOOKBACK_HOURS)
    eff_fwd = forwarder_email if forwarder_email is not None else _env_str("FT_FORWARDER_EMAIL", FT_FORWARDER_EMAIL)

    if imap_client is None and (not eff_user or not eff_password):
        raise ValueError("FT_IMAP_USER и FT_IMAP_PASSWORD обязательны для подключения к IMAP.")

    ref_now = now or datetime.now(timezone.utc)
    since_dt = ref_now - timedelta(hours=eff_lookback) - timedelta(days=1)
    since_str = _format_imap_since(since_dt)

    seen = load_ft_seen(path=seen_path, keep_days=FT_SEEN_KEEP_DAYS, now=ref_now)
    batch_hashes: set[str] = set()
    batch_ids: set[str] = set()
    newsletters: list[FTNewsletter] = []

    owns_conn = imap_client is None
    conn = imap_client if imap_client is not None else imaplib.IMAP4_SSL(eff_host, eff_port)

    try:
        if owns_conn:
            conn.login(eff_user, eff_password)
        conn.select(eff_folder, readonly=True)
        typ, data = conn.search(None, "SINCE", since_str)
        if typ != "OK" or not data or not data[0]:
            log.info("IMAP search SINCE %s: писем не найдено", since_str)
            return []

        msg_nums = data[0].split()
        log.info("IMAP search SINCE %s: найдено %d сообщений", since_str, len(msg_nums))

        for num in msg_nums:
            ftyp, msg_data = conn.fetch(num, "(RFC822)")
            if ftyp != "OK" or not msg_data:
                continue
            raw_bytes: bytes | None = None
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                    raw_bytes = bytes(part[1])
                    break
            if not raw_bytes:
                continue

            nl = parse_raw_email(
                raw_bytes,
                forwarder_email=eff_fwd,
                lookback_hours=eff_lookback,
                now=ref_now,
            )
            if nl is None:
                continue

            sd_hash = compute_subject_date_hash(nl.subject, nl.date)
            if is_newsletter_seen(nl, seen) or nl.message_id in batch_ids or sd_hash in batch_hashes:
                log.info("Письмо '%s' уже обработано (дедупликация ft_seen) — пропуск", nl.subject[:60])
                continue

            batch_ids.add(nl.message_id)
            batch_hashes.add(sd_hash)
            newsletters.append(nl)
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass

    return newsletters
