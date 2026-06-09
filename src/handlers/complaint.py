import asyncio
import logging
from html import escape
from math import ceil
from aiogram import Router, types, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from src.config import COMPLAINT_CATEGORY_LABELS
from src.forum.xenforo import (
    post_complaint, delete_thread, edit_thread_post,
    is_auth_error, is_noperm_error,
)
from src.forum.templates import (
    RULES,
    NEEDS_DATE,
    TARGET_LABEL,
    build_body,
    build_title,
    get_builtin_templates,
)
from src.database import (
    add_complaint,
    get_user_complaints,
    get_complaint,
    delete_complaint,
    get_servers,
    get_complaint_categories,
    get_active_account,
    get_account,
    update_account_cookies,
    find_available_account,
    set_account_cooldown,
    set_active_account,
    list_user_templates,
    get_user_template,
    add_user_template,
    delete_user_template,
    update_user_template,
    update_complaint_content,
    save_draft,
    get_draft,
    delete_draft,
    enqueue_complaint,
    mark_account_needs_reauth,
    search_complaints_by_nick,
)
from src.handlers.common import (
    check_access, _menu_for, is_admin, account_owner_id,
)
from src.logger import describe_user
from src.effects import EFFECT_CONFETTI
from src.status_monitor import status_label
from src.validation import (
    validate_nickname,
    validate_date,
    validate_summary,
    validate_description,
    validate_proof,
)

router = Router()
logger = logging.getLogger(__name__)

# Сколько серверов на одной странице inline-клавиатуры
SERVERS_PER_PAGE = 8

# Кулдаун аккаунта после публикации жалобы (антифлуд форума).
# 180 секунд — стандартный таймаут на BR между темами в одном разделе.
COMPLAINT_COOLDOWN_SECONDS = 180


