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
