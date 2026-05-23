"""Сценарий отправки баг-репорта от пользователя.

Поток:
  1. Пользователь нажимает «🐞 Сообщить о баге» (или /bug)
  2. Бот просит текст описания
  3. Опционально — скриншот (или /skip)
  4. Подтверждение → сохраняется в БД, копия летит всем админам
  5. Админы видят кнопки: «👁 В работе», «✅ Закрыть», «✍️ Ответить»
"""
from html import escape
import logging

from aiogram import Router, types, F, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.config import ADMIN_IDS
from src.database import (
    add_bug_report,
    get_bug_report,
    list_bug_reports,
    set_bug_report_status,
    count_recent_bug_reports,
)
from src.handlers.common import (
    check_access, _menu_for, is_admin,
)
from src.logger import describe_user

router = Router()
logger = logging.getLogger(__name__)


# Лимиты
MAX_TEXT_LEN = 1500
MIN_TEXT_LEN = 10

# Антиспам: не больше 3 баг-репортов в час и 10 в сутки от одного пользователя
HOURLY_LIMIT = 3
DAILY_LIMIT = 10


class BugForm(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()


class AdminReplyForm(StatesGroup):
    """FSM для ответа админа на конкретный баг-репорт."""
    waiting_for_reply = State()


# ---------------- Клавиатуры ----------------

def _bug_cancel_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def _photo_step_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="⏭ Пропустить")],
            [types.KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def _admin_actions_kb(report_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="👁 В работе", callback_data=f"bug_progress:{report_id}"),
            types.InlineKeyboardButton(
                text="✅ Закрыть", callback_data=f"bug_close:{report_id}"),
        ],
        [types.InlineKeyboardButton(
            text="✍️ Ответить пользователю", callback_data=f"bug_reply:{report_id}")],
    ])


# ---------------- Сценарий пользователя ----------------