def _format_cooldown(seconds: int) -> str:
    """Преобразует секунды в строку 'Xм Yс' или 'Yс'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}с"
    return f"{seconds // 60}м {seconds % 60}с"


async def _pick_account_for_complaint(telegram_id: int) -> tuple[dict | None, str | None]:
    """Подбирает аккаунт для подачи жалобы.

    Для админа — из его собственных аккаунтов. Для обычного пользователя —
    из пула первого админа (общий пул).

    Возвращает (account, error_message). Если все аккаунты в кулдауне —
    account=None и error_message с подсказкой когда можно попробовать.
    """
    owner_id = account_owner_id(telegram_id)
    candidate = await find_available_account(owner_id)
    if not candidate:
        if is_admin(telegram_id):
            return None, (
                "⚠️ У вас нет ни одного сохранённого форумного аккаунта.\n"
                "Нажмите <b>🔐 Войти по паролю</b> или <b>👥 Аккаунты</b> "
                "для добавления."
            )
        return None, (
            "⚠️ Бот пока не настроен — у администратора нет добавленных "
            "форумных аккаунтов. Попробуйте позже."
        )

    if not candidate["available"]:
        # Все в кулдауне
        remaining = candidate["cooldown_remaining_seconds"]
        return None, (
            f"⏳ Все аккаунты сейчас в кулдауне после публикации жалоб.\n"
            f"Самый ранний освободится через <b>{_format_cooldown(remaining)}</b>."
        )

    # Активным аккаунт глобально не делаем — куки публикации передаются
    # в post_complaint напрямую через cookies=, а cookies.json остаётся
    # под админскими сценариями (просмотр статусов, ручные команды).
    # Это критично для параллельной работы нескольких пользователей.
    logger.info("Жалоба будет подана от имени аккаунта «%s» (id=%s, owner=%s).",
                candidate["username"], candidate["id"], owner_id)
    return candidate, None


class ComplaintForm(StatesGroup):
    choosing_server = State()
    choosing_category = State()
    choosing_template = State()
    waiting_for_your_nickname = State()
    waiting_for_target_nickname = State()
    waiting_for_punishment_date = State()
    waiting_for_summary = State()
    waiting_for_description = State()
    waiting_for_proof = State()
    waiting_for_confirm = State()


class TemplateForm(StatesGroup):
    """FSM для добавления своего шаблона."""
    waiting_for_name = State()
    waiting_for_summary = State()
    waiting_for_description = State()


class TemplateEditForm(StatesGroup):
    """FSM для редактирования шаблона. После выбора поля — ждёт новое значение."""
    waiting_for_value = State()


class EditForm(StatesGroup):
    """FSM для редактирования уже опубликованной жалобы."""
    waiting_for_field = State()
    waiting_for_new_description = State()
    waiting_for_new_proof = State()


# ---------------- Клавиатуры ----------------

def _servers_keyboard(servers: list, page: int) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура со списком серверов с пагинацией."""
    total_pages = max(1, ceil(len(servers) / SERVERS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))

    start = page * SERVERS_PER_PAGE
    end = start + SERVERS_PER_PAGE
    chunk = servers[start:end]

    rows: list[list[types.InlineKeyboardButton]] = []
    # По 2 кнопки в строке
    for i in range(0, len(chunk), 2):
        row = []
        for entry in chunk[i:i + 2]:
            name, node_id = entry[0], entry[1]
            # В callback_data кладём ТОЛЬКО node_id: Telegram ограничивает
            # callback_data 64 байтами, а имя сервера приходит из HTML форума
            # и может быть длинным/кириллическим (2 байта на символ) — это
            # роняло бы отрисовку всей клавиатуры. Имя достаём из state по id.
            row.append(types.InlineKeyboardButton(
                text=name,
                callback_data=f"srv_pick:{node_id}",
            ))
        rows.append(row)

    # Навигация
    nav: list[types.InlineKeyboardButton] = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text="◀️", callback_data=f"srv_page:{page - 1}"))
    nav.append(types.InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="srv_noop"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton(text="▶️", callback_data=f"srv_page:{page + 1}"))
    rows.append(nav)
    rows.append([types.InlineKeyboardButton(text="❌ Отмена", callback_data="cmpl_cancel")])

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _categories_keyboard(categories: dict[str, tuple[str, int]]) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура с категориями жалоб конкретного сервера."""
    rows: list[list[types.InlineKeyboardButton]] = []
    for key, (name, node_id) in categories.items():
        label = COMPLAINT_CATEGORY_LABELS.get(key, name)
        rows.append([types.InlineKeyboardButton(
            text=label,
            callback_data=f"cat_pick:{node_id}:{key}",
        )])
    rows.append([types.InlineKeyboardButton(text="◀️ К серверам", callback_data="srv_back")])
    rows.append([types.InlineKeyboardButton(text="❌ Отмена", callback_data="cmpl_cancel")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


# ---------------- Старт сценария ----------------

@router.message(Command("new_complaint"))
@router.message(F.text == "📝 Подать жалобу")
async def start_complaint_flow(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return

    logger.info("Пользователь %s запустил сценарий подачи жалобы.",
                describe_user(message.from_user))

    # Подбираем свободный аккаунт (не в кулдауне) и автоматически
    # делаем его активным.
    account, err = await _pick_account_for_complaint(message.from_user.id)
    if err:
        await message.answer(err)
        return
    await state.update_data(complaint_account_id=account["id"],
                             complaint_account_name=account["username"])

    servers = await get_servers()
    if not servers:
        logger.warning("Сценарий жалобы прерван: список серверов пуст. Нужна синхронизация.")
        await message.answer(
            "⚠️ Список серверов пуст. Сначала выполните <b>🔄 Синхронизировать форум</b> "
            "или отправьте команду <code>/sync</code>."
        )
        return

    # Порядок берём как на форуме (RED → ASTANA), без алфавитной сортировки
    await state.update_data(servers_list=servers)
    await state.set_state(ComplaintForm.choosing_server)
    logger.debug("FSM -> choosing_server (доступно %d серверов).", len(servers))

    await message.answer(
        "📥 <b>Шаг 1: Выберите сервер</b>",
        reply_markup=_servers_keyboard(servers, page=0),
    )


# ---------------- Выбор сервера ----------------

@router.callback_query(ComplaintForm.choosing_server, F.data == "srv_noop")
async def srv_noop(call: types.CallbackQuery):
    await call.answer()


@router.callback_query(ComplaintForm.choosing_server, F.data.startswith("srv_page:"))
async def srv_page(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return

    page = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    servers_list = data.get("servers_list", [])
    await call.message.edit_reply_markup(reply_markup=_servers_keyboard(servers_list, page=page))
    await call.answer()


@router.callback_query(ComplaintForm.choosing_server, F.data.startswith("srv_pick:"))
async def srv_pick(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return

    try:
        node_id = int(call.data.split(":", 1)[1])
    except (ValueError, AttributeError, IndexError):
        logger.warning("srv_pick: некорректный callback data %r", call.data)
        await call.answer("Ошибка данных кнопки.", show_alert=True)
        return

    # Имя сервера берём из state (в callback_data его больше нет — лимит 64Б)
    data = await state.get_data()
    servers_list = data.get("servers_list", [])
    name = next(
        (entry[0] for entry in servers_list
         if len(entry) >= 2 and entry[1] == node_id),
        None,
    )
    if name is None:
        logger.warning("srv_pick: node_id=%s не найден в servers_list.", node_id)
        await call.answer("Сервер не найден, начните заново.", show_alert=True)
        return

    categories = await get_complaint_categories(node_id)
    if not categories:
        logger.warning("Пользователь %s выбрал сервер «%s» (node=%s), но категории не найдены.",
                       describe_user(call.from_user), name, node_id)
        await call.answer("На сервере не найдено категорий жалоб.", show_alert=True)
        return

    await state.update_data(server_node_id=node_id, server_name=name)
    await state.set_state(ComplaintForm.choosing_category)
    logger.info("Пользователь %s выбрал сервер «%s» (node=%s, %d категорий).",
                describe_user(call.from_user), name, node_id, len(categories))

    await call.message.edit_text(
        f"📥 <b>Шаг 2: Сервер <code>{escape(name)}</code></b> — выберите тип жалобы:",
        reply_markup=_categories_keyboard(categories),
    )
    await call.answer()


@router.callback_query(ComplaintForm.choosing_category, F.data == "srv_back")
async def srv_back(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return

    data = await state.get_data()
    servers_list = data.get("servers_list", [])
    await state.set_state(ComplaintForm.choosing_server)
    await call.message.edit_text(
        "📥 <b>Шаг 1: Выберите сервер</b>",
        reply_markup=_servers_keyboard(servers_list, page=0),
    )
    await call.answer()


# ---------------- Выбор категории ----------------

def _templates_keyboard(builtin: dict[str, dict[str, str]],
                         user_templates: list[dict]) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура с шаблонами + 'Своё описание' + 'Создать шаблон'."""
    rows: list[list[types.InlineKeyboardButton]] = []
    # Встроенные шаблоны — по 2 в ряд
    builtin_items = list(builtin.items())
    for i in range(0, len(builtin_items), 2):
        row = []
        for key, info in builtin_items[i:i + 2]:
            row.append(types.InlineKeyboardButton(
                text=info["name"],
                callback_data=f"tpl_use:b:{key}",
            ))
        rows.append(row)
    # Пользовательские шаблоны — по одному в ряд (могут быть длинные имена)
    for ut in user_templates:
        rows.append([types.InlineKeyboardButton(
            text=f"⭐ {ut['name']}",
            callback_data=f"tpl_use:u:{ut['id']}",
        )])
    rows.append([
        types.InlineKeyboardButton(
            text="✍️ Своё описание", callback_data="tpl_skip"),
        types.InlineKeyboardButton(
            text="➕ Создать шаблон", callback_data="tpl_new"),
    ])
    rows.append([
        types.InlineKeyboardButton(text="◀️ К серверам", callback_data="srv_back"),
        types.InlineKeyboardButton(text="❌ Отмена", callback_data="cmpl_cancel"),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(ComplaintForm.choosing_category, F.data.startswith("cat_pick:"))
async def cat_pick(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return

    _, node_id_str, key = call.data.split(":", 2)
    section_id = int(node_id_str)

    data = await state.get_data()
    server_name = data.get("server_name", "?")
    label = COMPLAINT_CATEGORY_LABELS.get(key, key)

    await state.update_data(section_id=section_id, category_key=key, category_label=label)
    logger.info("Пользователь %s выбрал категорию «%s» на сервере «%s» (раздел node=%s).",
                describe_user(call.from_user), key, server_name, section_id)

    # Показываем правила раздела перед заполнением
    rules_text = RULES.get(key)
    if rules_text:
        await call.message.answer(rules_text)

    # Шаблоны есть только для players. Для остальных — сразу к нику.
    builtin = get_builtin_templates(key)
    user_tpls = await list_user_templates(call.from_user.id, key)
    if builtin or user_tpls:
        await state.set_state(ComplaintForm.choosing_template)
        await call.message.edit_text(
            f"✅ Сервер <code>{escape(server_name)}</code> → {escape(label)}",
        )
        await call.message.answer(
            "📋 <b>Шаблон жалобы</b>\n\n"
            "Выберите готовый шаблон или нажмите «Своё описание», "
            "чтобы заполнить вручную.\nПосле выбора шаблона можно будет "
            "дополнить описание.",
            reply_markup=_templates_keyboard(builtin, user_tpls),
        )
    else:
        await state.set_state(ComplaintForm.waiting_for_your_nickname)
        await call.message.edit_text(
            f"✅ Сервер <code>{escape(server_name)}</code> → {escape(label)}",
        )
        await call.message.answer(
            "👤 <b>Шаг 3: Введите Ваш Nick_Name</b> на сервере:",
            reply_markup=_cancel_kb(),
        )
    await call.answer()


# Колбеки выбора шаблона

@router.callback_query(ComplaintForm.choosing_template, F.data.startswith("tpl_use:"))
async def tpl_use(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return

    _, kind, key = call.data.split(":", 2)
    if kind == "b":
        builtin = get_builtin_templates((await state.get_data())["category_key"])
        tpl = builtin.get(key)
        if not tpl:
            await call.answer("Шаблон не найден.", show_alert=True)
            return
        summary = tpl["summary"]
        description = tpl["description"]
        name = tpl["name"]
    else:
        ut = await get_user_template(int(key))
        if not ut or ut["telegram_id"] != call.from_user.id:
            await call.answer("Шаблон не найден.", show_alert=True)
            return
        summary = ut["summary"]
        description = ut["description"]
        name = f"⭐ {ut['name']}"

    await state.update_data(template_summary=summary, template_description=description)
    await state.set_state(ComplaintForm.waiting_for_your_nickname)
    logger.info("Пользователь %s выбрал шаблон «%s» (kind=%s).",
                describe_user(call.from_user), name, kind)

    await call.message.edit_text(
        f"📋 Выбран шаблон: <b>{escape(name)}</b>\n\n"
        f"<i>{escape(description)}</i>"
    )
    await call.message.answer(
        "👤 <b>Шаг 3: Введите Ваш Nick_Name</b> на сервере:",
        reply_markup=_cancel_kb(),
    )
    await call.answer()


@router.callback_query(ComplaintForm.choosing_template, F.data == "tpl_skip")
async def tpl_skip(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    # Никаких заранее заполненных полей — пользователь введёт всё сам
    await state.update_data(template_summary=None, template_description=None)
    await state.set_state(ComplaintForm.waiting_for_your_nickname)
    await call.message.edit_text("✍️ Своё описание — заполните вручную.")
    await call.message.answer(
        "👤 <b>Шаг 3: Введите Ваш Nick_Name</b> на сервере:",
        reply_markup=_cancel_kb(),
    )
    await call.answer()


@router.callback_query(ComplaintForm.choosing_template, F.data == "tpl_new")
async def tpl_new(call: types.CallbackQuery, state: FSMContext):
    """Запускает мини-сценарий создания пользовательского шаблона."""
    if not check_access(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    # Сохраняем категорию из текущего сценария жалобы, чтобы шаблон сохранился
    # с правильной привязкой
    await state.update_data(_creating_template_category=data.get("category_key"))
    await state.set_state(TemplateForm.waiting_for_name)
    await call.message.edit_text(
        "➕ <b>Создание своего шаблона жалобы</b>\n\n"
        "Введите название (как будет отображаться в кнопке).\n"
        "Пример: <code>🎯 RDM в больнице</code>"
    )
    await call.message.answer("Название шаблона:", reply_markup=_cancel_kb())
    await call.answer()


@router.message(TemplateForm.waiting_for_name)
async def tpl_name(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 50:
        await message.answer("Название должно быть от 1 до 50 символов.",
                             reply_markup=_cancel_kb())
        return
    await state.update_data(_tpl_name=name)
    await state.set_state(TemplateForm.waiting_for_summary)
    await message.answer(
        "Теперь введите <b>краткую суть</b> для заголовка темы "
        "(пример: <code>DM</code>):",
        reply_markup=_cancel_kb(),
    )


@router.message(TemplateForm.waiting_for_summary)
async def tpl_summary(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return
    summary = (message.text or "").strip()
    if not summary or len(summary) > 80 or "\n" in summary:
        await message.answer(
            "Суть — одна строка длиной до 80 символов.",
            reply_markup=_cancel_kb(),
        )
        return
    await state.update_data(_tpl_summary=summary)
    await state.set_state(TemplateForm.waiting_for_description)
    await message.answer(
        "Теперь введите <b>описание нарушения</b> — оно подставится в текст темы. "
        "Это можно будет дополнить при подаче конкретной жалобы.",
        reply_markup=_cancel_kb(),
    )


@router.message(TemplateForm.waiting_for_description)
async def tpl_description(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return
    description = (message.text or "").strip()
    if len(description) < 10 or len(description) > 2000:
        await message.answer(
            "Описание — от 10 до 2000 символов.",
            reply_markup=_cancel_kb(),
        )
        return

    data = await state.get_data()
    cat_key = data.get("_creating_template_category") or "players"
    name = data.get("_tpl_name", "Без имени")
    summary = data.get("_tpl_summary", "")
    tid = await add_user_template(
        telegram_id=message.from_user.id,
        category_key=cat_key,
        name=name,
        summary=summary,
        description=description,
    )

    # Возвращаемся к выбору шаблона — теперь там появится новый
    builtin = get_builtin_templates(cat_key)
    user_tpls = await list_user_templates(message.from_user.id, cat_key)
    await state.set_state(ComplaintForm.choosing_template)
    await message.answer(
        f"✅ Шаблон <b>{escape(name)}</b> сохранён (id={tid}).\n\n"
        "Можете выбрать его из списка ниже:",
        reply_markup=_templates_keyboard(builtin, user_tpls),
    )


# ---------------- Универсальная отмена ----------------

@router.callback_query(F.data == "cmpl_cancel")
async def cb_cancel(call: types.CallbackQuery, state: FSMContext):
    logger.info("Пользователь %s отменил сценарий жалобы (через inline-кнопку).",
                describe_user(call.from_user))
    await state.clear()
    await call.message.edit_text("❌ Сценарий отменён.")
    await call.message.answer("Главное меню:", reply_markup=_menu_for(call.from_user.id))
    await call.answer()


async def _cancel_via_text(message: types.Message, state: FSMContext) -> bool:
    """Если пользователь прислал '❌ Отмена' — выходим из сценария."""
    if message.text and message.text.strip() == "❌ Отмена":
        logger.info("Пользователь %s отменил сценарий жалобы (через текстовую кнопку).",
                    describe_user(message.from_user))
        await state.clear()
        await message.answer("❌ Сценарий отменён.", reply_markup=_menu_for(message.from_user.id))
        return True
    return False


# ---------------- Сбор полей ----------------

@router.message(ComplaintForm.waiting_for_your_nickname)
async def process_your_nickname(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    ok, value = validate_nickname(message.text or "")
    if not ok:
        logger.info("Валидация ника (свой) от %s не прошла: %s",
                    describe_user(message.from_user), value)
        await message.answer(
            f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(your_nickname=value)
    await state.set_state(ComplaintForm.waiting_for_target_nickname)
    logger.debug("Шаг: получен свой ник «%s» от %s.", value, describe_user(message.from_user))

    data = await state.get_data()
    target = TARGET_LABEL.get(data["category_key"], "нарушителя")
    await message.answer(
        f"👤 <b>Шаг 4: Введите Nick_Name {escape(target)}</b>:",
        reply_markup=_cancel_kb(),
    )


@router.message(ComplaintForm.waiting_for_target_nickname)
async def process_target_nickname(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    ok, value = validate_nickname(message.text or "")
    if not ok:
        logger.info("Валидация ника (цель) от %s не прошла: %s",
                    describe_user(message.from_user), value)
        await message.answer(
            f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(target_nickname=value)
    data = await state.get_data()
    key = data["category_key"]
    logger.debug("Шаг: получен ник цели «%s» (категория %s).", value, key)
    await _autosave_draft(state, message.from_user.id)

    if key in NEEDS_DATE:
        await state.set_state(ComplaintForm.waiting_for_punishment_date)
        date_kb = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="➖ Без даты")],
                [types.KeyboardButton(text="❌ Отмена")],
            ],
            resize_keyboard=True,
        )
        await message.answer(
            "📅 <b>Шаг 5: Дата выдачи/получения наказания</b> "
            "(например: <code>15.05.2026 19:30</code>).\n\n"
            "Если дата неизвестна — нажмите <b>«➖ Без даты»</b>: "
            "в жалобе будет прочерк.",
            reply_markup=date_kb,
        )
    else:
        await state.set_state(ComplaintForm.waiting_for_summary)
        await _ask_summary(message, key, state)


async def _ask_summary(message: types.Message, key: str, state: FSMContext):
    """Запрашивает короткую суть для заголовка темы. Если есть шаблон —
    предлагает использовать его суть нажатием кнопки."""
    data = await state.get_data()
    template_summary = data.get("template_summary")

    if key == "appeals":
        prompt = (
            "🏷 <b>Шаг: Причина наказания</b> (короткая фраза для заголовка темы)\n"
            "Например: <code>Массовый DM</code>"
        )
    elif key == "leaders":
        prompt = (
            "🏷 <b>Шаг: Краткая суть жалобы</b> (для заголовка темы)\n"
            "Например: <code>Электронные заявления не проверяются</code>"
        )
    else:
        prompt = (
            "🏷 <b>Краткая суть жалобы</b> (для заголовка темы)\n"
            "Например: <code>DM</code>"
        )

    if template_summary:
        prompt += (
            f"\n\n<i>Из шаблона:</i> <code>{escape(template_summary)}</code>"
            "\nНажмите «✅ Из шаблона», чтобы использовать его."
        )
        kb = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="✅ Из шаблона")],
                [types.KeyboardButton(text="❌ Отмена")],
            ],
            resize_keyboard=True,
        )
    else:
        kb = _cancel_kb()
    await message.answer(prompt, reply_markup=kb)


@router.message(ComplaintForm.waiting_for_punishment_date)
async def process_punishment_date(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    text = (message.text or "").strip()

    # Кнопка «Без даты» — пишем в жалобе прочерк, дата необязательна
    if text == "➖ Без даты":
        value = "—"
    else:
        ok, value = validate_date(text)
        if not ok:
            logger.info("Валидация даты от %s не прошла: %s",
                        describe_user(message.from_user), value)
            await message.answer(
                f"❌ {escape(value)}\n\nПопробуйте ещё раз "
                "или нажмите «➖ Без даты»:",
                reply_markup=types.ReplyKeyboardMarkup(
                    keyboard=[
                        [types.KeyboardButton(text="➖ Без даты")],
                        [types.KeyboardButton(text="❌ Отмена")],
                    ],
                    resize_keyboard=True,
                ),
            )
            return

    await state.update_data(punishment_date=value)
    data = await state.get_data()
    logger.debug("Шаг: получена дата наказания «%s».", value)
    await state.set_state(ComplaintForm.waiting_for_summary)
    await _ask_summary(message, data["category_key"], state)


@router.message(ComplaintForm.waiting_for_summary)
async def process_summary(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    text = (message.text or "").strip()
    data = await state.get_data()

    # Кнопка "Из шаблона" — берём заранее заготовленную суть
    if text == "✅ Из шаблона" and data.get("template_summary"):
        value = data["template_summary"]
    else:
        ok, value = validate_summary(text)
        if not ok:
            logger.info("Валидация сути от %s не прошла: %s",
                        describe_user(message.from_user), value)
            await message.answer(
                f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                reply_markup=_cancel_kb(),
            )
            return

    await state.update_data(summary=value)
    await state.set_state(ComplaintForm.waiting_for_description)
    logger.debug("Шаг: получена краткая суть «%s».", value)
    await _autosave_draft(state, message.from_user.id)

    template_description = data.get("template_description")
    if data["category_key"] == "appeals":
        prompt = (
            "📝 <b>Подробное описание ситуации</b>\n"
            "Опишите, за что было выдано наказание и почему оно несправедливо."
        )
    elif data["category_key"] == "leaders":
        prompt = (
            "📝 <b>Подробное описание</b>\n"
            "Опишите ситуацию максимально подробно и раскрыто."
        )
    else:
        prompt = (
            "📝 <b>Подробное описание нарушения</b>\n"
            "Опишите, что именно произошло."
        )

    if template_description:
        prompt += (
            f"\n\n<i>Из шаблона (можно использовать как есть, "
            f"нажав кнопку, или ввести своё):</i>\n"
            f"<blockquote>{escape(template_description)}</blockquote>"
        )
        kb = types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="✅ Использовать шаблон")],
                [types.KeyboardButton(text="❌ Отмена")],
            ],
            resize_keyboard=True,
        )
    else:
        kb = _cancel_kb()
    await message.answer(prompt, reply_markup=kb)


@router.message(ComplaintForm.waiting_for_description)
async def process_description(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    text = (message.text or "").strip()
    data = await state.get_data()

    if text == "✅ Использовать шаблон" and data.get("template_description"):
        value = data["template_description"]
    else:
        ok, value = validate_description(text)
        if not ok:
            logger.info("Валидация описания от %s не прошла: %s",
                        describe_user(message.from_user), value)
            await message.answer(
                f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                reply_markup=_cancel_kb(),
            )
            return

    await state.update_data(description=value)
    await state.set_state(ComplaintForm.waiting_for_proof)
    logger.debug("Шаг: получено описание (%d симв.).", len(value))
    await _autosave_draft(state, message.from_user.id)

    from src.uploader import has_uploader
    if has_uploader():
        proof_hint = (
            "🔗 <b>Доказательства</b>\n\n"
            "📸 <b>Можно прислать скриншот картинкой</b> — бот сам зальёт его "
            "на imgbb.com и подставит ссылку.\n\n"
            "🎥 <b>Можно прислать видео</b> — бот сам зальёт его на Catbox (до 20 МБ).\n\n"
            "Либо вставьте ссылки на YouTube/Imgur/Yapix через пробел или запятую.\n\n"
            "<i>Загрузка в ВКонтакте/Одноклассники запрещена правилами форума.</i>"
        )
    else:
        proof_hint = (
            "🔗 <b>Доказательства</b> (ссылки на YouTube/Imgur/Yapix и т.д. через пробел или запятую):\n\n"
            "🎥 <b>Можно прислать видео прямо в бота</b> — он зальёт его на Catbox (до 20 МБ).\n\n"
            "<i>Загрузка в ВКонтакте/Одноклассники запрещена правилами форума.</i>"
        )
    await message.answer(proof_hint, reply_markup=_cancel_kb())


@router.message(ComplaintForm.waiting_for_proof, F.photo)
async def process_proof_photo(message: types.Message, state: FSMContext, bot: Bot):
    """Принимает скриншот, автозагружает на imgbb и подставляет ссылку."""
    if not check_access(message.from_user.id):
        return

    from src.uploader import has_uploader, upload_image
    if not has_uploader():
        await message.answer(
            "📷 Картинки автозагружаются только если задан "
            "<code>IMGBB_API_KEY</code>. Сейчас он не настроен — "
            "пришлите ссылку текстом.",
            reply_markup=_cancel_kb(),
        )
        return

    status_msg = await message.answer("⏳ Загружаю скриншот на imgbb...")

    # Защита от больших файлов: лучше проверить размер ДО скачивания
    largest = message.photo[-1]
    if largest.file_size and largest.file_size > 32 * 1024 * 1024:
        await status_msg.edit_text(
            "❌ Файл больше 32 МБ — imgbb не примет. "
            "Пришлите файл меньше или вставьте ссылку текстом."
        )
        return

    try:
        file_id = largest.file_id
        file_info = await bot.get_file(file_id)
        file_bytes_io = await bot.download_file(file_info.file_path)
        image_bytes = file_bytes_io.read()
    except Exception as e:
        logger.exception("Не удалось скачать фото из Telegram: %s", e)
        await status_msg.edit_text(
            "❌ Не удалось скачать скриншот из Telegram. "
            "Попробуйте ещё раз или пришлите ссылку текстом."
        )
        return

    if len(image_bytes) > 32 * 1024 * 1024:
        await status_msg.edit_text(
            "❌ Файл больше 32 МБ — imgbb не примет. "
            "Пришлите файл меньше или вставьте ссылку текстом."
        )
        return

    # Проверка на NSFW/gore/экстремистскую символику
    from src.moderation import check_image, has_moderation
    if has_moderation():
        await status_msg.edit_text("⏳ Проверяю содержимое скриншота...")
        allowed, reason = await check_image(image_bytes)
        if not allowed:
            logger.warning(
                "Скриншот от %s отклонён модерацией: %s",
                describe_user(message.from_user), reason,
            )
            await status_msg.edit_text(
                f"🚫 <b>Скриншот не принят:</b> {escape(reason)}.\n\n"
                "Пришлите другой скриншот или вставьте ссылку текстом."
            )
            return
        await status_msg.edit_text("⏳ Загружаю скриншот на imgbb...")

    ok, result = await upload_image(image_bytes, filename=f"proof_{message.from_user.id}.jpg")
    if not ok:
        logger.warning("Загрузка на imgbb не удалась: %s", result)
        await status_msg.edit_text(
            f"❌ <b>Не удалось загрузить скриншот.</b>\n"
            f"<i>{escape(str(result))}</i>\n\n"
            "Попробуйте ещё раз или вставьте ссылку текстом."
        )
        return

    # Сохраняем накопленные ссылки. Если присылают несколько фото подряд —
    # просто склеиваем через пробел.
    data = await state.get_data()
    existing = (data.get("_uploaded_links") or "").strip()
    new_value = (existing + " " + result).strip() if existing else result
    await state.update_data(_uploaded_links=new_value)

    await status_msg.edit_text(
        f"✅ Загружено: <a href=\"{escape(result)}\">{escape(result)}</a>\n\n"
        "Можете прислать ещё скриншот, либо нажмите кнопку «✅ Использовать загруженное» "
        "или пришлите дополнительные ссылки текстом.",
        disable_web_page_preview=True,
    )

    use_kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="✅ Использовать загруженное")],
            [types.KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )
    await message.answer(
        f"<b>Накоплено ссылок:</b> <code>{escape(new_value)}</code>",
        reply_markup=use_kb,
    )


@router.message(ComplaintForm.waiting_for_proof, F.video)
async def process_proof_video(message: types.Message, state: FSMContext, bot: Bot):
    """Принимает видео, скачивает и заливает на Catbox."""
    if not check_access(message.from_user.id):
        return

    from src.uploader import upload_video_catbox
    import os
    import tempfile

    status_msg = await message.answer("⏳ Загружаю видео на сервер (это может занять время)...")

    largest = message.video
    if largest.file_size and largest.file_size > 20 * 1024 * 1024:
        # Telegram bot API limits download to 20MB normally.
        await status_msg.edit_text("❌ Telegram разрешает ботам скачивать файлы только до 20 МБ. Пришлите ссылку текстом.")
        return

    tmp_path = None
    try:
        file_id = largest.file_id
        file_info = await bot.get_file(file_id)
        
        # Скачиваем во временный файл
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp_path = tmp.name
            
        await bot.download_file(file_info.file_path, destination=tmp_path)
    except Exception as e:
        logger.exception("Не удалось скачать видео из Telegram: %s", e)
        await status_msg.edit_text("❌ Не удалось скачать видео из Telegram. Пришлите ссылку текстом.")
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        return

    await status_msg.edit_text("⏳ Видео скачано, загружаю на хостинг Catbox...")

    try:
        ok, result = await upload_video_catbox(tmp_path, filename=f"proof_{message.from_user.id}.mp4")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    if not ok:
        await status_msg.edit_text(
            f"❌ <b>Не удалось загрузить видео.</b>\n"
            f"<i>{escape(str(result))}</i>\n\n"
            "Попробуйте ещё раз или вставьте ссылку текстом."
        )
        return

    data = await state.get_data()
    existing = (data.get("_uploaded_links") or "").strip()
    new_value = (existing + " " + result).strip() if existing else result
    await state.update_data(_uploaded_links=new_value)

    await status_msg.edit_text(
        f"✅ Загружено: <a href=\"{escape(result)}\">{escape(result)}</a>\n\n"
        "Можете прислать ещё файлы, либо нажмите кнопку «✅ Использовать загруженное».",
        disable_web_page_preview=True,
    )

    use_kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="✅ Использовать загруженное")],
            [types.KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )
    await message.answer(
        f"<b>Накоплено ссылок:</b> <code>{escape(new_value)}</code>",
        reply_markup=use_kb,
    )



