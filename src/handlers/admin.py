"""Админские команды: статистика, рассылка, очередь жалоб."""
import asyncio
import logging
from html import escape

from aiogram import Router, types, F, Bot
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.database import (
    get_stats,
    list_all_users,
    list_queue_pending,
    ban_user,
    unban_user,
    list_banned,
    is_banned,
    list_complaints_paginated,
    get_complaint,
    list_subscription_channels,
    add_subscription_channel,
    remove_subscription_channel,
)
from src.handlers.common import is_admin, _menu_for
from src.logger import describe_user
from src.premium_emoji import (
    te,
    PE_CHART_STATS, PE_CHART_GROW, PE_PEOPLE, PE_PERSON_CHECK,
    PE_PERSON_CROSS, PE_MEGAPHONE, PE_BOX, PE_LOCK_CLOSED, PE_LOCK_OPEN,
    PE_CHECK, PE_CROSS, PE_TRASH, PE_LINK, PE_LOADING, PE_BELL,
    PE_INFO, PE_EYE, PE_PENCIL, PE_TIME_PASSED, PE_SEND_UP, PE_BOT,
    PE_ARROW_DOWN_LIST, PE_PROFILE,
)
from src.effects import EFFECT_LIKE, EFFECT_FIRE

router = Router()
logger = logging.getLogger(__name__)


# Глобальный экземпляр BanMiddleware — заполняется при старте бота через
# set_ban_middleware(). Нужен чтобы ban/unban команды могли инвалидировать
# 30-секундный кэш middleware и эффект применился сразу.
_ban_mw = None


def set_ban_middleware(mw) -> None:
    global _ban_mw
    _ban_mw = mw


def _invalidate_ban_cache(telegram_id: int) -> None:
    if _ban_mw is not None:
        try:
            _ban_mw.invalidate(telegram_id)
        except Exception:
            pass


# ---------------- Статистика ----------------

@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    s = await get_stats(within_days=7)
    rate = (s["accepted"] / s["total"] * 100) if s["total"] else 0
    lines = [
        f"{te(PE_CHART_STATS, '📊')} <b>Статистика за 7 дней</b>\n",
        f"{te(PE_PEOPLE, '👥')} Всего пользователей: <b>{s['total_users']}</b>",
        f"   {te(PE_PERSON_CHECK, '🆕')} Новых за 7 дн: "
        f"<b>{s.get('new_users', 0)}</b>",
        f"   {te(PE_CHART_GROW, '🔥')} Активных за 7 дн: "
        f"<b>{s.get('active_users', 0)}</b>",
        f"{te(PE_PENCIL, '📝')} Жалоб подано: <b>{s['total']}</b>",
        f"   {te(PE_CHECK, '✅')} Принято: <b>{s['accepted']}</b> "
        f"({rate:.0f}%)",
        f"   {te(PE_CROSS, '❌')} Отклонено: <b>{s['rejected']}</b>",
        f"   {te(PE_TIME_PASSED, '⏳')} Ожидание: <b>{s['pending']}</b>",
        f"{te(PE_BOX, '📦')} В очереди публикации: "
        f"<b>{s['queue_pending']}</b>",
    ]

    if s["top_users"]:
        lines.append(f"\n{te(PE_PROFILE, '👤')} <b>Топ авторов жалоб:</b>")
        for tg_id, count in s["top_users"]:
            lines.append(f"   • <code>{tg_id}</code> — {count}")

    if s["top_targets"]:
        lines.append(f"\n{te(PE_PERSON_CROSS, '🎯')} <b>Топ нарушителей:</b>")
        for nick, count in s["top_targets"]:
            lines.append(f"   • <b>{escape(nick)}</b> — {count}")

    if s.get("top_servers"):
        lines.append(f"\n{te(PE_CHART_GROW, '🌐')} <b>Топ серверов:</b>")
        for name, count in s["top_servers"]:
            lines.append(f"   • <b>{escape(name)}</b> — {count}")

    if s["by_day"]:
        lines.append(f"\n{te(PE_CHART_STATS, '📅')} <b>По дням:</b>")
        for d, count in s["by_day"]:
            lines.append(f"   {escape(str(d))}: {count}")

    await message.answer("\n".join(lines))

    # Графики — три картинки одна за одной
    try:
        from src.charts import (
            render_complaints_by_day,
            render_status_pie,
            render_top_servers,
        )
        from aiogram.types import BufferedInputFile

        if s.get("by_day"):
            png = await render_complaints_by_day(s["by_day"])
            await message.answer_photo(
                BufferedInputFile(png, filename="by_day.png"),
                caption="📅 Жалобы по дням",
            )

        if s["total"]:
            png = await render_status_pie(
                s["accepted"], s["rejected"], s["pending"],
            )
            await message.answer_photo(
                BufferedInputFile(png, filename="status_pie.png"),
                caption="📊 Распределение по статусам",
            )

        if s.get("top_servers"):
            png = await render_top_servers(s["top_servers"])
            await message.answer_photo(
                BufferedInputFile(png, filename="top_servers.png"),
                caption="🌐 Топ серверов",
            )
    except Exception:
        logger.exception("Не удалось сгенерировать графики статистики")


# ---------------- Рассылка ----------------

class BroadcastForm(StatesGroup):
    waiting_for_text = State()
    waiting_for_confirm = State()


@router.message(Command("broadcast"))
@router.message(F.text == "📢 Рассылка")
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(BroadcastForm.waiting_for_text)
    await message.answer(
        f"{te(PE_MEGAPHONE, '📢')} <b>Рассылка по всем пользователям бота</b>\n\n"
        "Отправьте текст сообщения. Поддерживается HTML (<code>&lt;b&gt;</code>, "
        "<code>&lt;i&gt;</code>, <code>&lt;a href=...&gt;</code>).\n\n"
        f"Для отмены — нажмите {te(PE_CROSS, '❌')} Отмена.",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(
                text="❌ Отмена", icon_custom_emoji_id=PE_CROSS)]],
            resize_keyboard=True,
        ),
    )


async def _broadcast_cancel(message: types.Message, state: FSMContext) -> bool:
    if message.text and message.text.strip() == "❌ Отмена":
        await state.clear()
        await message.answer(f"{te(PE_CROSS, '❌')} Рассылка отменена.",
                              reply_markup=_menu_for(message.from_user.id))
        return True
    return False