@router.message(Command("bug"))
@router.message(F.text == "🐞 Сообщить о баге")
async def bug_start(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return

    # Антиспам по лимитам
    hour_count = await count_recent_bug_reports(message.from_user.id, 60)
    if hour_count >= HOURLY_LIMIT:
        logger.info("Лимит баг-репортов (час) для %s превышен.",
                    describe_user(message.from_user))
        await message.answer(
            f"⚠️ Вы уже отправили {hour_count} баг-репорта за последний час. "
            f"Лимит — {HOURLY_LIMIT}/час. Попробуйте позже.",
            reply_markup=_menu_for(message.from_user.id),
        )
        return
    day_count = await count_recent_bug_reports(message.from_user.id, 60 * 24)
    if day_count >= DAILY_LIMIT:
        logger.info("Лимит баг-репортов (сутки) для %s превышен.",
                    describe_user(message.from_user))
        await message.answer(
            f"⚠️ Дневной лимит баг-репортов исчерпан "
            f"({day_count}/{DAILY_LIMIT}). Попробуйте завтра.",
            reply_markup=_menu_for(message.from_user.id),
        )
        return

    logger.info("Пользователь %s начал отправку баг-репорта.",
                describe_user(message.from_user))
    await state.set_state(BugForm.waiting_for_text)
    await message.answer(
        "🐞 <b>Сообщить о баге</b>\n\n"
        "Опишите проблему как можно подробнее: что вы делали, что случилось, "
        "что ожидали увидеть. Чем подробнее — тем быстрее починим.",
        reply_markup=_bug_cancel_kb(),
    )


async def _bug_cancel(message: types.Message, state: FSMContext) -> bool:
    if message.text and message.text.strip() == "❌ Отмена":
        await state.clear()
        await message.answer("❌ Отмена. Баг-репорт не отправлен.",
                              reply_markup=_menu_for(message.from_user.id))
        return True
    return False


@router.message(BugForm.waiting_for_text)
async def bug_text(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _bug_cancel(message, state):
        return

    text = (message.text or "").strip()
    if len(text) < MIN_TEXT_LEN:
        await message.answer(
            f"Слишком коротко (минимум {MIN_TEXT_LEN} символов). Опишите подробнее.",
            reply_markup=_bug_cancel_kb(),
        )
        return
    if len(text) > MAX_TEXT_LEN:
        await message.answer(
            f"Слишком длинно (максимум {MAX_TEXT_LEN} символов). Сократите.",
            reply_markup=_bug_cancel_kb(),
        )
        return

    await state.update_data(bug_text=text)
    await state.set_state(BugForm.waiting_for_photo)
    await message.answer(
        "📸 Если есть <b>скриншот</b> — пришлите его сейчас. "
        "Если нет — нажмите «⏭ Пропустить».",
        reply_markup=_photo_step_kb(),
    )


@router.message(BugForm.waiting_for_photo, F.text == "⏭ Пропустить")
async def bug_skip_photo(message: types.Message, state: FSMContext, bot: Bot):
    await _finalize_report(message, state, bot, photo_file_id=None)


@router.message(BugForm.waiting_for_photo, F.photo)
async def bug_photo(message: types.Message, state: FSMContext, bot: Bot):
    if await _bug_cancel(message, state):
        return
    # photo — список миниатюр, берём самую большую (последнюю)
    file_id = message.photo[-1].file_id
    await _finalize_report(message, state, bot, photo_file_id=file_id)


@router.message(BugForm.waiting_for_photo)
async def bug_photo_invalid(message: types.Message, state: FSMContext):
    if await _bug_cancel(message, state):
        return
    await message.answer(
        "Ожидаю скриншот картинкой или нажатие «⏭ Пропустить».",
        reply_markup=_photo_step_kb(),
    )


async def _finalize_report(message: types.Message, state: FSMContext,
                            bot: Bot, photo_file_id: str | None) -> None:
    data = await state.get_data()
    text = data.get("bug_text", "")

    user = message.from_user
    full_name = (user.full_name or "").strip() or None

    report_id = await add_bug_report(
        telegram_id=user.id,
        username=user.username,
        full_name=full_name,
        text=text,
        photo_file_id=photo_file_id,
    )
    await state.clear()

    await message.answer(
        f"✅ <b>Баг-репорт #{report_id} принят!</b>\n\n"
        "Спасибо за помощь. Мы рассмотрим сообщение и при необходимости "
        "ответим вам в этот же чат.",
        reply_markup=_menu_for(user.id),
    )

    # Шлём админам
    await _notify_admins(bot, report_id)


async def _notify_admins(bot: Bot, report_id: int) -> None:
    """Пересылает баг-репорт всем админам с inline-кнопками действий."""
    if not ADMIN_IDS:
        logger.warning("Не могу уведомить админов о баг-репорте #%s: ADMIN_IDS пуст.",
                       report_id)
        return

    rep = await get_bug_report(report_id)
    if not rep:
        logger.error("Баг-репорт #%s исчез из БД сразу после создания.", report_id)
        return

    # Собираем заголовок
    user_str_parts = [f"id=<code>{rep['telegram_id']}</code>"]
    if rep["username"]:
        user_str_parts.append(f"@{escape(rep['username'])}")
    if rep["full_name"]:
        user_str_parts.append(f"«{escape(rep['full_name'])}»")
    user_str = ", ".join(user_str_parts)

    header = (
        f"🐞 <b>Новый баг-репорт #{rep['id']}</b>\n"
        f"От: {user_str}\n"
        f"<i>{escape(str(rep['created_at']))}</i>\n\n"
        f"<blockquote>{escape(rep['text'])}</blockquote>"
    )

    kb = _admin_actions_kb(rep["id"])

    for admin_id in ADMIN_IDS:
        try:
            if rep["photo_file_id"]:
                await bot.send_photo(
                    admin_id, rep["photo_file_id"],
                    caption=header, reply_markup=kb,
                )
            else:
                await bot.send_message(
                    admin_id, header, reply_markup=kb,
                    disable_web_page_preview=True,
                )
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.warning("Не доставлено админу %s: %s", admin_id, e)


# ---------------- Админская сторона ----------------

@router.message(Command("bugs"))
@router.message(F.text == "🐞 Баг-репорты")
async def cmd_bugs(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    reports = await list_bug_reports(only_open=False, limit=15)
    if not reports:
        await message.answer("📭 Баг-репортов пока нет.")
        return

    lines = ["🐞 <b>Последние баг-репорты:</b>\n"]
    for r in reports:
        emoji = {"new": "🆕", "in_progress": "👁", "closed": "✅"}.get(r["status"], "❔")
        author = f"@{escape(r['username'])}" if r["username"] else f"id={r['telegram_id']}"
        lines.append(
            f"{emoji} <b>#{r['id']}</b> от {author}\n"
            f"   {escape(r['text'][:100])}{'…' if len(r['text']) > 100 else ''}\n"
            f"   <i>{escape(str(r['created_at']))}</i>"
        )
    await message.answer("\n\n".join(lines))


@router.callback_query(F.data.startswith("bug_progress:"))
async def bug_set_progress(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    rid = int(call.data.split(":", 1)[1])
    await set_bug_report_status(rid, "in_progress")
    await call.answer("👁 Помечено как «в работе»")
    try:
        await call.message.edit_reply_markup(reply_markup=_admin_actions_kb(rid))
    except Exception:
        pass


@router.callback_query(F.data.startswith("bug_close:"))
async def bug_close(call: types.CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    rid = int(call.data.split(":", 1)[1])
    rep = await get_bug_report(rid)
    if not rep:
        await call.answer("Не найдено.", show_alert=True)
        return
    await set_bug_report_status(rid, "closed")
    await call.answer("✅ Закрыто")

    # Уведомим автора
    try:
        await bot.send_message(
            rep["telegram_id"],
            f"✅ <b>Баг-репорт #{rid} закрыт.</b>\nСпасибо за помощь!",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass

    try:
        await call.message.edit_reply_markup(reply_markup=_admin_actions_kb(rid))
    except Exception:
        pass


@router.callback_query(F.data.startswith("bug_reply:"))
async def bug_reply_start(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    rid = int(call.data.split(":", 1)[1])
    await state.set_state(AdminReplyForm.waiting_for_reply)
    await state.update_data(_reply_bug_id=rid)
    await call.message.answer(
        f"✍️ Введите ответ для пользователя по баг-репорту <b>#{rid}</b>:",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True,
        ),
    )
    await call.answer()


@router.message(AdminReplyForm.waiting_for_reply)
async def bug_reply_send(message: types.Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    if message.text and message.text.strip() == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.",
                              reply_markup=_menu_for(message.from_user.id))
        return

    text = (message.text or "").strip()
    if len(text) < 1 or len(text) > 2000:
        await message.answer("Ответ должен быть от 1 до 2000 символов.")
        return

    data = await state.get_data()
    rid = data.get("_reply_bug_id")
    if not rid:
        await state.clear()
        return
    rep = await get_bug_report(rid)
    if not rep:
        await state.clear()
        await message.answer("Баг-репорт исчез из БД.")
        return

    await set_bug_report_status(rid, "closed", admin_reply=text)

    notify = (
        f"💬 <b>Ответ по баг-репорту #{rid}:</b>\n\n"
        f"{escape(text)}\n\n"
        f"<i>(статус: ✅ закрыт)</i>"
    )
    try:
        await bot.send_message(rep["telegram_id"], notify)
        delivered = True
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning("Ответ на баг #%s не доставлен: %s", rid, e)
        delivered = False

    await state.clear()
    if delivered:
        await message.answer(
            f"✅ Ответ отправлен пользователю по баг-репорту #{rid}.",
            reply_markup=_menu_for(message.from_user.id),
        )
    else:
        await message.answer(
            "⚠️ Ответ сохранён в БД, но Telegram не доставил его пользователю "
            "(возможно, заблокировал бота).",
            reply_markup=_menu_for(message.from_user.id),
        )