@router.message(ComplaintForm.waiting_for_proof)
async def process_proof(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    text = (message.text or "").strip()
    data = await state.get_data()

    # Если пользователь нажал кнопку "✅ Использовать загруженное" — берём
    # накопленные ссылки из state
    if text == "✅ Использовать загруженное":
        accumulated = (data.get("_uploaded_links") or "").strip()
        if not accumulated:
            await message.answer(
                "Сначала пришлите хотя бы один скриншот картинкой.",
                reply_markup=_cancel_kb(),
            )
            return
        value = accumulated
    else:
        # Если в state уже накоплены загруженные ссылки — добавим к новым
        accumulated = (data.get("_uploaded_links") or "").strip()
        combined = (accumulated + " " + text).strip() if accumulated else text

        ok, value = validate_proof(combined)
        if not ok:
            logger.info("Валидация доказательств от %s не прошла: %s",
                        describe_user(message.from_user), value)
            await message.answer(
                f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                reply_markup=_cancel_kb(),
            )
            return

    await state.update_data(proof_link=value)
    logger.debug("Шаг: получены доказательства (%d симв.). Готовлю превью.", len(value))

    # Защита от восстановленного черновика, в котором ключи могут отсутствовать
    required = ("category_key", "your_nickname", "target_nickname",
                "description", "summary", "section_id")
    missing = [k for k in required if k not in data or not data.get(k)]
    if missing:
        logger.warning("FSM-данные неполные для %s, не хватает: %s",
                       describe_user(message.from_user), missing)
        await message.answer(
            "⚠️ Похоже, какие-то шаги жалобы пропущены или не сохранились.\n"
            "Начните подачу заново через <b>📝 Подать жалобу</b>.",
            reply_markup=_menu_for(message.from_user.id),
        )
        await state.clear()
        return

    bb_code = build_body(
        category_key=data["category_key"],
        your_nickname=data["your_nickname"],
        target_nickname=data["target_nickname"],
        description=data["description"],
        proof_link=value,
        punishment_date=data.get("punishment_date"),
    )
    thread_title = build_title(data["target_nickname"], data["summary"])

    await state.update_data(bb_code=bb_code, title=thread_title)
    await state.set_state(ComplaintForm.waiting_for_confirm)

    confirm_kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="✅ Отправить на форум"),
            ],
            [
                types.KeyboardButton(text="📦 В очередь"),
                types.KeyboardButton(text="❌ Отмена"),
            ],
        ],
        resize_keyboard=True,
    )

    # Имя аккаунта мы сохранили на старте сценария.
    # Обычным пользователям имя аккаунта-публикатора не показываем.
    poster = data.get("complaint_account_name") or "по текущим cookies.json"
    poster_line_admin = (
        f"👤 <b>От имени:</b> {escape(str(poster))}\n"
        if is_admin(message.from_user.id) else ""
    )

    preview_text = (
        "🧐 <b>Проверьте корректность жалобы перед отправкой:</b>\n\n"
        f"{poster_line_admin}"
        f"📍 <b>Сервер:</b> {escape(str(data.get('server_name', '?')))}\n"
        f"📂 <b>Категория:</b> {escape(str(data.get('category_label', '?')))}\n"
        f"📌 <b>Заголовок темы:</b> {escape(thread_title)}\n\n"
        f"📄 <b>Текст сообщения:</b>\n"
        f"<pre>{escape(bb_code)}</pre>\n"
        "Нажмите <b>✅ Отправить на форум</b> для немедленной публикации "
        "или <b>📦 В очередь</b> чтобы поставить в очередь "
        "(публикация в фоне, когда освободится аккаунт)."
    )
    await message.answer(preview_text, reply_markup=confirm_kb)