@router.message(BroadcastForm.waiting_for_text)
async def broadcast_got_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if await _broadcast_cancel(message, state):
        return

    text = message.html_text or message.text or ""
    if len(text.strip()) < 1:
        await message.answer("Сообщение не может быть пустым.")
        return

    await state.update_data(broadcast_text=text)
    users = await list_all_users()
    # Список получателей в state НЕ храним — на большой базе это лишняя
    # нагрузка на storage, и список может устареть к моменту подтверждения.
    # В broadcast_send перечитаем актуальный список из БД.
    await state.set_state(BroadcastForm.waiting_for_confirm)

    preview = (
        f"{te(PE_MEGAPHONE, '📢')} <b>Рассылка готова</b>\n\n"
        f"{te(PE_PEOPLE, '👥')} <b>Получателей (примерно):</b> {len(users)}\n\n"
        f"<b>Превью сообщения:</b>\n"
        f"━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━\n\n"
        f"Подтверждаете? (это нельзя отменить после старта)"
    )
    await message.answer(
        preview,
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(
                    text="✅ Отправить всем",
                    icon_custom_emoji_id=PE_SEND_UP)],
                [types.KeyboardButton(
                    text="❌ Отмена", icon_custom_emoji_id=PE_CROSS)],
            ],
            resize_keyboard=True,
        ),
    )


