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
    cancel_queue_item,
)
from src.handlers.common import is_admin, _menu_for
from src.logger import describe_user

router = Router()
logger = logging.getLogger(__name__)


# ---------------- Статистика ----------------

@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    s = await get_stats(within_days=7)
    rate = (s["accepted"] / s["total"] * 100) if s["total"] else 0
    lines = [
        "📊 <b>Статистика за 7 дней</b>\n",
        f"👥 Уникальных пользователей: <b>{s['total_users']}</b>",
        f"📝 Жалоб подано: <b>{s['total']}</b>",
        f"   ✅ Принято: <b>{s['accepted']}</b> ({rate:.0f}%)",
        f"   ❌ Отклонено: <b>{s['rejected']}</b>",
        f"   ⏳ Ожидание: <b>{s['pending']}</b>",
        f"📦 В очереди публикации: <b>{s['queue_pending']}</b>",
    ]

    if s["top_users"]:
        lines.append("\n👤 <b>Топ авторов жалоб:</b>")
        for tg_id, count in s["top_users"]:
            lines.append(f"   • <code>{tg_id}</code> — {count}")

    if s["top_targets"]:
        lines.append("\n🎯 <b>Топ нарушителей:</b>")
        for nick, count in s["top_targets"]:
            lines.append(f"   • <b>{escape(nick)}</b> — {count}")

    if s["by_day"]:
        lines.append("\n📅 <b>По дням:</b>")
        for d, count in s["by_day"]:
            lines.append(f"   {escape(str(d))}: {count}")

    await message.answer("\n".join(lines))


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
        "📢 <b>Рассылка по всем пользователям бота</b>\n\n"
        "Отправьте текст сообщения. Поддерживается HTML (<code>&lt;b&gt;</code>, "
        "<code>&lt;i&gt;</code>, <code>&lt;a href=...&gt;</code>).\n\n"
        "Для отмены — нажмите ❌ Отмена.",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True,
        ),
    )


async def _broadcast_cancel(message: types.Message, state: FSMContext) -> bool:
    if message.text and message.text.strip() == "❌ Отмена":
        await state.clear()
        await message.answer("❌ Рассылка отменена.",
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
    await state.update_data(broadcast_recipients=users)
    await state.set_state(BroadcastForm.waiting_for_confirm)

    preview = (
        f"📢 <b>Рассылка готова</b>\n\n"
        f"<b>Получателей:</b> {len(users)}\n\n"
        f"<b>Превью сообщения:</b>\n"
        f"━━━━━━━━━━━━━━\n{text}\n━━━━━━━━━━━━━━\n\n"
        f"Подтверждаете? (это нельзя отменить после старта)"
    )
    await message.answer(
        preview,
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="✅ Отправить всем")],
                [types.KeyboardButton(text="❌ Отмена")],
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
    users: list[int] = data.get("broadcast_recipients", [])
    await state.clear()

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
    last_edit = asyncio.get_event_loop().time()

    for i, uid in enumerate(users, 1):
        try:
            await bot.send_message(uid, text, disable_web_page_preview=True)
            delivered += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            # Telegram попросил подождать — ждём и повторяем
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text, disable_web_page_preview=True)
                delivered += 1
            except Exception:
                failed += 1
        except (TelegramBadRequest, Exception) as e:
            logger.debug("broadcast: %s -> %s", uid, e)
            failed += 1

        # Anti rate-limit Telegram: ~25-30 сообщений в секунду
        await asyncio.sleep(0.05)

        # Прогресс не чаще раза в 2 секунды
        now = asyncio.get_event_loop().time()
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

    try:
        await status.edit_text(
            f"📢 <b>Рассылка завершена</b>\n\n"
            f"📤 Отправлено: <b>{delivered}</b>\n"
            f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
            f"❌ Других ошибок: <b>{failed}</b>"
        )
    except Exception:
        await message.answer(
            f"📢 Готово: {delivered}/{len(users)}",
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
    """Прогнать мониторинг статусов прямо сейчас, не ждать интервала."""
    if not is_admin(message.from_user.id):
        return
    from src.status_monitor import _check_once
    from src.database import list_complaints_for_status_check
    status_msg = await message.answer("⏳ Запускаю проверку статусов жалоб...")
    try:
        await asyncio.wait_for(_check_once(message.bot), timeout=300)
    except asyncio.TimeoutError:
        await status_msg.edit_text(
            "⏱ Проверка прервана по таймауту (5 мин). Проверьте логи."
        )
        return
    except Exception as e:
        logger.exception("Ошибка ручной проверки статусов")
        await status_msg.edit_text(f"❌ Ошибка: {escape(str(e))}")
        return

    # После прогона показываем сводку — какие жалобы в каком статусе
    from src.database import get_user_complaints
    complaints = await get_user_complaints(message.from_user.id)

    pending_n = sum(1 for c in complaints if c["status"] == "pending")
    accepted_n = sum(1 for c in complaints if c["status"] == "accepted")
    rejected_n = sum(1 for c in complaints if c["status"] == "rejected")
    closed_n = sum(1 for c in complaints if c["status"] == "closed")

    lines = [
        "✅ <b>Проверка статусов завершена</b>\n",
        f"⏳ Ожидание: <b>{pending_n}</b>",
        f"✅ Принято: <b>{accepted_n}</b>",
        f"❌ Отклонено: <b>{rejected_n}</b>",
        f"🔒 Закрыто: <b>{closed_n}</b>",
    ]

    if complaints:
        lines.append("\n<b>Последние 10 жалоб:</b>")
        for c in complaints[:10]:
            from src.status_monitor import status_label
            lbl = status_label(c["status"])
            lines.append(
                f"   <code>#{c['id']}</code> {lbl} • <b>{escape(c['nickname'])}</b>"
            )

    await status_msg.edit_text("\n".join(lines))


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
        status, prefix = await fetch_complaint_status(url, cookies=cookies)
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
    await status_msg.edit_text(
        f"🔍 <b>Результат:</b>\n\n"
        f"URL: <code>{escape(url)}</code>\n"
        f"От имени: <b>{escape(used_acc_name)}</b>\n"
        f"Префикс на форуме: <code>{escape(str(prefix or '—'))}</code>\n"
        f"Распознанный статус: <code>{escape(str(status or '—'))}</code>\n\n"
        f"<b>RAW диагностика:</b>\n{raw_block}"
    )