@router.message(ComplaintForm.waiting_for_confirm, F.text == "📦 В очередь")
async def process_enqueue(message: types.Message, state: FSMContext):
    """Кладёт жалобу в очередь — фоновый процессор её опубликует, когда
    освободится свободный аккаунт. Полезно когда все аккаунты в кулдауне."""
    if not check_access(message.from_user.id):
        return
    data = await state.get_data()
    # Защита от двойного тапа: между нажатием и state.clear() есть await
    # (запись в БД). Троттлинг пропускает повторное нажатие уже через 0.4с,
    # а aiogram обрабатывает апдейты конкурентно — без этого флага один
    # двойной тап создал бы две записи в очереди.
    if data.get("_publishing"):
        return
    await state.update_data(_publishing=True)
    qid = await enqueue_complaint(
        telegram_id=message.from_user.id,
        section_id=data["section_id"],
        title=data["title"],
        bb_code=data["bb_code"],
        target_nickname=data["target_nickname"],
        description=data["description"],
        proof_link=data["proof_link"],
    )
    await state.clear()
    await message.answer(
        f"📦 <b>Жалоба #{qid} поставлена в очередь.</b>\n\n"
        "Бот опубликует её, когда освободится свободный форумный аккаунт.\n"
        "Вы получите ссылку в личку, как только тема будет опубликована.\n\n"
        "Посмотреть статус: команда <code>/queue</code> (для админа) или "
        "<code>📦 Очередь жалоб</code>.",
        reply_markup=_menu_for(message.from_user.id),
    )