@router.message(BroadcastForm.waiting_for_confirm, F.text == "✅ Отправить всем")
async def broadcast_send(message: types.Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()

    # Перечитываем актуальный список получателей из БД (в state не хранили).
    users: list[int] = await list_all_users()

    if not text or not users:
        await message.answer("Сообщение или список получателей пусты.",
                              reply_markup=_menu_for(message.from_user.id))
        return

    status = await message.answer(
        f"⏳ Рассылаю {len(users)} получателям...",
        reply_markup=_menu_for(message.from_user.id),
    )

    delivered = 0
    blocked = 0
    failed = 0
    import time as _time
    last_edit = _time.monotonic()

    for i, uid in enumerate(users, 1):
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            delivered += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            # Telegram попросил подождать — ждём и повторяем (не более 30 сек)
            await asyncio.sleep(min(e.retry_after, 30) + 1)
            try:
                await bot.send_message(uid, text, disable_web_page_preview=True)
                delivered += 1
            except Exception:
                failed += 1
        except TelegramBadRequest as e:
            # Чаще всего — не валидный HTML в тексте админа. Пробуем plain.
            if "parse" in str(e).lower() or "tag" in str(e).lower():
                try:
                    await bot.send_message(
                        uid, text,
                        disable_web_page_preview=True,
                        parse_mode=None,
                    )
                    delivered += 1
                    continue
                except Exception:
                    pass
            logger.debug("broadcast: %s -> %s", uid, e)
            failed += 1
        except Exception as e:
            logger.debug("broadcast: %s -> %s", uid, e)
            failed += 1

        # Anti rate-limit Telegram: ~25-30 сообщений в секунду
        await asyncio.sleep(0.05)

        # Прогресс не чаще раза в 2 секунды
        now = _time.monotonic()
        if now - last_edit > 2 or i == len(users):
            try:
                await status.edit_text(
                    f"⏳ Рассылка: {i}/{len(users)}\n"
                    f"✅ {delivered} • ❌ {blocked + failed}"
                )
            except Exception:
                pass
            last_edit = now

    logger.info("Рассылка от %s: всего %d, доставлено %d, заблокировали %d, ошибок %d.",
                describe_user(message.from_user), len(users),
                delivered, blocked, failed)

    summary = (
        f"{te(PE_MEGAPHONE, '📢')} <b>Рассылка завершена</b>\n\n"
        f"{te(PE_SEND_UP, '📤')} Отправлено: <b>{delivered}</b>\n"
        f"{te(PE_PERSON_CROSS, '🚫')} Заблокировали бота: <b>{blocked}</b>\n"
        f"{te(PE_CROSS, '❌')} Других ошибок: <b>{failed}</b>"
    )
    try:
        # status.edit_text не поддерживает message_effect_id; шлём
        # отдельное сообщение со взлетающим эффектом и убираем статусное.
        await status.delete()
    except Exception:
        pass
    try:
        await message.answer(
            summary,
            reply_markup=_menu_for(message.from_user.id),
            message_effect_id=EFFECT_LIKE,
        )
    except Exception:
        await message.answer(
            f"{te(PE_MEGAPHONE, '📢')} Готово: {delivered}/{len(users)}",
            reply_markup=_menu_for(message.from_user.id),
        )


# ---------------- Состояние очереди (для админа) ----------------

@router.message(Command("queue"))
@router.message(F.text == "📦 Очередь жалоб")
async def cmd_queue(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    pending = await list_queue_pending()
    if not pending:
        await message.answer("📭 Очередь пуста — все жалобы опубликованы.")
        return
    lines = [f"📦 <b>В очереди публикации:</b> {len(pending)}\n"]
    for q in pending[:20]:
        lines.append(
            f"<b>#{q['id']}</b> от <code>{q['telegram_id']}</code> → "
            f"<b>{escape(q['target_nickname'])}</b>\n"
            f"   попыток: {q['attempts']} • <i>{escape(str(q['created_at']))}</i>"
        )
    if len(pending) > 20:
        lines.append(f"\n<i>...и ещё {len(pending) - 20} в очереди</i>")
    await message.answer("\n\n".join(lines))


# ---------------- Принудительный прогон мониторинга ----------------

@router.message(Command("check"))
async def cmd_force_check(message: types.Message):
    """Прогнать мониторинг статусов прямо сейчас, не ждать интервала.
    Для админа показывает сводку по ВСЕМ жалобам в БД (всех пользователей).
    """
    if not is_admin(message.from_user.id):
        return
    from src.status_monitor import _check_once, status_label
    from src.database import (
        list_all_complaints, count_complaints_by_status,
    )

    status_msg = await message.answer(
        "⏳ Запускаю проверку статусов жалоб...\n"
        "<i>Это может занять до минуты — бот идёт на форум по каждой "
        "жалобе.</i>"
    )

    logger.info("Ручной /check от %s — старт.", describe_user(message.from_user))
    try:
        # Жёсткий потолок чтобы пользователь не висел вечно
        await asyncio.wait_for(_check_once(message.bot), timeout=120)
        logger.info("Ручной /check от %s — завершён.",
                    describe_user(message.from_user))
    except asyncio.TimeoutError:
        logger.warning("Ручной /check от %s превысил 2 мин.",
                       describe_user(message.from_user))
        await status_msg.edit_text(
            "⏱ Проверка прервана по таймауту (2 мин). Покажу что успело "
            "проверить — посмотрите ниже."
        )
    except Exception as e:
        logger.exception("Ошибка ручной проверки статусов")
        try:
            await status_msg.edit_text(f"❌ Ошибка: <code>{escape(str(e))}</code>")
        except Exception:
            pass
        return

    # Глобальная сводка — для админа по всем жалобам в БД
    try:
        by_status = await count_complaints_by_status()
        all_complaints = await list_all_complaints(limit=15)
    except Exception:
        logger.exception("Не удалось загрузить статистику жалоб.")
        by_status = {}
        all_complaints = []

    pending_n = by_status.get("pending", 0)
    review_n = by_status.get("review", 0)
    accepted_n = by_status.get("accepted", 0)
    rejected_n = by_status.get("rejected", 0)
    closed_n = by_status.get("closed", 0)
    total_n = sum(by_status.values())

    lines = [
        "✅ <b>Проверка статусов завершена</b>\n",
        f"📦 Всего жалоб в БД: <b>{total_n}</b>",
        f"⏳ Ожидание: <b>{pending_n}</b>",
        f"🔎 На рассмотрении: <b>{review_n}</b>",
        f"✅ Принято: <b>{accepted_n}</b>",
        f"❌ Отклонено: <b>{rejected_n}</b>",
        f"🔒 Закрыто: <b>{closed_n}</b>",
    ]

    if all_complaints:
        lines.append("\n<b>Последние жалобы (всех пользователей):</b>")
        for c in all_complaints[:15]:
            lbl = status_label(c["status"])
            nick = c["nickname"][:25]
            lines.append(
                f"   <code>#{c['id']}</code> {lbl} • "
                f"<b>{escape(nick)}</b> "
                f"<i>(автор {c['telegram_id']})</i>"
            )
    else:
        lines.append("\n<i>В БД пока нет ни одной жалобы.</i>")

    text = "\n".join(lines)
    if len(text) > 3500:
        text = text[:3400] + "\n<i>...сводка обрезана</i>"

    try:
        await status_msg.edit_text(text)
    except Exception:
        try:
            await message.answer(text)
        except Exception:
            logger.exception("Не удалось вывести сводку /check")


@router.message(Command("checkurl"))
async def cmd_check_url(message: types.Message):
    """Проверить статус одной конкретной темы.
    Использование:
        /checkurl <url>                     — проверить под активным аккаунтом
        /checkurl <url> <account_id>        — проверить под конкретным аккаунтом
    """
    if not is_admin(message.from_user.id):
        return
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "<code>/checkurl https://forum.blackrussia.online/threads/...</code>\n"
            "<code>/checkurl URL ACCOUNT_ID</code> — проверить под кук конкретного аккаунта"
        )
        return
    url = args[1].strip()
    account_id = None
    if len(args) >= 3:
        try:
            account_id = int(args[2])
        except ValueError:
            pass

    cookies = None
    used_acc_name = "active cookies.json"
    if account_id is not None:
        from src.database import get_account
        acc = await get_account(account_id)
        if not acc:
            await message.answer(f"❌ Аккаунт id={account_id} не найден.")
            return
        cookies = acc["cookies"]
        used_acc_name = acc["username"]

    from src.forum.xenforo import (
        fetch_complaint_status,
        HEADERS, FORUM_HOST, _solve_ddos_guard,
    )
    import httpx
    from bs4 import BeautifulSoup as _BS
    import re as _re

    status_msg = await message.answer(
        f"⏳ Проверяю {escape(url)} от имени <b>{escape(used_acc_name)}</b>..."
    )

    # Сначала «как обычно» — через нашу функцию
    try:
        status, prefix, comment = await fetch_complaint_status(url, cookies=cookies)
    except Exception as e:
        logger.exception("checkurl failed")
        await status_msg.edit_text(f"❌ Ошибка: <code>{escape(str(e))}</code>")
        return

    # Дополнительно — RAW-диагностика: что реально на странице
    raw_info: list[str] = []
    try:
        async with httpx.AsyncClient(
            cookies=cookies or {}, headers=HEADERS,
            follow_redirects=True, timeout=15.0,
        ) as c:
            r = await c.get(url)
            html = r.text
            if "vddosw3data.js" in html or "slowAES" in html:
                fresh = await _solve_ddos_guard()
                if fresh:
                    c.cookies.set("R3ACTLB", fresh, domain=FORUM_HOST, path="/")
                    r = await c.get(url)
                    html = r.text
            raw_info.append(f"HTTP {r.status_code}")
            raw_info.append(f"final URL: <code>{escape(str(r.url))[:120]}</code>")
            raw_info.append(f"size: {len(html)} байт")

            soup = _BS(html, "html.parser")
            tt = soup.find("title")
            raw_info.append(f"&lt;title&gt;: <code>{escape(tt.text.strip()[:120]) if tt else '—'}</code>")

            og = soup.find("meta", attrs={"property": "og:title"})
            raw_info.append(
                f"og:title: <code>{escape(og['content'][:120]) if og and og.get('content') else '—'}</code>"
            )

            h1 = soup.find(class_="p-title-value") or soup.find("h1")
            if h1:
                raw_info.append(
                    f"h1 классы: <code>{escape(' '.join(h1.get('class') or []))}</code>"
                )
                raw_info.append(
                    f"h1 текст: <code>{escape(h1.get_text(' ', strip=True)[:200])}</code>"
                )
            else:
                raw_info.append("h1 не найден")

            # Все label-элементы с текстом
            label_texts = []
            for el in soup.find_all(class_=_re.compile(r"\blabel\b")):
                t = el.get_text(strip=True)
                if t:
                    label_texts.append(t[:50])
            raw_info.append(f"все .label на странице: {label_texts[:10]}")

    except Exception as e:
        raw_info.append(f"raw fetch error: {escape(str(e))}")

    raw_block = "\n".join(raw_info) if raw_info else "—"
    comment_block = ""
    if comment:
        # Обрезаем чтобы влезло в Telegram
        preview = comment if len(comment) <= 500 else comment[:500] + "..."
        comment_block = (
            f"\n\n💬 <b>Комментарий админа:</b>\n"
            f"<blockquote>{escape(preview)}</blockquote>"
        )

    await status_msg.edit_text(
        f"🔍 <b>Результат:</b>\n\n"
        f"URL: <code>{escape(url)}</code>\n"
        f"От имени: <b>{escape(used_acc_name)}</b>\n"
        f"Префикс на форуме: <code>{escape(str(prefix or '—'))}</code>\n"
        f"Распознанный статус: <code>{escape(str(status or '—'))}</code>"
        f"{comment_block}\n\n"
        f"<b>RAW диагностика:</b>\n{raw_block}"
    )


@router.message(Command("dbinfo"))
async def cmd_db_info(message: types.Message):
    """Диагностика: сколько записей в каждой таблице БД."""
    if not is_admin(message.from_user.id):
        return
    import aiosqlite
    from src.config import DB_PATH

    tables = [
        "complaints", "accounts", "servers", "complaint_categories",
        "user_templates", "bug_reports", "complaint_queue",
    ]
    lines = [f"📊 <b>Состояние БД</b>\n<code>{escape(str(DB_PATH))}</code>\n"]
    async with aiosqlite.connect(DB_PATH) as db:
        for t in tables:
            try:
                async with db.execute(f"SELECT COUNT(*) FROM {t}") as cur:
                    row = await cur.fetchone()
                    n = row[0] if row else 0
                lines.append(f"<code>{t}</code>: <b>{n}</b>")
            except Exception as e:
                lines.append(f"<code>{t}</code>: ❌ {escape(str(e))}")

        # Топ-5 жалоб
        try:
            async with db.execute(
                "SELECT id, telegram_id, nickname, status, forum_thread_url "
                "FROM complaints ORDER BY id DESC LIMIT 5"
            ) as cur:
                rows = await cur.fetchall()
            if rows:
                lines.append("\n<b>Последние жалобы:</b>")
                for r in rows:
                    has_url = "🔗" if r[4] else "—"
                    lines.append(
                        f"   #{r[0]} • от {r[1]} • <b>{escape(r[2])}</b> "
                        f"• <code>{r[3]}</code> {has_url}"
                    )
            else:
                lines.append("\n<i>complaints пуста</i>")
        except Exception:
            pass

    await message.answer("\n".join(lines))


# ---------------- Режим обслуживания ----------------

@router.message(Command("maintenance"))
@router.message(F.text == "🔒 Режим обслуживания")
async def cmd_maintenance(message: types.Message):
    """Переключатель режима обслуживания.
    /maintenance        — показать текущий статус и кнопки переключения
    /maintenance on     — включить
    /maintenance off    — выключить
    """
    if not is_admin(message.from_user.id):
        return

    from src.maintenance import is_enabled, enable, disable

    args = (message.text or "").split(maxsplit=1)
    arg = args[1].strip().lower() if len(args) >= 2 else ""

    if arg == "on":
        await enable()
        await message.answer(
            "🔒 <b>Режим обслуживания включён.</b>\n\n"
            "Все обычные пользователи получат сообщение «бот на техработах». "
            "Админы продолжают работать как обычно.\n\n"
            "Выключить: <code>/maintenance off</code>"
        )
        return
    if arg == "off":
        await disable()
        await message.answer(
            "🔓 <b>Режим обслуживания выключен.</b>\n\n"
            "Бот снова доступен всем пользователям."
        )
        return

    # Без аргумента — показываем статус и кнопки
    enabled = await is_enabled()
    status_emoji = "🔒" if enabled else "🔓"
    status_text = "ВКЛЮЧЕНО" if enabled else "выключено"

    rows: list[list[types.InlineKeyboardButton]] = []
    if enabled:
        rows.append([types.InlineKeyboardButton(
            text="🔓 Выключить (открыть бот для всех)",
            callback_data="maint_off",
            icon_custom_emoji_id=PE_LOCK_OPEN,
        )])
    else:
        rows.append([types.InlineKeyboardButton(
            text="🔒 Включить (закрыть для не-админов)",
            callback_data="maint_on",
            icon_custom_emoji_id=PE_LOCK_CLOSED,
        )])
    kb = types.InlineKeyboardMarkup(inline_keyboard=rows)

    await message.answer(
        f"{status_emoji} <b>Режим обслуживания:</b> {status_text}\n\n"
        "Когда включён, бот для обычных пользователей блокируется и шлёт "
        "пояснение. Админы продолжают работать без ограничений.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "maint_on")
async def maint_on(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    from src.maintenance import enable
    await enable()
    try:
        await call.message.edit_text(
            "🔒 <b>Режим обслуживания включён.</b>\n\n"
            "Все обычные пользователи теперь получат «бот на техработах».\n\n"
            "Выключить: <code>/maintenance off</code> или кнопкой ниже.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="🔓 Выключить", callback_data="maint_off",
                    icon_custom_emoji_id=PE_LOCK_OPEN)],
            ]),
        )
    except Exception:
        pass
    await call.answer("🔒 Включено")


