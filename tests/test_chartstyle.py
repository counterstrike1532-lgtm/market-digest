"""chartstyle.py: регистрация шрифта Inter + переиспользуемые хелперы отрисовки
(редизайн графиков). Без сети - matplotlib рисует в Agg-backend в память/tmp."""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src import chartstyle


# ---------------------------------------------------------------- регистрация Inter

def test_inter_actually_registered_from_real_assets():
    """Реальные файлы шрифта должны лежать в assets/fonts/ и грузиться при
    импорте модуля - иначе прогон тихо остаётся на DejaVu Sans."""
    assert chartstyle.FONT_FAMILY == "Inter"


def test_register_inter_falls_back_when_files_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(chartstyle, "_FONTS_DIR", tmp_path)
    assert chartstyle._register_inter() == "DejaVu Sans"


def test_register_inter_falls_back_on_exception(monkeypatch, tmp_path):
    """Регистрация шрифта не должна ронять прогон ни при каких обстоятельствах -
    даже если matplotlib.font_manager сам бросит исключение."""
    regular = tmp_path / "Inter-Regular.ttf"
    regular.write_bytes(b"not a real font file")
    monkeypatch.setattr(chartstyle, "_FONTS_DIR", tmp_path)
    assert chartstyle._register_inter() == "DejaVu Sans"


# ---------------------------------------------------------------- хелперы отрисовки

def test_draw_grid_adds_n_lines():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    before = len(ax.lines)
    chartstyle.draw_grid(ax, 90, 110, c, n=5)
    assert len(ax.lines) - before == 5
    plt.close(fig)


def test_draw_grid_noop_on_degenerate_range():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    before = len(ax.lines)
    chartstyle.draw_grid(ax, 100, 100, c, n=5)
    assert len(ax.lines) == before
    plt.close(fig)


def test_draw_baseline_adds_line_and_label():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    chartstyle.draw_baseline(ax, 100, c, "100")
    assert len(ax.lines) == 1
    assert ax.lines[0].get_linestyle() == "--"
    assert any(t.get_text() == "100" for t in ax.texts)
    plt.close(fig)


def test_glow_line_draws_four_passes_correct_order():
    fig, ax = plt.subplots()
    x, y = [0, 1, 2], [100, 102, 101]
    chartstyle.glow_line(ax, x, y, "#A9082C", base_lw=2.6)
    assert len(ax.lines) == 4
    widths = [ln.get_linewidth() for ln in ax.lines]
    alphas = [ln.get_alpha() for ln in ax.lines]
    # три широких прозрачных прохода + один чёткий поверх, в этом порядке
    assert widths == [12, 8, 5, 2.6]
    assert alphas == [0.02, 0.04, 0.06, 1.0]
    plt.close(fig)


def test_gradient_fill_layer_count_and_alpha_decreasing():
    fig, ax = plt.subplots()
    x = [0, 1, 2]
    y_line = [105, 108, 103]
    chartstyle.gradient_fill(ax, x, y_line, y_bottom=90, color="#A9082C", n=14,
                             max_alpha=0.18)
    assert len(ax.collections) == 14
    alphas = [coll.get_alpha() for coll in ax.collections]
    # первый слой (у линии) самый непрозрачный, дальше убывает к низу
    assert alphas[0] == 0.18
    assert all(alphas[i] > alphas[i + 1] for i in range(len(alphas) - 1))
    plt.close(fig)


def test_end_marker_adds_scatter_point():
    fig, ax = plt.subplots()
    before = len(ax.collections)
    chartstyle.end_marker(ax, 5, 103.2, "#A9082C")
    assert len(ax.collections) == before + 1
    plt.close(fig)


def test_value_chip_accent_uses_solid_bg_white_text():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    chartstyle.value_chip(ax, 5, 103.2, "WIG20 TR +3.2%", c, kind="accent")
    ann = ax.texts[-1]
    assert ann.get_text() == "WIG20 TR +3.2%"
    assert ann.get_color() == c["chip_accent_fg"]
    bbox = ann.get_bbox_patch()
    assert bbox is not None
    assert bbox.get_alpha() == 1.0
    plt.close(fig)


def test_value_chip_neutral_uses_translucent_bg():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    chartstyle.value_chip(ax, 5, 100.1, "S&P 500 -0.4%", c, kind="neutral")
    ann = ax.texts[-1]
    bbox = ann.get_bbox_patch()
    assert bbox.get_alpha() == c["chip_neutral_alpha"]
    assert bbox.get_alpha() < 1.0
    plt.close(fig)


def test_titles_writes_kicker_and_title():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    chartstyle.titles(fig, ax, "wig20 tr vs s&p 500", "Markets, last 45 days", c)
    assert ax.get_title(loc="left") == "Markets, last 45 days"
    kicker_texts = [t.get_text() for t in fig.texts]
    assert any("W I G 2 0" in t for t in kicker_texts)   # letter-spacing имитация
    plt.close(fig)


def test_footer_writes_muted_text():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    chartstyle.footer(fig, "Data: yfinance | 01.08.2026", c)
    assert any(t.get_text() == "Data: yfinance | 01.08.2026" for t in fig.texts)
    plt.close(fig)


def test_x_edge_labels_places_first_and_last_date():
    fig, ax = plt.subplots()
    c = chartstyle.colors("dark")
    chartstyle.x_edge_labels(ax, "2026-06-01", "2026-07-17", c)
    texts = [t.get_text() for t in ax.texts]
    assert "2026-06-01" in texts
    assert "2026-07-17" in texts
    plt.close(fig)


# ---------------------------------------------------------------- rc_params / spines

def test_rc_params_disables_all_spines_including_bottom():
    """Раньше нижний спайн был виден белой полосой на карточках - в редизайне
    выключены все четыре явно."""
    rc = chartstyle.rc_params("dark")
    assert rc["axes.spines.top"] is False
    assert rc["axes.spines.right"] is False
    assert rc["axes.spines.left"] is False
    assert rc["axes.spines.bottom"] is False


def test_rc_params_disables_tick_marks_both_axes():
    rc = chartstyle.rc_params("dark")
    assert rc["xtick.bottom"] is False
    assert rc["xtick.top"] is False
    assert rc["ytick.left"] is False
    assert rc["ytick.right"] is False


def test_rc_params_keeps_size_dpi_unchanged():
    rc = chartstyle.rc_params("dark")
    assert rc["figure.figsize"] == (8.0, 4.5)
    assert rc["figure.dpi"] == 150
    assert rc["savefig.dpi"] == 150


def test_rc_params_uses_registered_font_family():
    rc = chartstyle.rc_params("dark")
    assert rc["font.family"] == chartstyle.FONT_FAMILY


def test_colors_light_and_dark_both_have_readable_chip_fields():
    for theme in ("dark", "light"):
        c = chartstyle.colors(theme)
        for key in ("bg", "fg", "muted", "grid", "accent", "neutral",
                   "chip_accent_bg", "chip_accent_fg", "chip_neutral_bg",
                   "chip_neutral_fg", "chip_neutral_alpha"):
            assert key in c
        assert 0 < c["chip_neutral_alpha"] < 1
