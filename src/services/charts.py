"""Построение графиков статистики через matplotlib.

matplotlib работает синхронно, но мы вызываем его в run_in_executor чтобы
не блокировать event loop.
"""
import asyncio
import io
import logging
from typing import Sequence

logger = logging.getLogger(__name__)

# Бэкенд без GUI — обязательно, иначе на серверах падает
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _set_dark_theme() -> None:
    """Тёмная тема похожая на Telegram dark mode."""
    plt.rcParams.update({
        "figure.facecolor": "#212121",
        "axes.facecolor": "#212121",
        "axes.edgecolor": "#9e9e9e",
        "axes.labelcolor": "#e0e0e0",
        "xtick.color": "#bdbdbd",
        "ytick.color": "#bdbdbd",
        "text.color": "#e0e0e0",
        "axes.titlecolor": "#ffffff",
        "axes.grid": True,
        "grid.color": "#424242",
        "grid.linestyle": "--",
        "grid.linewidth": 0.5,
    })


def _render_complaints_by_day(by_day: Sequence[tuple[str, int]]) -> bytes:
    """Bar chart: количество жалоб по дням."""
    _set_dark_theme()
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)

    if not by_day:
        ax.text(0.5, 0.5, "Нет данных", ha="center", va="center",
                transform=ax.transAxes, fontsize=14)
    else:
        days = [d for d, _ in by_day]
        counts = [c for _, c in by_day]
        bars = ax.bar(days, counts, color="#42a5f5", edgecolor="#1565c0")
        # Подписи над столбцами
        for b, c in zip(bars, counts):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.1,
                    str(c), ha="center", va="bottom", fontsize=10,
                    color="#e0e0e0")
        ax.set_ylabel("Жалоб")
        plt.xticks(rotation=30, ha="right")

    ax.set_title("Жалобы по дням (последние 7 дней)", pad=12, fontsize=13, weight="bold")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_status_pie(accepted: int, rejected: int, pending: int,
                        closed: int = 0) -> bytes:
    """Pie chart: распределение жалоб по статусам."""
    _set_dark_theme()
    fig, ax = plt.subplots(figsize=(6, 5), dpi=120)

    labels = []
    values = []
    colors = []
    if accepted:
        labels.append(f"Принято: {accepted}"); values.append(accepted); colors.append("#66bb6a")
    if rejected:
        labels.append(f"Отклонено: {rejected}"); values.append(rejected); colors.append("#ef5350")
    if pending:
        labels.append(f"Ожидание: {pending}"); values.append(pending); colors.append("#ffa726")
    if closed:
        labels.append(f"Закрыто: {closed}"); values.append(closed); colors.append("#9e9e9e")

    if not values:
        ax.text(0.5, 0.5, "Нет данных", ha="center", va="center",
                transform=ax.transAxes, fontsize=14)
    else:
        wedges, _texts, autotexts = ax.pie(
            values, labels=labels, colors=colors,
            autopct="%1.0f%%", startangle=90,
            wedgeprops={"edgecolor": "#212121", "linewidth": 2},
            textprops={"color": "#e0e0e0", "fontsize": 10},
        )
        for at in autotexts:
            at.set_color("#212121")
            at.set_weight("bold")

    ax.set_title("Распределение жалоб по статусам", pad=12, fontsize=13, weight="bold")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _render_top_servers(top: Sequence[tuple[str, int]]) -> bytes:
    """Horizontal bar chart: топ серверов по количеству жалоб."""
    _set_dark_theme()
    fig, ax = plt.subplots(figsize=(8, max(3.5, 0.5 * len(top) + 1)), dpi=120)

    if not top:
        ax.text(0.5, 0.5, "Нет данных", ha="center", va="center",
                transform=ax.transAxes, fontsize=14)
    else:
        names = [t[0][:20] for t in top]
        counts = [t[1] for t in top]
        bars = ax.barh(names[::-1], counts[::-1],
                       color="#ab47bc", edgecolor="#6a1b9a")
        for b, c in zip(bars, counts[::-1]):
            ax.text(b.get_width() + max(counts) * 0.02,
                    b.get_y() + b.get_height() / 2,
                    str(c), ha="left", va="center", fontsize=10,
                    color="#e0e0e0")
        ax.set_xlabel("Жалоб")

    ax.set_title("Топ серверов по числу жалоб", pad=12, fontsize=13, weight="bold")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# Async-обёртки чтобы не блокировать loop

async def render_complaints_by_day(by_day: Sequence[tuple[str, int]]) -> bytes:
    return await asyncio.to_thread(_render_complaints_by_day, by_day)


async def render_status_pie(accepted: int, rejected: int,
                              pending: int, closed: int = 0) -> bytes:
    return await asyncio.to_thread(
        _render_status_pie, accepted, rejected, pending, closed
    )


async def render_top_servers(top: Sequence[tuple[str, int]]) -> bytes:
    return await asyncio.to_thread(_render_top_servers, top)