@router.callback_query(F.data == "maint_off")
async def maint_off(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    from src.maintenance import disable
    await disable()
    try:
        await call.message.edit_text(
            "🔓 <b>Режим обслуживания выключен.</b>\n\n"
            "Бот снова доступен всем пользователям.",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="🔒 Включить", callback_data="maint_on",
                    icon_custom_emoji_id=PE_LOCK_CLOSED)],
            ]),
        )
    except Exception:
        pass
    await call.answer("🔓 Выключено")


# ---------------- Бан / разбан пользователей ----------------

def _parse_ban_args(args: str) -> tuple[int | None, str | None]:
    """Парсит «<id> [причина...]» → (id, reason). Возвращает (None, None)
    если id не распознан."""
    args = (args or "").strip()
    if not args:
        return None, None
    parts = args.split(maxsplit=1)
    raw_id = parts[0].lstrip("@").lstrip("#")
    if not raw_id.isdigit():
        return None, None
    reason = parts[1].strip() if len(parts) > 1 else None
    return int(raw_id), reason


@router.message(Command("ban"))
async def cmd_ban(message: types.Message, command: Command):
    """Забанить пользователя в боте. Использование:
        /ban <telegram_id> [причина]
    """
    if not is_admin(message.from_user.id):
        return
    user_id, reason = _parse_ban_args(command.args or "")
    if user_id is None:
        await message.answer(
            "Использование: <code>/ban &lt;telegram_id&gt; [причина]</code>\n"
            "Пример: <code>/ban 123456789 спам жалобами</code>"
        )
        return

    if is_admin(user_id):
        await message.answer("🔒 Нельзя забанить админа.")
        return

    new_ban = await ban_user(
        telegram_id=user_id, reason=reason, banned_by=message.from_user.id,
    )
    _invalidate_ban_cache(user_id)
    logger.info("Админ %s забанил user_id=%s. Причина: %s.",
                describe_user(message.from_user), user_id, reason or "—")

    suffix = "🆕" if new_ban else "🔁 (обновлён)"
    reason_part = f"\nПричина: <i>{escape(reason)}</i>" if reason else ""
    await message.answer(
        f"🚫 <b>Пользователь {user_id} забанен</b> {suffix}.{reason_part}"
    )