@router.message(ComplaintForm.waiting_for_confirm, F.text == "✅ Отправить на форум")
async def process_confirm(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return

    data = await state.get_data()
    # Защита от двойного тапа «✅ Отправить на форум». Публикация делает
    # несколько сетевых запросов с паузами (до 3 попыток), а state.clear()
    # происходит только в самом конце. Троттлинг пропускает повторное
    # нажатие через 0.4с, и без этого флага две корутины опубликовали бы
    # ДВЕ одинаковые темы на форуме. Флаг ставим атомарно до любых await.
    if data.get("_publishing"):
        return
    await state.update_data(_publishing=True)

    account_id = data.get("complaint_account_id")
    account_name = data.get("complaint_account_name", "?")
    owner_id = account_owner_id(message.from_user.id)

    # Куки берём напрямую из БД — они попадут в post_complaint(cookies=...).
    # set_active_account / apply_account_cookies глобально не трогаем,
    # это исключает race на cookies.json при параллельной работе.

    logger.info(
        "Пользователь %s подтвердил отправку жалобы. "
        "От имени «%s», сервер «%s», категория «%s», цель «%s».",
        describe_user(message.from_user),
        account_name,
        data.get("server_name", "?"),
        data.get("category_key", "?"),
        data.get("target_nickname", "?"),
    )
    status_msg = await message.answer(
        "🚀 Публикую тему на форуме, пожалуйста, подождите...",
        reply_markup=_menu_for(message.from_user.id),
    )

    # Пытаемся отправить с авто-перебором аккаунтов: если первый аккаунт
    # получил 403 / редирект на /login (нет прав в этом разделе или куки
    # протухли), пробуем ещё один из пула. Лимит — 3 попытки.
    #
    # Куки передаются в post_complaint напрямую: это исключает race на
    # cookies.json при параллельной работе нескольких пользователей.
    from src.database import list_accounts as _list_accounts

    success = False
    result: str = ""
    used_account_id = account_id
    used_account_name = account_name
    used_account_cookies: dict | None = None
    if account_id:
        # Берём свежие куки из БД на момент публикации
        acc_full = await get_account(account_id)
        if acc_full and acc_full.get("cookies"):
            used_account_cookies = acc_full["cookies"]

    tried_ids: set[int] = set()
    if account_id:
        tried_ids.add(account_id)

    pool = await _list_accounts(owner_id)
    last_error = ""

    for attempt in range(1, 4):
        if not used_account_cookies:
            logger.warning("Нет куков для аккаунта id=%s — прерываю.",
                           used_account_id)
            success = False
            result = "Не удалось получить куки выбранного аккаунта."
            break

        success, result = await post_complaint(
            section_id=data["section_id"],
            title=data["title"],
            message=data["bb_code"],
            cookies=used_account_cookies,
        )

        if success:
            break

        last_error = str(result)
        logger.warning(
            "Попытка %d отправить жалобу от %s через «%s» (id=%s) "
            "не удалась: %s",
            attempt, describe_user(message.from_user), used_account_name,
            used_account_id, last_error,
        )

        # AUTH-ошибка: куки реально протухли (редирект на /login/) — помечаем
        # аккаунт как нужный перелогин и переключаемся на следующий.
        # NOPERM-ошибка: 403 в разделе. Куки валидны, но нет прав ИЛИ
        # DDoS-Guard. Переключаемся на следующий аккаунт БЕЗ needs_reauth —
        # иначе при первой же 403-ке весь пул сгорит.
        is_auth = is_auth_error(last_error)
        is_noperm = is_noperm_error(last_error)

        if is_auth and used_account_id:
            try:
                await mark_account_needs_reauth(used_account_id)
            except Exception:
                logger.exception("mark_account_needs_reauth failed")
        elif not (is_auth or is_noperm):
            # Не AUTH/NOPERM — нет смысла пробовать другой аккаунт
            # (валидация формы, сетевая ошибка, форум упал и т.п.).
            break

        # Ищем следующий аккаунт из пула, который ещё не пробовали
        next_acc_full = None
        for acc_short in pool:
            if acc_short["id"] in tried_ids:
                continue
            if acc_short.get("needs_reauth"):
                continue
            full = await get_account(acc_short["id"])
            if full and full.get("cookies"):
                next_acc_full = full
                break

        if not next_acc_full:
            logger.info("Аккаунты для перебора закончились — прекращаю ретрай.")
            break

        used_account_id = next_acc_full["id"]
        used_account_name = next_acc_full["username"]
        used_account_cookies = next_acc_full["cookies"]
        tried_ids.add(used_account_id)
        # set_active_account здесь не делаем — это глобальный shared state,
        # который мешает параллельной работе. Аккаунт «активен» только для
        # этой публикации, в скоупе локальной переменной used_account_cookies.
        logger.info("Переключился на «%s» (id=%s) для повторной попытки %d.",
                    used_account_name, used_account_id, attempt + 1)
        try:
            await status_msg.edit_text(
                f"🔁 Попытка {attempt + 1}/3: пробую через "
                f"<b>{escape(used_account_name)}</b>..."
            )
        except Exception:
            pass
        await asyncio.sleep(1.5)

    # Сохраняем итоговый аккаунт в data — он попадёт в БД и кулдаун
    account_id = used_account_id
    account_name = used_account_name

    if success:
        logger.info("Жалоба от %s опубликована. URL: %s",
                    describe_user(message.from_user), result)

        # Кулдаун на использованный аккаунт. Свежие куки в БД не зеркалим:
        # post_complaint(cookies=...) не модифицирует cookies.json, а
        # `load_cookies()` тут вернул бы куки совсем другого аккаунта
        # (того, кто последним вызывал apply_account_cookies из админки).
        # Значит, перезапись в БД старого/чужого набора — реальный риск.
        if account_id:
            await set_account_cooldown(account_id, COMPLAINT_COOLDOWN_SECONDS)

        await add_complaint(
            telegram_id=message.from_user.id,
            nickname=data["target_nickname"],
            description=data["description"],
            proof_link=data["proof_link"],
            forum_thread_url=result,
            account_id=account_id,
            your_nickname=data.get("your_nickname"),
            summary=data.get("summary"),
            category_key=data.get("category_key"),
            punishment_date=data.get("punishment_date"),
            server_node_id=data.get("server_node_id"),
            server_name=data.get("server_name"),
        )
        # Жалоба отправлена — можно удалить черновик
        await delete_draft(message.from_user.id)

        # Подбираем следующий свободный аккаунт — только для информации.
        # Глобальный «активный» в cookies.json больше не переключаем
        # (это глобальный side effect, мешающий параллельной работе).
        next_acc = await find_available_account(owner_id)
        next_info = ""
        if next_acc:
            if next_acc["available"]:
                if is_admin(message.from_user.id):
                    next_info = (
                        f"\n\n🔄 В пуле есть свободный аккаунт "
                        f"<b>{escape(next_acc['username'])}</b> — "
                        f"можно сразу подать следующую жалобу."
                    )
            else:
                rem = _format_cooldown(next_acc["cooldown_remaining_seconds"])
                if is_admin(message.from_user.id):
                    next_info = (
                        f"\n\n⏳ Все аккаунты сейчас в кулдауне.\n"
                        f"Ближайший освободится через <b>{rem}</b> "
                        f"(<b>{escape(next_acc['username'])}</b>)."
                    )
                else:
                    next_info = (
                        f"\n\n⏳ Следующую жалобу можно будет подать "
                        f"через <b>{rem}</b>."
                    )

        await status_msg.delete()
        if is_admin(message.from_user.id):
            final_text = (
                f"🎉 <b>Жалоба успешно опубликована!</b>\n\n"
                f"👤 От имени: <b>{escape(account_name)}</b>\n"
                f"🔗 <a href=\"{escape(result)}\">Открыть тему на форуме</a>\n"
                f"⏱ Аккаунт ушёл в кулдаун на "
                f"<b>{_format_cooldown(COMPLAINT_COOLDOWN_SECONDS)}</b>."
                f"{next_info}"
            )
        else:
            # Обычным пользователям не светим имя аккаунта-публикатора
            final_text = (
                f"🎉 <b>Жалоба успешно опубликована!</b>\n\n"
                f"🔗 <a href=\"{escape(result)}\">Открыть тему на форуме</a>"
                f"{next_info}"
            )
        await message.answer(
            final_text,
            disable_web_page_preview=False,
            message_effect_id=EFFECT_CONFETTI,
        )
    else:
        logger.error("Не удалось опубликовать жалобу от %s. Причина: %s. "
                     "Перебрано аккаунтов: %d.",
                     describe_user(message.from_user), result, len(tried_ids))
        tried_part = ""
        if is_admin(message.from_user.id) and len(tried_ids) > 1:
            tried_part = (
                f"\n\n<i>Перебрано аккаунтов: {len(tried_ids)} — "
                f"ни один не имеет доступа к этому разделу.</i>"
            )
        await status_msg.edit_text(
            f"❌ <b>Не удалось опубликовать жалобу</b>\n\n"
            f"Описание ошибки:\n<code>{escape(str(result))}</code>"
            f"{tried_part}"
        )

    await state.clear()


@router.message(ComplaintForm.waiting_for_confirm, F.text == "❌ Отмена")
async def cancel_confirm(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отправка жалобы отменена.",
                         reply_markup=_menu_for(message.from_user.id))


# ---------------- История жалоб ----------------

def _complaints_list_keyboard(complaints: list[dict]) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура со списком жалоб для управления (открыть детали)."""
    rows: list[list[types.InlineKeyboardButton]] = []
    for c in complaints:
        rows.append([types.InlineKeyboardButton(
            text=f"#{c['id']} — {c['nickname'][:30]}",
            callback_data=f"cmpl_open:{c['id']}",
        )])
    rows.append([types.InlineKeyboardButton(
        text="🔄 Обновить", callback_data="cmpl_refresh",
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _complaint_detail_keyboard(complaint_id: int,
                                 has_thread: bool) -> types.InlineKeyboardMarkup:
    """Кнопки для одной конкретной жалобы."""
    rows: list[list[types.InlineKeyboardButton]] = []
    if has_thread:
        rows.append([types.InlineKeyboardButton(
            text="✏️ Редактировать на форуме",
            callback_data=f"cmpl_edit:{complaint_id}",
        )])
        rows.append([
            types.InlineKeyboardButton(
                text="🗑 Удалить с форума",
                callback_data=f"cmpl_delf:{complaint_id}"),
        ])
    rows.append([types.InlineKeyboardButton(
        text="🗂 Удалить из истории",
        callback_data=f"cmpl_del:{complaint_id}",
    )])
    rows.append([types.InlineKeyboardButton(
        text="◀️ К списку",
        callback_data="cmpl_refresh",
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _format_complaints_list(complaints: list[dict]) -> str:
    if not complaints:
        return "📭 Вы ещё не отправляли жалоб через этого бота."
    lines = ["📜 <b>История ваших жалоб (последние 10):</b>\n"]
    for i, comp in enumerate(complaints[:10], 1):
        if comp["forum_thread_url"]:
            link = (
                f"<a href=\"{escape(comp['forum_thread_url'])}\">тема</a>"
            )
        else:
            link = "<i>не опубликована</i>"
        st_label = status_label(comp.get("status", "pending"))
        # Используем summary (заголовок), а не description, чтобы пользователь
        # видел понятный заголовок жалобы. Для старых жалоб без summary —
        # fallback на description.
        summary_or_desc = comp.get("summary") or comp["description"]
        lines.append(
            f"<b>#{comp['id']}</b> {st_label}\n"
            f"   <b>Цель:</b> {escape(comp['nickname'])}\n"
            f"   <b>Суть:</b> {escape(summary_or_desc[:80])}"
            f"{'…' if len(summary_or_desc) > 80 else ''}\n"
            f"   {link} • <i>{escape(str(comp['created_at']))}</i>"
        )
    lines.append(
        "\n<i>Нажмите на жалобу, чтобы открыть карточку — там доступны "
        "редактирование, удаление с форума и удаление из истории.</i>"
    )
    return "\n\n".join(lines)


@router.message(F.text == "📜 Мои жалобы")
async def show_my_complaints(message: types.Message):
    if not check_access(message.from_user.id):
        return

    complaints = await get_user_complaints(message.from_user.id)
    text = _format_complaints_list(complaints)
    if complaints:
        await message.answer(
            text,
            reply_markup=_complaints_list_keyboard(complaints[:10]),
            disable_web_page_preview=True,
        )
    else:
        await message.answer(text)


@router.callback_query(F.data == "cmpl_refresh")
async def cmpl_refresh(call: types.CallbackQuery):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    complaints = await get_user_complaints(call.from_user.id)
    text = _format_complaints_list(complaints)
    try:
        if complaints:
            await call.message.edit_text(
                text,
                reply_markup=_complaints_list_keyboard(complaints[:10]),
                disable_web_page_preview=True,
            )
        else:
            await call.message.edit_text(text)
    except Exception:
        # Если содержимое не изменилось, Telegram бросает исключение
        pass
    await call.answer("Обновлено")


@router.callback_query(F.data.startswith("cmpl_open:"))
async def cmpl_open(call: types.CallbackQuery):
    """Открывает карточку конкретной жалобы со всеми кнопками."""
    if not check_access(call.from_user.id):
        await call.answer()
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp or comp["telegram_id"] != call.from_user.id:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return

    st_label = status_label(comp.get("status", "pending"))
    if comp["forum_thread_url"]:
        link = f"<a href=\"{escape(comp['forum_thread_url'])}\">Открыть тему</a>"
    else:
        link = "<i>тема не опубликована</i>"

    admin_comment = (comp.get("admin_comment") or "").strip()
    # Если коммент в БД пуст, но тема опубликована — подтянем с форума
    # на лету (старые жалобы могли быть до миграции).
    if comp.get("forum_thread_url") and not admin_comment:
        try:
            from src.forum.xenforo import fetch_thread_admin_comment
            from src.database import (
                update_complaint_admin_comment as _save_comment,
            )
            cookies_to_use = None
            if comp.get("account_id"):
                acc_full = await get_account(comp["account_id"])
                if acc_full and acc_full.get("cookies"):
                    cookies_to_use = acc_full["cookies"]
            fetched = await fetch_thread_admin_comment(
                comp["forum_thread_url"], cookies=cookies_to_use,
            )
            if fetched:
                admin_comment = fetched.strip()
                await _save_comment(comp["id"], admin_comment)
        except Exception:
            logger.debug("on-demand admin_comment failed", exc_info=True)

    comment_block = ""
    if admin_comment:
        snippet = admin_comment if len(admin_comment) <= 800 \
            else admin_comment[:800] + "..."
        comment_block = (
            f"\n<b>💬 Комментарий админа форума:</b>\n"
            f"<blockquote>{escape(snippet)}</blockquote>\n"
        )

    text = (
        f"<b>Жалоба #{comp['id']}</b>  {st_label}\n\n"
        f"<b>Цель:</b> {escape(comp['nickname'])}\n"
        f"<b>Дата:</b> {escape(str(comp['created_at']))}\n"
        f"<b>Тема:</b> {link}\n\n"
        f"<b>Описание:</b>\n<blockquote>{escape(comp['description'])}</blockquote>\n"
        f"<b>Доказательства:</b> <code>{escape(comp['proof_link'])}</code>"
        f"{comment_block}"
    )
    try:
        await call.message.edit_text(
            text,
            reply_markup=_complaint_detail_keyboard(
                cid, has_thread=bool(comp["forum_thread_url"])
            ),
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("cmpl_delf:"))
async def cmpl_delete_from_forum(call: types.CallbackQuery):
    """Удаляет тему на форуме и помечает жалобу удалённой в БД."""
    if not check_access(call.from_user.id):
        await call.answer()
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp or comp["telegram_id"] != call.from_user.id:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return
    if not comp["forum_thread_url"]:
        await call.answer("У жалобы нет ссылки на форум.", show_alert=True)
        return

    # Берём куки того аккаунта, под которым жалоба была подана, и передаём их
    # в delete_thread напрямую — без apply_account_cookies, чтобы не было
    # гонки на глобальном cookies.json при параллельной работе. Если по
    # какой-то причине account_id отсутствует — фолбэк на активный аккаунт.
    owner_id = account_owner_id(call.from_user.id)
    cookies_to_use = None
    if comp.get("account_id"):
        account_used = await get_account(comp["account_id"])
        if account_used and account_used.get("cookies"):
            cookies_to_use = account_used["cookies"]
    if cookies_to_use is None:
        active = await get_active_account(owner_id)
        if active:
            cookies_to_use = active["cookies"]

    await call.answer("⏳ Удаляю тему на форуме...")
    try:
        await call.message.edit_text(
            "⏳ <b>Удаляю тему на форуме...</b>\nПодождите несколько секунд."
        )
    except Exception:
        pass

    success, msg = await delete_thread(comp["forum_thread_url"],
                                         reason="Удалено автором через бота",
                                         cookies=cookies_to_use)

    if success:
        # Тоже удаляем из локальной истории
        await delete_complaint(call.from_user.id, cid)
        logger.info("Жалоба #%s удалена с форума пользователем %s.",
                    cid, describe_user(call.from_user))
        await call.message.edit_text(
            f"🗑 <b>Жалоба #{cid} удалена с форума</b> и из истории.\n\n"
            f"<i>{escape(msg)}</i>"
        )
    else:
        logger.warning("Не удалось удалить жалобу #%s: %s", cid, msg)
        try:
            await call.message.edit_text(
                f"❌ <b>Не удалось удалить тему на форуме.</b>\n\n"
                f"<i>{escape(msg)}</i>\n\n"
                f"Жалоба осталась в истории. Удалите вручную на форуме "
                f"или используйте «🗂 Удалить из истории».",
                reply_markup=_complaint_detail_keyboard(cid, has_thread=True),
                disable_web_page_preview=True,
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("cmpl_del:"))
async def cmpl_del(call: types.CallbackQuery):
    """Удаляет жалобу только из локальной истории. Тема на форуме не трогается."""
    if not check_access(call.from_user.id):
        await call.answer()
        return
    complaint_id = int(call.data.split(":", 1)[1])
    comp = await get_complaint(complaint_id)
    if not comp or comp["telegram_id"] != call.from_user.id:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return
    deleted = await delete_complaint(call.from_user.id, complaint_id)
    if not deleted:
        await call.answer("Не удалось удалить.", show_alert=True)
        return

    complaints = await get_user_complaints(call.from_user.id)
    text = _format_complaints_list(complaints)
    try:
        if complaints:
            await call.message.edit_text(
                text,
                reply_markup=_complaints_list_keyboard(complaints[:10]),
                disable_web_page_preview=True,
            )
        else:
            await call.message.edit_text(text)
    except Exception:
        pass
    await call.answer(f"🗂 Удалена из истории #{complaint_id}", show_alert=False)


# ---------------- Управление пользовательскими шаблонами ----------------

@router.message(Command("templates"))
@router.message(F.text == "📋 Мои шаблоны")
async def cmd_templates(message: types.Message):
    """Показывает пользовательские шаблоны (по всем категориям)."""
    if not check_access(message.from_user.id):
        return
    # Собираем шаблоны из всех категорий
    all_categories = ["players", "admins", "leaders", "appeals"]
    user_tpls: list[dict] = []
    for cat in all_categories:
        for ut in await list_user_templates(message.from_user.id, cat):
            ut["category_key"] = cat
            user_tpls.append(ut)

    if not user_tpls:
        await message.answer(
            "📋 У вас пока нет своих шаблонов.\n\n"
            "Чтобы создать — выберите при подаче жалобы кнопку "
            "<b>➕ Создать шаблон</b>."
        )
        return

    rows: list[list[types.InlineKeyboardButton]] = []
    for ut in user_tpls:
        rows.append([
            types.InlineKeyboardButton(
                text=f"✏️ {ut['name']}",
                callback_data=f"utpl_edit:{ut['id']}",
            ),
            types.InlineKeyboardButton(
                text="🗑",
                callback_data=f"utpl_del:{ut['id']}",
            ),
        ])
    kb = types.InlineKeyboardMarkup(inline_keyboard=rows)

    cat_label = {
        "players": "🎮 игроки", "admins": "🛡 админы",
        "leaders": "👑 лидеры", "appeals": "⚖️ обжалования",
    }
    lines = ["⭐ <b>Ваши шаблоны жалоб:</b>\n"]
    for ut in user_tpls:
        lines.append(
            f"<b>{escape(ut['name'])}</b> "
            f"<i>({cat_label.get(ut.get('category_key'), '?')})</i>\n"
            f"   <i>Суть:</i> <code>{escape(ut['summary'])}</code>\n"
            f"   <i>Описание:</i> {escape(ut['description'][:120])}"
            f"{'…' if len(ut['description']) > 120 else ''}"
        )
    lines.append("\n<i>✏️ — изменить, 🗑 — удалить.</i>")
    await message.answer("\n\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("utpl_del:"))
async def utpl_del(call: types.CallbackQuery):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    tid = int(call.data.split(":", 1)[1])
    ok = await delete_user_template(call.from_user.id, tid)
    if not ok:
        await call.answer("Шаблон не найден.", show_alert=True)
        return

    # Перебираем все категории, как в cmd_templates — иначе при удалении
    # шаблона из admins/leaders/appeals список после обновления был бы пуст
    # или показывал бы только players.
    all_categories = ["players", "admins", "leaders", "appeals"]
    user_tpls: list[dict] = []
    for cat in all_categories:
        for ut in await list_user_templates(call.from_user.id, cat):
            ut["category_key"] = cat
            user_tpls.append(ut)

    if not user_tpls:
        try:
            await call.message.edit_text("📋 У вас не осталось своих шаблонов.")
        except Exception:
            pass
        await call.answer("🗑 Удалён", show_alert=False)
        return

    rows = [[types.InlineKeyboardButton(
        text=f"🗑 {ut['name']}", callback_data=f"utpl_del:{ut['id']}",
    )] for ut in user_tpls]

    cat_label = {
        "players": "🎮 игроки", "admins": "🛡 админы",
        "leaders": "👑 лидеры", "appeals": "⚖️ обжалования",
    }
    lines = ["⭐ <b>Ваши шаблоны жалоб:</b>\n"]
    for ut in user_tpls:
        lines.append(
            f"<b>{escape(ut['name'])}</b> "
            f"<i>({cat_label.get(ut.get('category_key'), '?')})</i>\n"
            f"   <i>Суть:</i> <code>{escape(ut['summary'])}</code>\n"
            f"   <i>Описание:</i> {escape(ut['description'][:120])}"
            f"{'…' if len(ut['description']) > 120 else ''}"
        )
    try:
        await call.message.edit_text(
            "\n\n".join(lines),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception:
        pass
    await call.answer("🗑 Удалён", show_alert=False)


# ---------------- Редактирование пользовательского шаблона ----------------

@router.callback_query(F.data.startswith("utpl_edit:"))
async def utpl_edit_start(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    tid = int(call.data.split(":", 1)[1])
    tpl = await get_user_template(tid)
    if not tpl or tpl["telegram_id"] != call.from_user.id:
        await call.answer("Шаблон не найден.", show_alert=True)
        return

    text = (
        f"✏️ <b>Шаблон «{escape(tpl['name'])}»</b>\n\n"
        f"<b>Имя:</b> {escape(tpl['name'])}\n"
        f"<b>Суть:</b> <code>{escape(tpl['summary'])}</code>\n"
        f"<b>Описание:</b> {escape(tpl['description'][:200])}"
        f"{'…' if len(tpl['description']) > 200 else ''}\n\n"
        "Что меняем?"
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✏️ Имя",
            callback_data=f"utpl_efield:name:{tid}")],
        [types.InlineKeyboardButton(text="✏️ Суть",
            callback_data=f"utpl_efield:summary:{tid}")],
        [types.InlineKeyboardButton(text="✏️ Описание",
            callback_data=f"utpl_efield:description:{tid}")],
        [types.InlineKeyboardButton(text="◀️ Отмена",
            callback_data="utpl_back")],
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "utpl_back")
async def utpl_back(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Отменено")
    try:
        await call.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("utpl_efield:"))
async def utpl_efield(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    try:
        _, field, tid_str = call.data.split(":", 2)
        tid = int(tid_str)
    except (ValueError, AttributeError):
        await call.answer("Ошибка данных кнопки.", show_alert=True)
        return
    if field not in ("name", "summary", "description"):
        await call.answer("Неизвестное поле.", show_alert=True)
        return
    await state.set_state(TemplateEditForm.waiting_for_value)
    await state.update_data(_edit_tpl_id=tid, _edit_tpl_field=field)
    field_label = {
        "name": "имя", "summary": "краткую суть",
        "description": "описание",
    }[field]
    await call.message.answer(
        f"✏️ Введите новое значение для поля <b>{field_label}</b>:",
        reply_markup=_cancel_kb(),
    )
    await call.answer()


@router.message(TemplateEditForm.waiting_for_value)
async def utpl_save_value(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    text = (message.text or "").strip()
    data = await state.get_data()
    tid = data.get("_edit_tpl_id")
    field = data.get("_edit_tpl_field")

    if not tid or field not in ("name", "summary", "description"):
        await state.clear()
        await message.answer("⚠️ Сессия редактирования утеряна.",
                              reply_markup=_menu_for(message.from_user.id))
        return

    # Валидация в зависимости от поля
    if field == "summary":
        ok, value = validate_summary(text)
        if not ok:
            await message.answer(f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                                  reply_markup=_cancel_kb())
            return
    elif field == "description":
        ok, value = validate_description(text)
        if not ok:
            await message.answer(f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                                  reply_markup=_cancel_kb())
            return
    else:  # name
        if len(text) < 1 or len(text) > 60:
            await message.answer("Имя должно быть 1–60 символов.",
                                  reply_markup=_cancel_kb())
            return
        value = text

    success = await update_user_template(
        message.from_user.id, tid, **{field: value},
    )
    await state.clear()
    if success:
        logger.info("Пользователь %s обновил шаблон #%s (поле %s).",
                    describe_user(message.from_user), tid, field)
        await message.answer(
            f"✅ Шаблон обновлён. Поле <b>{field}</b> = "
            f"<code>{escape(value[:120])}</code>",
            reply_markup=_menu_for(message.from_user.id),
        )
    else:
        await message.answer(
            "⚠️ Не удалось обновить шаблон.",
            reply_markup=_menu_for(message.from_user.id),
        )


# ---------------- Редактирование жалобы на форуме ----------------

@router.callback_query(F.data.startswith("cmpl_edit:"))
async def cmpl_edit_start(call: types.CallbackQuery, state: FSMContext):
    """Открывает меню выбора что редактировать."""
    if not check_access(call.from_user.id):
        await call.answer()
        return
    cid = int(call.data.split(":", 1)[1])
    comp = await get_complaint(cid)
    if not comp or comp["telegram_id"] != call.from_user.id:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return
    if not comp["forum_thread_url"]:
        await call.answer("У жалобы нет ссылки на форум.", show_alert=True)
        return

    await state.set_state(EditForm.waiting_for_field)
    await state.update_data(_edit_complaint_id=cid)

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="📝 Описание", callback_data=f"cmpl_efield:desc:{cid}")],
        [types.InlineKeyboardButton(
            text="🔗 Доказательства", callback_data=f"cmpl_efield:proof:{cid}")],
        [types.InlineKeyboardButton(
            text="◀️ Отмена", callback_data=f"cmpl_open:{cid}")],
    ])
    await call.message.edit_text(
        f"✏️ <b>Редактирование жалобы #{cid}</b>\n\n"
        "Что хотите изменить?\n\n"
        "<i>Изменение применится и в локальной истории, "
        "и в теме на форуме (если форум разрешает редактирование автору).</i>",
        reply_markup=kb,
    )
    await call.answer()


@router.callback_query(EditForm.waiting_for_field, F.data.startswith("cmpl_efield:"))
async def cmpl_edit_field(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    _, field, cid_str = call.data.split(":", 2)
    cid = int(cid_str)
    comp = await get_complaint(cid)
    if not comp or comp["telegram_id"] != call.from_user.id:
        await call.answer("Жалоба не найдена.", show_alert=True)
        return

    await state.update_data(_edit_complaint_id=cid, _edit_field=field)

    if field == "desc":
        await state.set_state(EditForm.waiting_for_new_description)
        await call.message.edit_text(
            f"✏️ <b>Жалоба #{cid}</b> — новое описание\n\n"
            f"<b>Текущее:</b>\n<blockquote>{escape(comp['description'])}</blockquote>\n\n"
            "Введите новое описание (10-4000 символов):"
        )
        await call.message.answer(
            "Жду новый текст:", reply_markup=_cancel_kb(),
        )
    elif field == "proof":
        await state.set_state(EditForm.waiting_for_new_proof)
        await call.message.edit_text(
            f"✏️ <b>Жалоба #{cid}</b> — новые доказательства\n\n"
            f"<b>Текущие:</b>\n<code>{escape(comp['proof_link'])}</code>\n\n"
            "Введите новые ссылки:"
        )
        await call.message.answer(
            "Жду ссылки (YouTube/Imgur/Yapix):", reply_markup=_cancel_kb(),
        )
    else:
        await state.clear()
        await call.answer("Неизвестное поле.", show_alert=True)
        return
    await call.answer()


@router.message(EditForm.waiting_for_new_description)
async def cmpl_edit_save_desc(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    ok, value = validate_description(message.text or "")
    if not ok:
        await message.answer(f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                              reply_markup=_cancel_kb())
        return

    await _apply_edit(message, state, new_description=value)


@router.message(EditForm.waiting_for_new_proof)
async def cmpl_edit_save_proof(message: types.Message, state: FSMContext):
    if not check_access(message.from_user.id):
        return
    if await _cancel_via_text(message, state):
        return

    ok, value = validate_proof(message.text or "")
    if not ok:
        await message.answer(f"❌ {escape(value)}\n\nПопробуйте ещё раз:",
                              reply_markup=_cancel_kb())
        return

    await _apply_edit(message, state, new_proof=value)


async def _apply_edit(message: types.Message, state: FSMContext,
                       new_description: str | None = None,
                       new_proof: str | None = None) -> None:
    """Применяет изменение жалобы: обновляет в БД и пересохраняет тело
    темы на форуме (с пересборкой настоящего BB-кода)."""
    data = await state.get_data()
    cid = data.get("_edit_complaint_id")
    if not cid:
        await state.clear()
        return
    comp = await get_complaint(cid)
    if not comp or comp["telegram_id"] != message.from_user.id:
        await state.clear()
        await message.answer("Жалоба не найдена.",
                              reply_markup=_menu_for(message.from_user.id))
        return

    # Обновляем БД
    description = new_description if new_description is not None else comp["description"]
    proof_link = new_proof if new_proof is not None else comp["proof_link"]
    await update_complaint_content(
        cid, message.from_user.id,
        description=description if new_description is not None else None,
        proof_link=proof_link if new_proof is not None else None,
    )

    if not comp["forum_thread_url"]:
        await state.clear()
        await message.answer(
            "✅ Локальная запись обновлена. У жалобы нет ссылки на форум, "
            "поэтому правки только в истории.",
            reply_markup=_menu_for(message.from_user.id),
        )
        return

    # Берём куки именно того аккаунта, под которым жалоба была подана, и
    # передаём их в edit_thread_post напрямую — иначе форум вернёт 403
    # (редактировать может только автор). Без apply_account_cookies, чтобы
    # не было гонки на глобальном cookies.json.
    cookies_to_use = None
    if comp.get("account_id"):
        account_used = await get_account(comp["account_id"])
        if account_used and account_used.get("cookies"):
            cookies_to_use = account_used["cookies"]
    if cookies_to_use is None:
        # Фолбэк: используем активный, но скорее всего форум откажет
        owner_id = account_owner_id(message.from_user.id)
        active = await get_active_account(owner_id)
        if active:
            cookies_to_use = active["cookies"]

    status_msg = await message.answer(
        "⏳ Обновляю тему на форуме...",
        reply_markup=_menu_for(message.from_user.id),
    )

    # Пересобираем настоящий BB-код по сохранённым полям. Если каких-то
    # полей нет в БД (старые жалобы до миграции) — fallback на простой
    # формат, чтобы тема не превращалась в мусор.
    cat_key = comp.get("category_key")
    your_nick = comp.get("your_nickname")
    if cat_key and your_nick:
        new_body = build_body(
            category_key=cat_key,
            your_nickname=your_nick,
            target_nickname=comp["nickname"],
            description=description,
            proof_link=proof_link,
            punishment_date=comp.get("punishment_date"),
        )
    else:
        # Старая жалоба без полей — оставляем человекочитаемый формат,
        # чтобы не уничтожить тему.
        new_body = (
            f"1. Ваш Nick_Name: —\n"
            f"2. Nick_Name цели: [COLOR=rgb(235, 10, 10)]{comp['nickname']}[/COLOR]\n"
            f"3. Суть: {description}\n"
            f"4. Доказательство: {proof_link}"
        )

    # Заголовок при возможности тоже пересобираем
    new_title = None
    if comp.get("summary"):
        new_title = build_title(comp["nickname"], comp["summary"])

    success, msg = await edit_thread_post(
        comp["forum_thread_url"], new_message=new_body,
        new_title=new_title, cookies=cookies_to_use,
    )

    await state.clear()
    if success:
        logger.info("Жалоба #%s отредактирована на форуме пользователем %s.",
                    cid, describe_user(message.from_user))
        try:
            await status_msg.edit_text(
                f"✅ <b>Жалоба #{cid} обновлена</b> и в истории, и на форуме."
            )
        except Exception:
            await message.answer(f"✅ Жалоба #{cid} обновлена.")
    else:
        logger.warning("Не удалось отредактировать жалобу #%s: %s", cid, msg)
        try:
            await status_msg.edit_text(
                f"⚠️ <b>В истории обновлено, но форум отказал.</b>\n\n"
                f"<i>{escape(msg)}</i>\n\n"
                f"Возможно, истёк срок редактирования или нет прав. "
                f"Попробуйте отредактировать вручную."
            )
        except Exception:
            pass


# ---------------- Черновики жалоб ----------------

async def _autosave_draft(state: FSMContext, telegram_id: int) -> None:
    """Сохраняет текущее FSM-состояние как черновик в БД. Игнорирует ошибки."""
    try:
        data = await state.get_data()
        current = await state.get_state()
        await save_draft(telegram_id, data, str(current) if current else None)
    except Exception:
        logger.debug("autosave_draft failed", exc_info=True)


@router.message(Command("draft"))
async def cmd_draft(message: types.Message, state: FSMContext):
    """Показывает черновик жалобы и предлагает продолжить или удалить."""
    if not check_access(message.from_user.id):
        return
    draft = await get_draft(message.from_user.id)
    if not draft:
        await message.answer(
            "📝 У вас нет сохранённого черновика жалобы.\n"
            "Черновик создаётся автоматически при подаче — если случайно "
            "закроете бота на середине, потом сможете продолжить."
        )
        return

    data = draft.get("state_data", {})
    step = draft.get("step", "")
    target = data.get("target_nickname", "—")
    server = data.get("server_name", "—")
    summary = data.get("summary", "—")
    description = (data.get("description", "") or "")[:120]
    your_nick = data.get("your_nickname", "—")

    # Какой следующий шаг — для подсказки
    step_label = "?"
    if "your_nickname" in step:
        step_label = "ввод вашего ника"
    elif "target_nickname" in step:
        step_label = "ввод ника цели"
    elif "punishment_date" in step:
        step_label = "ввод даты наказания"
    elif "summary" in step:
        step_label = "ввод сути"
    elif "description" in step:
        step_label = "ввод описания"
    elif "proof" in step:
        step_label = "ввод доказательств"
    elif "confirm" in step:
        step_label = "подтверждение"
    elif "category" in step:
        step_label = "выбор категории"
    elif "server" in step:
        step_label = "выбор сервера"

    text = (
        f"📝 <b>Сохранён черновик жалобы</b>\n\n"
        f"🕐 Обновлён: <code>{escape(str(draft.get('updated_at', '')))}</code>\n"
        f"📍 Шаг: <i>{escape(step_label)}</i>\n\n"
        f"<b>Сервер:</b> {escape(server)}\n"
        f"<b>Ваш ник:</b> {escape(str(your_nick))}\n"
        f"<b>Цель:</b> {escape(str(target))}\n"
        f"<b>Суть:</b> {escape(str(summary))}\n"
        f"<b>Описание:</b> {escape(description)}{'…' if len(data.get('description', '') or '') > 120 else ''}"
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="📝 Продолжить с этого шага",
            callback_data="draft_resume",
        )],
        [types.InlineKeyboardButton(
            text="🗑 Удалить черновик",
            callback_data="draft_delete",
        )],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "draft_resume")
async def draft_resume(call: types.CallbackQuery, state: FSMContext):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    draft = await get_draft(call.from_user.id)
    if not draft:
        await call.answer("Черновик не найден.", show_alert=True)
        return

    # Восстанавливаем state и переход на сохранённый шаг
    await state.update_data(**draft["state_data"])
    step = draft.get("step", "")
    # Маппинг строки состояния обратно в State
    state_map = {
        "ComplaintForm:choosing_server": ComplaintForm.choosing_server,
        "ComplaintForm:choosing_category": ComplaintForm.choosing_category,
        "ComplaintForm:choosing_template": ComplaintForm.choosing_template,
        "ComplaintForm:waiting_for_your_nickname": ComplaintForm.waiting_for_your_nickname,
        "ComplaintForm:waiting_for_target_nickname": ComplaintForm.waiting_for_target_nickname,
        "ComplaintForm:waiting_for_punishment_date": ComplaintForm.waiting_for_punishment_date,
        "ComplaintForm:waiting_for_summary": ComplaintForm.waiting_for_summary,
        "ComplaintForm:waiting_for_description": ComplaintForm.waiting_for_description,
        "ComplaintForm:waiting_for_proof": ComplaintForm.waiting_for_proof,
        "ComplaintForm:waiting_for_confirm": ComplaintForm.waiting_for_confirm,
    }
    target_state = state_map.get(step)
    if not target_state:
        await call.answer("Не удалось определить шаг черновика.", show_alert=True)
        await delete_draft(call.from_user.id)
        return
    await state.set_state(target_state)
    await call.answer("✅ Восстановлено")
    await call.message.answer(
        "📝 Продолжаем с того же места. Введите данные для текущего шага.",
        reply_markup=_cancel_kb(),
    )


@router.callback_query(F.data == "draft_delete")
async def draft_del(call: types.CallbackQuery):
    if not check_access(call.from_user.id):
        await call.answer()
        return
    await delete_draft(call.from_user.id)
    try:
        await call.message.edit_text("🗑 Черновик удалён.")
    except Exception:
        pass
    await call.answer("Удалено")


# ---------------------------------------------------------------------------
# /find — поиск жалоб по нику
# ---------------------------------------------------------------------------

_FIND_RESULTS_PER_PAGE = 5  # жалоб на одной «странице» результатов


def _find_result_keyboard(
    results: list[dict],
    query: str,
    page: int,
    total: int,
) -> types.InlineKeyboardMarkup:
    """Inline-кнопки для результатов /find: кнопки «Открыть» + пагинация."""
    rows = []
    start = page * _FIND_RESULTS_PER_PAGE
    page_items = results[start: start + _FIND_RESULTS_PER_PAGE]

    for item in page_items:
        url = item.get("forum_thread_url") or ""
        if url:
            rows.append([
                types.InlineKeyboardButton(
                    text=f"🔗 #{item['id']} {escape(item['nickname'][:18])}",
                    url=url,
                )
            ])

    # Пагинация
    total_pages = (total + _FIND_RESULTS_PER_PAGE - 1) // _FIND_RESULTS_PER_PAGE
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(
            text="◀️", callback_data=f"find_page:{query}:{page - 1}",
        ))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton(
            text="▶️", callback_data=f"find_page:{query}:{page + 1}",
        ))
    if nav:
        rows.append(nav)

    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _format_find_results(
    results: list[dict],
    query: str,
    page: int,
    admin_mode: bool,
) -> str:
    """Текст ответа на /find."""
    from src.status_monitor import status_label as slabel
    total = len(results)
    if not total:
        return (
            f"🔍 По запросу <b>{escape(query)}</b> жалоб не найдено.\n\n"
            "<i>Попробуйте изменить запрос (часть ника, без учёта регистра).</i>"
        )

    start = page * _FIND_RESULTS_PER_PAGE
    page_items = results[start: start + _FIND_RESULTS_PER_PAGE]
    total_pages = (total + _FIND_RESULTS_PER_PAGE - 1) // _FIND_RESULTS_PER_PAGE

    header = (
        f"🔍 Найдено <b>{total}</b> жалоб по запросу «<b>{escape(query)}</b>»"
    )
    if admin_mode:
        header += " <i>(все пользователи)</i>"
    header += f"\nСтраница {page + 1}/{total_pages}:\n"

    lines = [header]
    for item in page_items:
        st = slabel(item["status"])
        nick = escape(item["nickname"] or "—")
        date = (item.get("created_at") or "")[:10]
        summary = escape((item.get("summary") or "")[:40])
        url = item.get("forum_thread_url") or ""
        link = f' <a href="{escape(url)}">тема</a>' if url else ""
        author = f" <i>(tg:{item['telegram_id']})</i>" if admin_mode else ""
        lines.append(f"• {st} <b>{nick}</b>{link}{author}\n"
                     f"  <i>{date}</i> — {summary}")

    return "\n".join(lines)


@router.message(Command("find"))
@router.message(F.text.startswith("🔍 Найти жалобу"))
async def cmd_find(message: types.Message):
    """Поиск жалоб по нику цели.

    Синтаксис: /find <ник>   (частичное совпадение, без учёта регистра)
    Обычный пользователь видит только свои жалобы.
    Администратор видит жалобы всех пользователей.
    """
    if not check_access(message.from_user.id):
        return

    # Извлекаем запрос из команды или текста кнопки
    text = message.text or ""
    if text.startswith("/find"):
        query = text[len("/find"):].strip()
    else:
        # Кнопка «🔍 Найти жалобу» — без аргумента, просим ввести
        query = ""

    if not query:
        await message.answer(
            "🔍 <b>Поиск жалоб по нику</b>\n\n"
            "Введите команду с ником цели:\n"
            "<code>/find BlackPlayer</code>\n\n"
            "Поиск частичный и без учёта регистра — "
            "достаточно части ника (<code>/find Black</code>)."
        )
        return

    if len(query) < 2:
        await message.answer("⚠️ Запрос слишком короткий. Введите минимум 2 символа.")
        return

    uid = message.from_user.id
    admin = is_admin(uid)
    tid = None if admin else uid

    results = await search_complaints_by_nick(query, telegram_id=tid, limit=50)
    text_out = _format_find_results(results, query, page=0, admin_mode=admin)
    kb = _find_result_keyboard(results, query, page=0, total=len(results)) if results else None

    await message.answer(text_out, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("find_page:"))
async def find_page(call: types.CallbackQuery):
    """Пагинация результатов /find."""
    if not check_access(call.from_user.id):
        await call.answer()
        return

    _, query, page_str = call.data.split(":", 2)
    page = int(page_str)
    uid = call.from_user.id
    admin = is_admin(uid)
    tid = None if admin else uid

    results = await search_complaints_by_nick(query, telegram_id=tid, limit=50)
    text_out = _format_find_results(results, query, page=page, admin_mode=admin)
    kb = _find_result_keyboard(results, query, page=page, total=len(results))

    try:
        await call.message.edit_text(
            text_out, reply_markup=kb, disable_web_page_preview=True,
        )
    except Exception:
        pass
    await call.answer()