@router.message(Command("unban"))
async def cmd_unban(message: types.Message, command: Command):
    """Разбанить пользователя. Использование: /unban <telegram_id>"""
    if not is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip().lstrip("@").lstrip("#")
    if not raw.isdigit():
        await message.answer(
            "Использование: <code>/unban &lt;telegram_id&gt;</code>"
        )
        return
    user_id = int(raw)
    ok = await unban_user(user_id)
    _invalidate_ban_cache(user_id)
    if ok:
        logger.info("Админ %s разбанил user_id=%s.",
                    describe_user(message.from_user), user_id)
        await message.answer(f"✅ Пользователь {user_id} разбанен.")
    else:
        await message.answer(f"ℹ️ Пользователь {user_id} не был забанен.")


@router.message(Command("baninfo"))
async def cmd_baninfo(message: types.Message, command: Command):
    """Информация о бане конкретного пользователя.
    Использование: /baninfo <telegram_id>"""
    if not is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip().lstrip("@").lstrip("#")
    if not raw.isdigit():
        await message.answer(
            "Использование: <code>/baninfo &lt;telegram_id&gt;</code>"
        )
        return
    rec = await is_banned(int(raw))
    if not rec:
        await message.answer(f"✅ Пользователь {raw} не забанен.")
        return
    reason = escape(rec.get("reason") or "—")
    by = rec.get("banned_by") or "—"
    when = escape(str(rec.get("banned_at") or "—"))
    await message.answer(
        f"🚫 <b>Пользователь {raw} забанен</b>\n"
        f"Причина: <i>{reason}</i>\n"
        f"Кто: <code>{by}</code>\n"
        f"Когда: <code>{when}</code>"
    )


@router.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    """Все забаненные пользователи."""
    if not is_admin(message.from_user.id):
        return
    bans = await list_banned()
    if not bans:
        await message.answer("📭 Список банов пуст.")
        return

    lines = [f"🚫 <b>Забанено: {len(bans)}</b>\n"]
    for b in bans[:50]:
        reason = escape(b["reason"]) if b["reason"] else "<i>без причины</i>"
        lines.append(
            f"• <code>{b['telegram_id']}</code> — {reason}\n"
            f"   <i>{escape(str(b['banned_at']))}</i>"
        )
    if len(bans) > 50:
        lines.append(f"\n…и ещё {len(bans) - 50}")
    await message.answer("\n\n".join(lines))


# ---------------- Просмотр всех жалоб (с пагинацией) ----------------

# Размер страницы для /complaints
COMPLAINTS_PAGE_SIZE = 8


def _complaints_list_kb(page: int, total_pages: int,
                          status_filter: str | None) -> types.InlineKeyboardMarkup:
    """Строит клавиатуру навигации по списку жалоб."""
    rows: list[list[types.InlineKeyboardButton]] = []
    nav: list[types.InlineKeyboardButton] = []
    if page > 1:
        nav.append(types.InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"adm_cs:{page - 1}:{status_filter or 'all'}",
        ))
    nav.append(types.InlineKeyboardButton(
        text=f"📄 {page}/{total_pages or 1}",
        callback_data="adm_cs_noop",
        icon_custom_emoji_id=PE_ARROW_DOWN_LIST,
    ))
    if page < total_pages:
        nav.append(types.InlineKeyboardButton(
            text="Вперёд ▶️",
            callback_data=f"adm_cs:{page + 1}:{status_filter or 'all'}",
        ))
    if nav:
        rows.append(nav)

    # Фильтры по статусу
    rows.append([
        types.InlineKeyboardButton(
            text="🔄 Все" + (" ✓" if not status_filter else ""),
            callback_data="adm_cs:1:all",
            icon_custom_emoji_id=PE_LOADING,
        ),
        types.InlineKeyboardButton(
            text="⏳ В ожидании" + (" ✓" if status_filter == "pending" else ""),
            callback_data="adm_cs:1:pending",
            icon_custom_emoji_id=PE_TIME_PASSED,
        ),
    ])
    rows.append([
        types.InlineKeyboardButton(
            text="🔎 Рассмотр." + (" ✓" if status_filter == "review" else ""),
            callback_data="adm_cs:1:review",
            icon_custom_emoji_id=PE_EYE,
        ),
        types.InlineKeyboardButton(
            text="🔒 Закрыто" + (" ✓" if status_filter == "closed" else ""),
            callback_data="adm_cs:1:closed",
            icon_custom_emoji_id=PE_LOCK_CLOSED,
        ),
    ])
    rows.append([
        types.InlineKeyboardButton(
            text="✅ Принято" + (" ✓" if status_filter == "accepted" else ""),
            callback_data="adm_cs:1:accepted",
            icon_custom_emoji_id=PE_CHECK,
        ),
        types.InlineKeyboardButton(
            text="❌ Отказано" + (" ✓" if status_filter == "rejected" else ""),
            callback_data="adm_cs:1:rejected",
            icon_custom_emoji_id=PE_CROSS,
        ),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _complaint_actions_kb(complaint_id: int,
                            has_thread: bool) -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []
    if has_thread:
        rows.append([types.InlineKeyboardButton(
            text="🔗 Открыть на форуме",
            callback_data=f"adm_c_open_url:{complaint_id}",
            icon_custom_emoji_id=PE_LINK,
        )])
        rows.append([types.InlineKeyboardButton(
            text="🗑 Удалить с форума и из БД",
            callback_data=f"adm_c_delf:{complaint_id}",
            icon_custom_emoji_id=PE_TRASH,
        )])
    rows.append([
        types.InlineKeyboardButton(
            text="🗂 Удалить из БД",
            callback_data=f"adm_c_del:{complaint_id}",
            icon_custom_emoji_id=PE_TRASH,
        ),
        types.InlineKeyboardButton(
            text="🚫 Забанить автора",
            callback_data=f"adm_c_banauth:{complaint_id}",
            icon_custom_emoji_id=PE_PERSON_CROSS,
        ),
    ])
    rows.append([types.InlineKeyboardButton(
        text="◀️ К списку", callback_data="adm_cs:1:all",
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _format_complaint_short(c: dict) -> str:
    """Строка для списка."""
    status_emoji = {
        "pending": "⏳", "review": "🔎",
        "accepted": "✅", "rejected": "❌", "closed": "🔒",
    }.get(c["status"], "❔")
    summary = c.get("summary") or ""
    target = escape(c["nickname"])
    info = f"{status_emoji} <b>#{c['id']}</b> • <code>{c['telegram_id']}</code> → <b>{target}</b>"
    if summary:
        info += f" • <i>{escape(summary)}</i>"
    return info


async def _render_complaints_page(page: int,
                                    status_filter: str | None) -> tuple[str, types.InlineKeyboardMarkup]:
    items, total = await list_complaints_paginated(
        page=page,
        page_size=COMPLAINTS_PAGE_SIZE,
        status=status_filter,
    )
    total_pages = max(1, (total + COMPLAINTS_PAGE_SIZE - 1) // COMPLAINTS_PAGE_SIZE)

    if total == 0:
        text = (
            "📭 <b>Нет жалоб</b>"
            + (f" в статусе «{status_filter}»" if status_filter else "")
            + "."
        )
    else:
        header = f"📋 <b>Жалобы — стр. {page}/{total_pages}</b>"
        if status_filter:
            header += f"  •  фильтр: <code>{status_filter}</code>"
        header += f"\n<i>Всего: {total}</i>\n"
        body = "\n\n".join(_format_complaint_short(c) for c in items)
        body += "\n\n<i>Нажмите кнопку ниже чтобы открыть подробности конкретной жалобы.</i>"
        text = f"{header}\n{body}"

    rows: list[list[types.InlineKeyboardButton]] = []
    # Кнопки на конкретные жалобы
    for c in items:
        rows.append([types.InlineKeyboardButton(
            text=f"📄 #{c['id']} {c['nickname']}",
            callback_data=f"adm_c_open:{c['id']}",
        )])
    # Навигация
    nav_kb = _complaints_list_kb(page, total_pages, status_filter)
    rows.extend(nav_kb.inline_keyboard)
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("complaints"))
async def cmd_complaints(message: types.Message):
    """Список всех жалоб (для админа). Поддерживает фильтр по статусу."""
    if not is_admin(message.from_user.id):
        return
    text, kb = await _render_complaints_page(page=1, status_filter=None)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "adm_cs_noop")
async def adm_cs_noop(call: types.CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("adm_cs:"))
async def adm_complaints_navigate(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    parts = call.data.split(":", 2)
    page = int(parts[1])
    status_arg = parts[2] if len(parts) > 2 else "all"
    status_filter = None if status_arg == "all" else status_arg

    text, kb = await _render_complaints_page(page=page, status_filter=status_filter)
    try:
        await call.message.edit_text(
            text, reply_markup=kb, disable_web_page_preview=True,
        )
    except Exception:
        # содержимое не изменилось — просто закрываем спиннер
        pass
    await call.answer()


@router.callback_query(F.data.startswith("adm_c_open:"))
async def adm_complaint_open(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return

    status_label_map = {
        "pending":  "⏳ В ожидании",
        "review":   "🔎 На рассмотрении",
        "accepted": "✅ Принята",
        "rejected": "❌ Отклонена",
        "closed":   "🔒 Закрыта",
    }
    status = comp.get("status") or "pending"
    label = status_label_map.get(status, status)

    target = escape(comp["nickname"])
    your_nick = escape(comp.get("your_nickname") or "—")
    summary = escape(comp.get("summary") or "—")
    description = escape((comp.get("description") or "")[:1500])
    proof = escape(comp.get("proof_link") or "—")
    server = escape(comp.get("server_name") or "—")
    category = escape(comp.get("category_key") or "—")
    pdate = escape(comp.get("punishment_date") or "—")
    created = escape(str(comp.get("created_at") or "—"))
    thread_url = comp.get("forum_thread_url")
    admin_comment = (comp.get("admin_comment") or "").strip()

    # Если темы есть, но коммент в БД пуст — попробуем подтянуть с форума
    # прямо сейчас. Это нужно для старых жалоб (миграция только добавила
    # поле, мониторинг ещё не успел заполнить).
    if thread_url and not admin_comment:
        try:
            from src.forum.xenforo import fetch_thread_admin_comment
            from src.database import get_account, update_complaint_admin_comment
            cookies_to_use = None
            if comp.get("account_id"):
                acc_full = await get_account(comp["account_id"])
                if acc_full and acc_full.get("cookies"):
                    cookies_to_use = acc_full["cookies"]
            fetched = await fetch_thread_admin_comment(
                thread_url, cookies=cookies_to_use,
            )
            if fetched:
                admin_comment = fetched.strip()
                # Сохраняем в БД, чтобы в следующий раз не идти на форум
                await update_complaint_admin_comment(comp["id"], admin_comment)
        except Exception:
            logger.debug("Не удалось подтянуть admin_comment с форума",
                         exc_info=True)

    parts = [
        f"📄 <b>Жалоба #{comp['id']}</b> — {label}",
        f"<i>Создана: {created}</i>",
        "",
        f"👤 Автор: <code>{comp['telegram_id']}</code>",
        f"🎮 Сервер: {server}",
        f"📁 Категория: <code>{category}</code>",
        f"🔫 Цель: <b>{target}</b>",
        f"📝 Ваш ник в жалобе: <b>{your_nick}</b>",
        f"📌 Суть: {summary}",
    ]
    if pdate and pdate != "—":
        parts.append(f"📅 Дата наказания: {pdate}")
    parts.append("")
    parts.append(f"📖 <b>Описание:</b>\n<blockquote>{description}</blockquote>")
    parts.append(f"🔗 <b>Доказательства:</b>\n<code>{proof}</code>")
    if admin_comment:
        # Обрезаем очень длинные комментарии чтобы не упереться в лимит Telegram
        snippet = admin_comment if len(admin_comment) <= 1500 \
            else admin_comment[:1500] + "..."
        parts.append(
            f"\n💬 <b>Комментарий админа форума:</b>\n"
            f"<blockquote>{escape(snippet)}</blockquote>"
        )
    if thread_url:
        parts.append(
            f"\n🌐 <a href=\"{escape(thread_url)}\">Тема на форуме</a>"
        )

    text = "\n".join(parts)
    kb = _complaint_actions_kb(comp["id"], has_thread=bool(thread_url))
    try:
        await call.message.edit_text(
            text, reply_markup=kb, disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        # если очень длинное — отправляем новым сообщением
        await call.message.answer(
            text, reply_markup=kb, disable_web_page_preview=True,
        )
    await call.answer()


@router.callback_query(F.data.startswith("adm_c_open_url:"))
async def adm_complaint_open_url(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp or not comp.get("forum_thread_url"):
        await call.answer("Нет ссылки.", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        f"🔗 {escape(comp['forum_thread_url'])}",
        disable_web_page_preview=False,
    )


@router.callback_query(F.data.startswith("adm_c_del:"))
async def adm_complaint_delete(call: types.CallbackQuery):
    """Удаление жалобы только из БД (тема на форуме остаётся)."""
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return
    from src.database import admin_delete_complaint
    ok = await admin_delete_complaint(cid)
    if ok:
        logger.info("Админ %s удалил жалобу #%s из БД.",
                    describe_user(call.from_user), cid)
        await call.answer("🗂 Удалена из БД", show_alert=False)
        text, kb = await _render_complaints_page(page=1, status_filter=None)
        try:
            await call.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True,
            )
        except Exception:
            pass
    else:
        await call.answer("Не удалось удалить.", show_alert=True)


@router.callback_query(F.data.startswith("adm_c_delf:"))
async def adm_complaint_delete_forum(call: types.CallbackQuery):
    """Удаление темы на форуме + жалобы из БД. Использует куки того
    аккаунта, под которым жалоба подавалась (только автор может удалять)."""
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return
    if not comp.get("forum_thread_url"):
        await call.answer("У жалобы нет ссылки на форум.", show_alert=True)
        return

    await call.answer("⏳ Удаляю тему на форуме...")
    try:
        await call.message.edit_text(
            "⏳ <b>Удаляю тему на форуме и из БД...</b>"
        )
    except Exception:
        pass

    # Куки аккаунта-автора жалобы — передаём в delete_thread напрямую,
    # без apply_account_cookies (иначе гонка на глобальном cookies.json).
    from src.database import get_account, admin_delete_complaint
    from src.forum.xenforo import delete_thread

    cookies_to_use = None
    if comp.get("account_id"):
        acc_full = await get_account(comp["account_id"])
        if acc_full and acc_full.get("cookies"):
            cookies_to_use = acc_full["cookies"]

    success, msg = await delete_thread(
        comp["forum_thread_url"],
        reason="Удалено администратором бота",
        cookies=cookies_to_use,
    )

    if success:
        await admin_delete_complaint(cid)
        logger.info("Админ %s удалил жалобу #%s с форума и из БД (%s).",
                    describe_user(call.from_user), cid, msg)
        try:
            await call.message.edit_text(
                f"🗑 <b>Жалоба #{cid} удалена</b> с форума и из БД.\n\n"
                f"<i>{escape(str(msg))}</i>"
            )
        except Exception:
            pass
    else:
        logger.warning("Не удалось удалить жалобу #%s с форума: %s", cid, msg)
        # Спросим — удалить только из БД? Кнопкой.
        try:
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="🗂 Удалить только из БД",
                    callback_data=f"adm_c_del:{cid}",
                )],
                [types.InlineKeyboardButton(
                    text="◀️ Назад к карточке",
                    callback_data=f"adm_c_open:{cid}",
                )],
            ])
            await call.message.edit_text(
                f"❌ <b>Не удалось удалить тему на форуме.</b>\n\n"
                f"<i>{escape(str(msg))}</i>\n\n"
                f"Возможно, истёк срок удаления или у аккаунта нет прав.\n"
                f"Можно удалить только из БД (тема на форуме останется):",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


@router.message(Command("delcomplaint"))
async def cmd_delcomplaint(message: types.Message, command: Command):
    """Удаление жалобы по id из БД (без удаления темы на форуме).
    Использование: /delcomplaint <id>"""
    if not is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer(
            "Использование: <code>/delcomplaint &lt;id&gt;</code>\n\n"
            "Чтобы удалить и с форума — откройте жалобу через "
            "<code>/complaints</code> и нажмите «🗑 Удалить с форума и из БД»."
        )
        return
    cid = int(raw)
    comp = await get_complaint(cid)
    if not comp:
        await message.answer(f"Жалоба #{cid} не найдена.")
        return
    from src.database import admin_delete_complaint
    ok = await admin_delete_complaint(cid)
    if ok:
        logger.info("Админ %s удалил жалобу #%s через /delcomplaint.",
                    describe_user(message.from_user), cid)
        await message.answer(
            f"🗂 Жалоба #{cid} удалена из БД.\n"
            f"Тема на форуме (если была) <b>сохранена</b>."
        )
    else:
        await message.answer(f"Не удалось удалить жалобу #{cid}.")


@router.callback_query(F.data.startswith("adm_c_banauth:"))
async def adm_complaint_ban_author(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return
    author_id = comp["telegram_id"]
    if is_admin(author_id):  # защита: не банить коллег-админов
        await call.answer("🔒 Нельзя банить других админов.", show_alert=True)
        return
    if author_id == call.from_user.id:
        await call.answer("🔒 Нельзя забанить самого себя.", show_alert=True)
        return

    await ban_user(
        telegram_id=author_id,
        reason=f"Бан через карточку жалобы #{cid}",
        banned_by=call.from_user.id,
    )
    _invalidate_ban_cache(author_id)
    logger.info("Админ %s забанил автора жалобы #%s (user_id=%s).",
                describe_user(call.from_user), cid, author_id)
    await call.answer(f"🚫 Забанен {author_id}", show_alert=True)


# ---------------- Управление обязательной подпиской (/subs) ----------------

class SubsForm(StatesGroup):
    waiting_for_channel = State()


def _build_subs_menu(channels: list[str]) -> types.InlineKeyboardMarkup:
    """Inline-меню: список каналов с кнопкой ❌ для удаления + ➕ Добавить."""
    rows: list[list[types.InlineKeyboardButton]] = []
    if channels:
        for ch in channels:
            rows.append([
                types.InlineKeyboardButton(
                    text=f"❌ @{ch}",
                    callback_data=f"subs:remove:{ch}",
                    icon_custom_emoji_id=PE_TRASH,
                ),
            ])
    else:
        # Если каналов нет — сообщим через пустую строку
        rows.append([
            types.InlineKeyboardButton(
                text="(список пуст)", callback_data="subs:noop",
            ),
        ])
    rows.append([
        types.InlineKeyboardButton(
            text="➕ Добавить канал", callback_data="subs:add",
            icon_custom_emoji_id=PE_MEGAPHONE,
        ),
    ])
    rows.append([
        types.InlineKeyboardButton(
            text="🔄 Обновить", callback_data="subs:list",
            icon_custom_emoji_id=PE_LOADING,
        ),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _subs_text(channels: list[str]) -> str:
    if not channels:
        return (
            f"{te(PE_BELL, '🔔')} <b>Проверка подписки на каналы</b>\n\n"
            "Сейчас список пуст — бот не требует подписки ни на какие каналы.\n"
            "Нажмите <b>«➕ Добавить канал»</b>, чтобы добавить первый."
        )
    listed = "\n".join(f"  • @{ch}" for ch in channels)
    return (
        f"{te(PE_BELL, '🔔')} <b>Проверка подписки на каналы</b>\n\n"
        "Пользователи должны быть подписаны на все каналы ниже, "
        "иначе бот будет показывать приглашение подписаться.\n\n"
        f"<b>Текущий список ({len(channels)}):</b>\n{listed}\n\n"
        "• <b>➕ Добавить канал</b> — добавить новый канал\n"
        "• <b>❌ @канал</b> — удалить канал из списка\n"
        "• <b>🔄 Обновить</b> — перерисовать меню"
    )


@router.message(Command("subs"))
async def cmd_subs(message: types.Message):
    """Главное меню управления обязательной подпиской (только админам)."""
    if not is_admin(message.from_user.id):
        return
    channels = await list_subscription_channels()
    await message.answer(
        _subs_text(channels), reply_markup=_build_subs_menu(channels),
    )


@router.callback_query(F.data == "subs:list")
async def cb_subs_list(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Недоступно.", show_alert=True)
        return
    channels = await list_subscription_channels()
    try:
        await call.message.edit_text(
            _subs_text(channels), reply_markup=_build_subs_menu(channels),
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "subs:noop")
async def cb_subs_noop(call: types.CallbackQuery):
    # Заглушка для неактивной кнопки "(список пуст)"
    await call.answer()


@router.callback_query(F.data == "subs:add")
async def cb_subs_add(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Недоступно.", show_alert=True)
        return
    await state.set_state(SubsForm.waiting_for_channel)
    await call.message.answer(
        "➕ <b>Добавление канала</b>\n\n"
        "Отправьте <b>username</b> канала одним сообщением.\n"
        "Можно с <code>@</code> или ссылкой <code>t.me/...</code>.\n\n"
        "Примеры:\n"
        "  <code>@borzyyyy1</code>\n"
        "  <code>https://t.me/tgkborzov</code>\n\n"
        "Для отмены — напишите <code>отмена</code>.",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True,
        ),
    )
    await call.answer()


@router.message(SubsForm.waiting_for_channel)
async def subs_got_channel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or "").strip()
    if txt.lower() in ("отмена", "❌ отмена", "/cancel"):
        await state.clear()
        await message.answer(
            "❌ Добавление канала отменено.",
            reply_markup=_menu_for(message.from_user.id),
        )
        return
    ok, result = await add_subscription_channel(txt, added_by=message.from_user.id)
    await state.clear()
    if ok:
        # Сбрасываем кеш middleware, чтобы новые каналы начали проверяться
        # сразу же у всех пользователей.
        try:
            from src.subscription import _get_active_middleware
            mw = _get_active_middleware()
            if mw is not None:
                mw.invalidate()
        except Exception:
            pass
        await message.answer(
            f"✅ Канал <b>@{escape(result)}</b> добавлен в список.",
            reply_markup=_menu_for(message.from_user.id),
        )
        # Перерисуем меню /subs
        channels = await list_subscription_channels()
        await message.answer(
            _subs_text(channels), reply_markup=_build_subs_menu(channels),
        )
    else:
        await message.answer(
            f"{result}\n\nПопробуйте ещё раз или нажмите ❌ Отмена.",
            reply_markup=types.ReplyKeyboardMarkup(
                keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
                resize_keyboard=True,
            ),
        )
        # Возвращаем в состояние, чтобы можно было сразу попробовать снова
        await state.set_state(SubsForm.waiting_for_channel)


@router.callback_query(F.data.startswith("subs:remove:"))
async def cb_subs_remove(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Недоступно.", show_alert=True)
        return
    channel = (call.data or "").split(":", 2)[-1]
    ok, result = await remove_subscription_channel(channel)
    if ok:
        try:
            from src.subscription import _get_active_middleware
            mw = _get_active_middleware()
            if mw is not None:
                mw.invalidate()
        except Exception:
            pass
        await call.answer(f"Удалён @{result}", show_alert=False)
    else:
        await call.answer(result, show_alert=True)
        return
    # Перерисовываем меню
    channels = await list_subscription_channels()
    try:
        await call.message.edit_text(
            _subs_text(channels), reply_markup=_build_subs_menu(channels),
        )
    except Exception:
        pass

