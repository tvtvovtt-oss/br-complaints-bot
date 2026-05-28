import json
import logging
import time
from html import escape
from aiogram import Router, types, F, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, any_state

from src.config import ADMIN_IDS, COOKIES_PATH
from src.forum.xenforo import (
    check_auth,
    check_auth_for_cookies,
    discover_servers,
    discover_all_complaint_categories,
    invalidate_cookies_cache,
    forum_login,
    forum_submit_2fa,
    apply_account_cookies,
)
from src.database import (
    save_servers,
    save_complaint_categories,
    upsert_account,
    list_accounts,
    set_active_account,
    delete_account,
    get_account,
    get_active_account,
)
from src.logger import describe_user
from src.effects import EFFECT_CONFETTI, EFFECT_FIRE, EFFECT_LIKE

router = Router()
logger = logging.getLogger(__name__)


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь админом.
    Если ADMIN_IDS пуст — все админы (для локальной отладки)."""
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def check_access(user_id: int) -> bool:
    """Проверка базового доступа к боту. Бот публичный — пускаем всех."""
    return True


def account_owner_id(user_id: int) -> int:
    """Telegram_id, чей пул форумных аккаунтов использовать для операций с
    форумом. Админ работает со своими аккаунтами; обычный пользователь —
    с аккаунтами первого админа (общий пул для подачи жалоб).
    """
    if is_admin(user_id):
        return user_id
    if ADMIN_IDS:
        return ADMIN_IDS[0]
    return user_id


def main_menu_keyboard(is_admin_user: bool = False) -> types.ReplyKeyboardMarkup:
    """Главная клавиатура бота. Админу показываем расширенный набор кнопок."""
    if is_admin_user:
        kb = [
            [
                types.KeyboardButton(text="📝 Подать жалобу"),
                types.KeyboardButton(text="📜 Мои жалобы"),
            ],
            [
                types.KeyboardButton(text="📋 Мои шаблоны"),
                types.KeyboardButton(text="📦 Очередь жалоб"),
            ],
            [
                types.KeyboardButton(text="🔒 Режим обслуживания"),
                types.KeyboardButton(text="🐞 Баг-репорты"),
            ],
            [
                types.KeyboardButton(text="🔍 Проверить статус форума"),
                types.KeyboardButton(text="🔄 Синхронизировать форум"),
            ],
            [
                types.KeyboardButton(text="👥 Аккаунты"),
                types.KeyboardButton(text="🔐 Войти по паролю"),
            ],
            [
                types.KeyboardButton(text="📊 Статистика"),
                types.KeyboardButton(text="📢 Рассылка"),
            ],
        ]
    else:
        kb = [
            [
                types.KeyboardButton(text="📝 Подать жалобу"),
                types.KeyboardButton(text="📜 Мои жалобы"),
            ],
            [
                types.KeyboardButton(text="📋 Мои шаблоны"),
                types.KeyboardButton(text="🐞 Сообщить о баге"),
            ],
        ]
    return types.ReplyKeyboardMarkup(
        keyboard=kb,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие...",
    )


# Алиас для обратной совместимости с теми вызовами, что не передают флаг
def _menu_for(user_id: int) -> types.ReplyKeyboardMarkup:
    return main_menu_keyboard(is_admin_user=is_admin(user_id))


# ---------------- Логин по паролю + 2FA ----------------

class LoginForm(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
    waiting_for_2fa_code = State()


def _login_cancel_kb() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


@router.message(Command("login"))
@router.message(F.text == "🔐 Войти по паролю")
async def login_start(message: types.Message, state: FSMContext):
    if await _deny_non_admin(message):
        return
    await _begin_login(message, state, message.from_user)


async def _begin_login(message: types.Message, state: FSMContext,
                        actor: types.User) -> None:
    """Запускает FSM-сценарий входа. Используется и из login_start, и из
    inline-кнопки 'Добавить аккаунт' (в callback нужен другой actor)."""
    logger.info("Пользователь %s запустил вход по паролю.", describe_user(actor))
    await state.set_state(LoginForm.waiting_for_login)
    await message.answer(
        "🔐 <b>Вход на форум по паролю</b>\n\n"
        "Введите ваш логин или email от форума Black Russia.\n"
        "Пароль и логин не сохраняются — используются только для получения куков.",
        reply_markup=_login_cancel_kb(),
    )


async def _login_cancel(message: types.Message, state: FSMContext) -> bool:
    if message.text and message.text.strip() == "❌ Отмена":
        # Если внутри 2FA — закрываем httpx-клиент чтобы не утечь
        data = await state.get_data()
        twofa = data.get("twofa")
        if twofa and twofa.get("client"):
            try:
                await twofa["client"].aclose()
            except Exception:
                pass
        # Затираем все возможные временные пароли из FSM
        await state.update_data(
            _save_password=None,
            _password_temp=None,
            login=None,
        )
        await state.clear()
        await message.answer("❌ Вход отменён.", reply_markup=_menu_for(message.from_user.id))
        return True
    return False


@router.message(LoginForm.waiting_for_login)
async def login_step_login(message: types.Message, state: FSMContext):
    if await _deny_non_admin(message):
        await state.clear()
        return
    if await _login_cancel(message, state):
        return

    await state.update_data(login=(message.text or "").strip())
    await state.set_state(LoginForm.waiting_for_password)
    await message.answer(
        "🔑 Теперь введите <b>пароль</b>.\n"
        "<i>После ввода пароль будет удалён из чата для безопасности.</i>",
        reply_markup=_login_cancel_kb(),
    )


@router.message(LoginForm.waiting_for_password)
async def login_step_password(message: types.Message, state: FSMContext, bot: Bot):
    if await _deny_non_admin(message):
        await state.clear()
        return
    if await _login_cancel(message, state):
        return

    password = message.text or ""
    # Удаляем сообщение с паролем сразу. Если по какой-то причине не вышло —
    # пробуем ещё раз через 0.5 сек (Telegram иногда даёт RetryAfter), и
    # явно ругаемся в лог. Сам пароль в state мы не пишем — только логин.
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as first_err:
        logger.warning("Не удалось удалить сообщение с паролем (1-я попытка): %s",
                       first_err)
        try:
            import asyncio as _aio
            await _aio.sleep(0.5)
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception as second_err:
            logger.error("Не удалось удалить сообщение с паролем (2-я попытка): %s. "
                         "ВНИМАНИЕ: пароль остался в чате — попросите пользователя "
                         "удалить его вручную.", second_err)
            try:
                await message.answer(
                    "⚠️ <b>Не смог автоматически удалить ваш пароль из чата.</b>\n"
                    "Удалите его вручную как можно скорее (тапните по сообщению "
                    "и выберите «Удалить»)."
                )
            except Exception:
                pass

    data = await state.get_data()
    login_value = data.get("login", "")
    status_msg = await message.answer("⏳ Пытаюсь войти на форум...")

    result = await forum_login(login_value, password)

    if result["status"] == "error":
        # Очищаем пароль из памяти state и FSM
        await state.update_data(login=None)
        await state.clear()
        logger.warning("Вход для %s не удался: %s",
                       describe_user(message.from_user), result["message"])
        await status_msg.edit_text(
            f"❌ <b>Не удалось войти.</b>\n\n{escape(result['message'])}"
        )
        await message.answer("Главное меню:", reply_markup=_menu_for(message.from_user.id))
        return

    if result["status"] == "ok":
        username = result["username"]
        account_id = await upsert_account(
            telegram_id=message.from_user.id,
            username=username,
            login=login_value,
            cookies=result["cookies"],
            make_active=True,
        )
        apply_account_cookies(result["cookies"], account_id=account_id)
        logger.info("Вход для %s успешен, аккаунт «%s» сохранён в БД и активирован.",
                    describe_user(message.from_user), username)
        await status_msg.delete()
        # Финальное сообщение (пароль больше не сохраняем — авто-перелогин убран)
        await _offer_save_password(message, state, account_id, username, None)
        return

    # status == "2fa"
    await state.update_data(twofa=result)
    await state.set_state(LoginForm.waiting_for_2fa_code)
    provider = result.get("provider", "email")
    providers = result.get("providers", [provider])

    if provider == "email":
        prompt = (
            "✉️ <b>Двухфакторная авторизация</b>\n\n"
            "Форум отправил <b>код подтверждения на вашу email-почту</b>.\n"
            "Откройте письмо и введите код сюда (обычно 6 цифр)."
        )
    elif provider == "totp":
        prompt = (
            "🔢 <b>Двухфакторная авторизация (TOTP)</b>\n\n"
            "Откройте Google Authenticator / Authy и введите 6-значный код."
        )
    else:
        prompt = (
            f"🔐 <b>Двухфакторная авторизация (провайдер: {escape(provider)})</b>\n\n"
            "Введите код подтверждения."
        )

    if len(providers) > 1:
        others = ", ".join(p for p in providers if p != provider)
        prompt += f"\n\n<i>Другие доступные методы: {escape(others)}</i>"

    await status_msg.delete()
    await message.answer(prompt, reply_markup=_login_cancel_kb())


@router.message(LoginForm.waiting_for_2fa_code)
async def login_step_2fa(message: types.Message, state: FSMContext):
    if await _deny_non_admin(message):
        await state.clear()
        return
    if await _login_cancel(message, state):
        return

    code = (message.text or "").strip()
    if not code:
        await message.answer("Введите код, пожалуйста.")
        return

    data = await state.get_data()
    twofa_state = data.get("twofa")
    if not twofa_state:
        await state.clear()
        await message.answer("⚠️ Сессия 2FA утеряна. Начните заново через /login.",
                             reply_markup=_menu_for(message.from_user.id))
        return

    status_msg = await message.answer("⏳ Проверяю код...")
    result = await forum_submit_2fa(twofa_state, code)

    if result["status"] == "error":
        logger.warning("2FA-код от %s неверен: %s",
                       describe_user(message.from_user), result["message"])
        await status_msg.edit_text(
            f"❌ <b>Код не принят.</b>\n\n{escape(result['message'])}\n\n"
            "Попробуйте ввести код ещё раз или нажмите ❌ Отмена."
        )
        return

    # Успех
    username = result["username"]
    data2 = await state.get_data()
    login_value = (data2.get("twofa") or {}).get("login")  # на всякий случай
    if not login_value:
        # достанем из FSM-данных шага login если был
        login_value = data2.get("login")
    account_id = await upsert_account(
        telegram_id=message.from_user.id,
        username=username,
        login=login_value,
        cookies=result["cookies"],
        make_active=True,
    )
    apply_account_cookies(result["cookies"], account_id=account_id)
    logger.info("Вход с 2FA для %s успешен, аккаунт «%s» сохранён в БД.",
                describe_user(message.from_user), username)
    await status_msg.delete()
    # Финальное сообщение
    await _offer_save_password(message, state, account_id, username, None)


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    admin = is_admin(user_id)
    logger.info("Команда /start от %s (admin=%s).",
                describe_user(message.from_user), admin)

    if admin:
        # Если у админа есть активный аккаунт в БД — применяем его куки.
        active = await get_active_account(user_id)
        if not active:
            if await _try_import_existing_session(user_id):
                active = await get_active_account(user_id)
        if active:
            apply_account_cookies(active["cookies"], account_id=active["id"])
            logger.debug("При /start применены куки активного аккаунта «%s».",
                         active["username"])

        welcome_text = (
            "👋 Привет! Я бот для автоматической подачи жалоб на форум Black Russia.\n\n"
            "🔐 <b>Самый простой способ начать</b> — нажмите <b>«Войти по паролю»</b> "
            "или отправьте <code>/login</code>. Я залогинюсь, при необходимости "
            "приму код 2FA с почты и сохраню сессию.\n\n"
            "Альтернатива — пришлите готовый файл <code>cookies.json</code>.\n\n"
            "После входа выполните <b>🔄 Синхронизировать форум</b>."
        )
    else:
        welcome_text = (
            "👋 Привет! Я бот для автоматической подачи жалоб на форум Black Russia.\n\n"
            "Просто нажмите <b>📝 Подать жалобу</b> и заполните форму — "
            "бот сам опубликует тему на форуме от имени общего аккаунта.\n\n"
            "В <b>📜 Мои жалобы</b> можно посмотреть свою историю."
        )
    # Если есть незаконченный черновик — упомянем
    try:
        from src.database import get_draft as _get_draft
        draft = await _get_draft(user_id)
        if draft:
            welcome_text += (
                "\n\n📝 <i>У вас есть незаконченный черновик жалобы. "
                "Откройте его командой /draft.</i>"
            )
    except Exception:
        pass

    sent = await message.answer(welcome_text, reply_markup=main_menu_keyboard(admin))
    # Закрепляем главное меню чтобы юзер всегда видел его сверху чата
    try:
        await message.bot.pin_chat_message(
            chat_id=message.chat.id,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception:
        # В каналах/группах pin может быть запрещён или уже занят — не критично
        pass

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    admin = is_admin(message.from_user.id)
    if admin:
        help_text = (
            "📖 <b>Справка по боту (админ):</b>\n\n"
            "1. <b>🔐 Войти по паролю</b> или <code>/login</code> — бот сам залогинится "
            "на форум (включая 2FA-код с почты) и сохранит свежие куки.\n"
            "2. <b>👥 Аккаунты</b> или <code>/accounts</code> — список форумных аккаунтов, "
            "переключение между ними, удаление.\n"
            "3. Либо отправьте файл <code>cookies.json</code> в чат — обновит сессию вручную.\n"
            "4. <b>🔄 Синхронизировать форум</b> или <code>/sync</code> — бот находит все "
            "сервера и подразделы жалоб.\n"
            "5. <b>📝 Подать жалобу</b> или <code>/new_complaint</code> — пошаговый сценарий публикации.\n"
            "6. <b>🔍 Проверить статус форума</b> — проверка всех аккаунтов админа.\n"
            "7. <b>📜 Мои жалобы</b> — история отправленных жалоб.\n"
            "8. <b>🐞 Баг-репорты</b> или <code>/bugs</code> — поступившие сообщения от пользователей."
        )
    else:
        help_text = (
            "📖 <b>Справка:</b>\n\n"
            "• <b>📝 Подать жалобу</b> — пошагово заполните форму, бот опубликует тему "
            "на форуме от имени общего аккаунта.\n"
            "• <b>📜 Мои жалобы</b> — ваши прошлые жалобы со ссылками на темы.\n"
            "• <b>📋 Мои шаблоны</b> — личные шаблоны для быстрой подачи.\n"
            "• <b>🐞 Сообщить о баге</b> — если что-то не работает, напишите нам."
        )
    await message.answer(help_text)


async def _deny_non_admin(message: types.Message) -> bool:
    """Если пользователь не админ — пишет отказ и возвращает True."""
    if is_admin(message.from_user.id):
        return False
    logger.info("Не-админ %s попытался вызвать админскую команду.",
                describe_user(message.from_user))
    await message.answer(
        "🔒 Эта функция доступна только администраторам бота.",
        reply_markup=_menu_for(message.from_user.id),
    )
    return True


@router.message(F.text == "🔍 Проверить статус форума")
async def check_forum_status(message: types.Message):
    if await _deny_non_admin(message):
        return

    logger.info("Пользователь %s запросил проверку статуса форума.",
                describe_user(message.from_user))

    accounts = await list_accounts(message.from_user.id)
    # Если в БД пусто — пробуем то что в cookies.json
    if not accounts:
        checking_msg = await message.answer("⏳ Проверяю авторизацию на форуме...")
        success, result = await check_auth()
        if success:
            await checking_msg.delete()
            await message.answer(
                f"✅ Успешно авторизован на форуме!\n👤 Аккаунт: <b>{escape(result)}</b>\n\n"
                "<i>В БД пока нет аккаунтов — добавьте через "
                "🔐 Войти по паролю или 👥 Аккаунты.</i>",
                message_effect_id=EFFECT_FIRE,
            )
        else:
            await checking_msg.edit_text(
                f"❌ <b>Ошибка авторизации.</b>\n\n{result}\n\n"
                "Пришлите свежий <code>cookies.json</code> или войдите через "
                "<b>🔐 Войти по паролю</b>."
            )
        return

    # Параллельно проверяем все аккаунты
    checking_msg = await message.answer(
        f"⏳ Проверяю {len(accounts)} аккаунт(ов) на форуме..."
    )

    import asyncio
    async def _check(acc):
        # достаём куки полной записи через get_account
        full = await get_account(acc["id"])
        cookies = full["cookies"] if full else {}
        ok, name_or_err = await check_auth_for_cookies(cookies)
        return acc, ok, name_or_err

    results = await asyncio.gather(*[_check(a) for a in accounts])

    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(results) - ok_count

    lines = [f"<b>Проверка статуса аккаунтов:</b> ✅ {ok_count} • ❌ {fail_count}\n"]
    for acc, ok, info in results:
        marker_active = " ⭐" if acc["is_active"] else ""
        if ok:
            lines.append(
                f"✅ <b>{escape(acc['username'])}</b>{marker_active} — "
                f"<i>сессия активна</i> (на форуме: <b>{escape(info)}</b>)"
            )
        else:
            # info может содержать HTML с расширенной диагностикой —
            # покажем только первые 120 символов чтобы не раздувать сообщение
            short = info if len(info) < 200 else info[:200].replace("\n", " ") + "…"
            lines.append(
                f"❌ <b>{escape(acc['username'])}</b>{marker_active} — {short}"
            )

    if fail_count:
        lines.append(
            "\n<i>⚠️ Для просроченных аккаунтов обновите куки через "
            "<b>🔐 Войти по паролю</b>.</i>"
        )

    logger.info("Проверка статуса для %s: %d успешно, %d с ошибкой.",
                describe_user(message.from_user), ok_count, fail_count)

    await checking_msg.delete()
    await message.answer(
        "\n".join(lines),
        message_effect_id=EFFECT_FIRE if ok_count > 0 else None,
    )


@router.message(Command("sync"))
@router.message(F.text == "🔄 Синхронизировать форум")
async def sync_forum_structure(message: types.Message):
    """Синхронизирует список серверов и подразделы жалоб для каждого сервера.
    Категории получает параллельно (ускорение в ~6 раз против последовательного обхода)."""
    if await _deny_non_admin(message):
        return

    started = time.monotonic()
    logger.info("Пользователь %s запустил синхронизацию форума.",
                describe_user(message.from_user))
    status = await message.answer("⏳ Сканирую главную страницу форума...")

    ok, servers = await discover_servers()
    if not ok:
        logger.error("Синхронизация прервана на этапе серверов: %s", servers)
        await status.edit_text(f"❌ Не удалось получить серверы: {escape(str(servers))}")
        return

    await save_servers(servers)
    total = len(servers)
    logger.info("Шаг 1/2: получено %d серверов. Запускаю параллельный обход категорий.", total)

    await status.edit_text(
        f"✅ Найдено серверов: {total}\n"
        "⏳ Сканирую подразделы жалоб (параллельно)..."
    )

    failed_servers: list[str] = []
    last_edit = time.monotonic()

    async def progress(done: int, _total: int, _name: str, ok_flag: bool):
        nonlocal last_edit
        if not ok_flag:
            failed_servers.append(_name)
        # Редактируем сообщение не чаще раза в 1.5 секунды и в самом конце,
        # чтобы не упереться в rate limit Telegram при параллельной обработке.
        now = time.monotonic()
        if now - last_edit < 1.5 and done != _total:
            return
        last_edit = now
        try:
            await status.edit_text(
                f"⏳ Синхронизация: {done}/{_total}\n"
                f"✅ Успешно: {done - len(failed_servers)}, ❌ С ошибкой: {len(failed_servers)}"
            )
        except Exception:
            pass

    categories_map = await discover_all_complaint_categories(
        servers, concurrency=3, progress=progress,
    )

    # Пишем результаты в БД
    success_count = 0
    for node_id, cats in categories_map.items():
        await save_complaint_categories(node_id, cats)
        success_count += 1
    fail_count = total - success_count

    elapsed = time.monotonic() - started
    logger.info("Синхронизация завершена за %.1f с. Серверов: %d, успех: %d, без категорий: %d.",
                elapsed, total, success_count, fail_count)
    if failed_servers:
        logger.warning("Серверы без категорий жалоб: %s", ", ".join(failed_servers))

    summary = (
        "🎉 <b>Синхронизация завершена!</b>\n\n"
        f"📊 Всего серверов: {total}\n"
        f"✅ Категории получены: {success_count}\n"
        f"❌ Без категорий: {fail_count}\n"
        f"⏱ Заняло: {elapsed:.1f} с"
    )
    if failed_servers:
        preview = ", ".join(failed_servers[:10])
        if len(failed_servers) > 10:
            preview += f" и ещё {len(failed_servers) - 10}"
        summary += f"\n\n⚠️ Не удалось получить категории для: {escape(preview)}"

    # Эффект "конфетти" — новое сообщение, т.к. edit_text эффекты не поддерживает
    await status.delete()
    await message.answer(summary, message_effect_id=EFFECT_CONFETTI)


@router.message(F.document)
async def handle_cookies_upload(message: types.Message, bot: Bot):
    if await _deny_non_admin(message):
        return

    doc = message.document
    logger.info("Пользователь %s загрузил файл «%s» (размер %s байт).",
                describe_user(message.from_user), doc.file_name, doc.file_size)
    if not doc.file_name.endswith(".json"):
        logger.warning("Загруженный файл не .json: «%s» — отказ.", doc.file_name)
        await message.answer(
            "❌ Пожалуйста, отправьте файл в формате JSON (например, <code>cookies.json</code>)."
        )
        return

    status_msg = await message.answer("⏳ Скачиваю и проверяю файл кук...")

    try:
        # Скачиваем файл в память
        file_info = await bot.get_file(doc.file_id)
        file_bytes = await bot.download_file(file_info.file_path)

        # Проверяем структуру JSON
        content = file_bytes.read().decode("utf-8")
        data = json.loads(content)
        cookies_count = len(data) if isinstance(data, (list, dict)) else 0
        logger.info("Файл успешно прочитан, найдено %s записей.", cookies_count)

        # Нормализуем формат: расширения вроде Cookie-Editor выдают список
        # вида [{"name":..., "value":...}, ...]. В БД нам нужен dict.
        normalized: dict = {}
        if isinstance(data, list):
            for cookie in data:
                if isinstance(cookie, dict) and "name" in cookie and "value" in cookie:
                    normalized[cookie["name"]] = cookie["value"]
        elif isinstance(data, dict):
            normalized = {str(k): str(v) for k, v in data.items()}

        if not normalized:
            await status_msg.edit_text(
                "❌ Файл не содержит распознаваемых кук. Ожидается dict "
                "<code>{\"name\": \"value\"}</code> или список "
                "<code>[{\"name\": ..., \"value\": ...}]</code>."
            )
            return

        # Сохраняем нормализованный dict на диск
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=4, ensure_ascii=False)
        invalidate_cookies_cache()  # сбрасываем кэш, чтобы новый файл прочитался
        logger.info("Куки сохранены в %s (%d записей).",
                    COOKIES_PATH, len(normalized))

        await status_msg.edit_text("💾 Файл сохранён! Проверяю подключение к форуму...")

        # Проверяем авторизацию с новыми куками
        success, result = await check_auth()
        if success:
            logger.info("Новые куки рабочие, аккаунт: «%s».", result)
            # Сохраняем в БД и помечаем активным (для мульти-аккаунтов)
            await upsert_account(
                telegram_id=message.from_user.id,
                username=result,
                login=None,
                cookies=normalized,
                make_active=True,
            )
            await status_msg.delete()
            await message.answer(
                f"✅ Новые куки успешно установлены!\n"
                f"👤 Авторизован как: <b>{escape(result)}</b>\n\n"
                "Аккаунт сохранён в БД и помечен активным.\n"
                "Теперь рекомендую запустить <b>🔄 Синхронизировать форум</b>.",
                message_effect_id=EFFECT_LIKE,
            )
        else:
            logger.warning("Куки сохранены, но форум не принимает сессию: %s", result)
            # result уже содержит HTML-разметку с подробностями
            await status_msg.edit_text(
                "⚠️ Файл сохранён, но форум выдал ошибку:\n\n" + result
            )

    except json.JSONDecodeError as e:
        logger.error("Ошибка разбора JSON в загруженном файле: %s", e)
        await status_msg.edit_text("❌ Ошибка: файл содержит некорректный JSON.")
    except Exception as e:
        logger.exception("Ошибка при сохранении кук")
        await status_msg.edit_text(f"❌ Произошла ошибка при обработке файла: {escape(str(e))}")


# ---------------- Управление форумными аккаунтами ----------------

def _accounts_keyboard(accounts: list[dict]) -> types.InlineKeyboardMarkup:
    """Inline-клавиатура для списка аккаунтов: для каждого две кнопки —
    переключиться (если не активен) и удалить."""
    rows: list[list[types.InlineKeyboardButton]] = []
    for acc in accounts:
        marker = "✅ " if acc["is_active"] else ""
        # Первая строка: имя как заголовок (без действия)
        rows.append([
            types.InlineKeyboardButton(
                text=f"{marker}{acc['username']}",
                callback_data=f"acc_noop:{acc['id']}",
            )
        ])
        # Вторая строка: действия
        actions: list[types.InlineKeyboardButton] = []
        if not acc["is_active"]:
            actions.append(types.InlineKeyboardButton(
                text="↪️ Сделать активным",
                callback_data=f"acc_use:{acc['id']}",
            ))
        actions.append(types.InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"acc_del:{acc['id']}",
        ))
        rows.append(actions)

    rows.append([types.InlineKeyboardButton(
        text="➕ Добавить аккаунт (вход)",
        callback_data="acc_add",
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _format_cooldown_secs(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}с"
    return f"{seconds // 60}м {seconds % 60:02d}с"


def _format_accounts_list(accounts: list[dict]) -> str:
    if not accounts:
        return (
            "👥 <b>Аккаунты форума</b>\n\n"
            "У вас пока нет сохранённых аккаунтов.\n"
            "Нажмите кнопку ниже или <b>🔐 Войти по паролю</b>, чтобы добавить."
        )
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    lines = ["👥 <b>Аккаунты форума</b>\n"]
    for acc in accounts:
        marker = "✅ " if acc["is_active"] else "▫️ "
        login = f" <code>({escape(acc['login'])})</code>" if acc.get("login") else ""

        # Считаем кулдаун
        cd_str = ""
        if acc.get("cooldown_until"):
            try:
                # SQLite хранит как "YYYY-MM-DD HH:MM:SS" в UTC
                cd_dt = datetime.strptime(
                    acc["cooldown_until"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                remaining = (cd_dt - now).total_seconds()
                if remaining > 0:
                    cd_str = f"   ⏳ <b>кулдаун:</b> {_format_cooldown_secs(remaining)}"
            except Exception:
                pass

        lines.append(f"{marker}<b>{escape(acc['username'])}</b> "
                     f"<code>id={acc['id']}</code>{login}")
        if acc.get("needs_reauth"):
            lines.append(
                "   ⚠️ <b>Куки протухли — нужен повторный /login.</b>\n"
                "   <i>Аккаунт временно исключён из пула публикации.</i>"
            )
        if cd_str:
            lines.append(cd_str)
        lines.append(f"   <i>обновлён: {escape(str(acc['updated_at']))}</i>")
    lines.append(
        "\n✅ — активный. ⏳ — кулдаун после публикации жалобы (180с).\n"
        "При подаче жалобы бот сам выберет первый свободный аккаунт.\n"
        "<i>Чтобы проверить статус темы под конкретным аккаунтом:</i>\n"
        "<code>/checkurl https://forum... ID</code>"
    )
    return "\n".join(lines)


@router.message(Command("accounts"))
@router.message(F.text == "👥 Аккаунты")
async def cmd_accounts(message: types.Message):
    if await _deny_non_admin(message):
        return

    # Авто-импорт: если в БД нет аккаунтов, но cookies.json валиден —
    # подтягиваем текущую сессию как первый аккаунт.
    accounts = await list_accounts(message.from_user.id)
    if not accounts:
        await _try_import_existing_session(message.from_user.id)
        accounts = await list_accounts(message.from_user.id)

    logger.info("Пользователь %s открыл список аккаунтов (всего: %d).",
                describe_user(message.from_user), len(accounts))
    await message.answer(
        _format_accounts_list(accounts),
        reply_markup=_accounts_keyboard(accounts),
    )


async def _try_import_existing_session(telegram_id: int) -> bool:
    """Если в cookies.json лежит рабочая сессия, добавляет её как новый
    активный аккаунт. Возвращает True если импорт удался.

    Использует тот же `load_cookies()` что и сам форумный модуль —
    гарантирует, что в БД попадёт ровно тот же словарь кук, который
    проверял check_auth (никаких гонок при одновременной перезаписи).
    """
    if not COOKIES_PATH.exists():
        logger.info("Авто-импорт пропущен: cookies.json не существует.")
        return False

    # Сбрасываем кэш чтобы прочитать свежий файл, и затем берём один и тот
    # же snapshot и для проверки, и для записи в БД.
    invalidate_cookies_cache()
    from src.forum.xenforo import load_cookies
    data = load_cookies()
    if not data:
        logger.info("Авто-импорт пропущен: cookies.json пуст.")
        return False
    if "xf_user" not in data:
        logger.info("Авто-импорт пропущен: в cookies.json нет xf_user. Ключи: %s",
                    ", ".join(data.keys()) or "—")
        return False

    logger.info("Авто-импорт: проверяю сессию для telegram_id=%s ...", telegram_id)
    success, result = await check_auth()
    if not success:
        logger.warning("Авто-импорт отменён: check_auth вернул ошибку: %s",
                       result[:100] if isinstance(result, str) else result)
        return False

    aid = await upsert_account(
        telegram_id=telegram_id,
        username=result,
        login=None,
        cookies=data,
        make_active=True,
    )
    logger.info("Авто-импорт: аккаунт «%s» (id=%s) сохранён как активный.", result, aid)
    return True


@router.callback_query(F.data.startswith("acc_noop:"))
async def acc_noop(call: types.CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("acc_use:"))
async def acc_use(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return

    account_id = int(call.data.split(":", 1)[1])
    account = await get_account(account_id)
    if not account or account["telegram_id"] != call.from_user.id:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return

    ok = await set_active_account(call.from_user.id, account_id)
    if not ok:
        await call.answer("Не удалось переключить.", show_alert=True)
        return

    apply_account_cookies(account["cookies"], account_id=account_id)
    logger.info("Пользователь %s переключился на аккаунт «%s» (id=%s).",
                describe_user(call.from_user), account["username"], account_id)

    accounts = await list_accounts(call.from_user.id)
    try:
        await call.message.edit_text(
            _format_accounts_list(accounts),
            reply_markup=_accounts_keyboard(accounts),
        )
    except Exception:
        # Сообщение могло не измениться (если активен уже был) — не страшно
        pass
    await call.answer(f"✅ Активный аккаунт: {account['username']}", show_alert=False)


@router.callback_query(F.data.startswith("acc_del:"))
async def acc_del(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return

    account_id = int(call.data.split(":", 1)[1])
    account = await get_account(account_id)
    if not account or account["telegram_id"] != call.from_user.id:
        await call.answer("Аккаунт не найден.", show_alert=True)
        return

    deleted = await delete_account(call.from_user.id, account_id)
    if not deleted:
        await call.answer("Не удалось удалить.", show_alert=True)
        return

    # Если после удаления остался активный другой аккаунт — применяем его куки
    new_active = await get_active_account(call.from_user.id)
    if new_active:
        apply_account_cookies(new_active["cookies"], account_id=new_active["id"])
        logger.info("После удаления аккаунта активным стал «%s».",
                    new_active["username"])

    logger.info("Пользователь %s удалил аккаунт «%s» (id=%s).",
                describe_user(call.from_user), account["username"], account_id)

    accounts = await list_accounts(call.from_user.id)
    await call.message.edit_text(
        _format_accounts_list(accounts),
        reply_markup=_accounts_keyboard(accounts),
    )
    await call.answer(f"🗑 Удалён: {account['username']}", show_alert=False)


@router.callback_query(F.data == "acc_add")
async def acc_add(call: types.CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("🔒 Только для админов.", show_alert=True)
        return
    await call.answer()
    # Запускаем сценарий логина от имени реального пользователя (не бота)
    await _begin_login(call.message, state, call.from_user)


# ---------------- Финальное сообщение после входа ----------------

async def _offer_save_password(message: types.Message, state: FSMContext,
                                 account_id: int, username: str,
                                 password: str | None = None) -> None:
    """Финальное сообщение после успешного входа.

    Параметр `password` оставлен для обратной совместимости, но больше
    не используется (фича авто-перелогина удалена).
    """
    # Затираем все возможные следы пароля в FSM (на случай старых черновиков)
    await state.update_data(_save_password=None, _password_temp=None)
    await state.clear()
    await message.answer(
        f"✅ <b>Вход выполнен!</b>\n"
        f"👤 Аккаунт: <b>{escape(username)}</b>\n\n"
        "Аккаунт сохранён и помечен активным.\n\n"
        "Теперь рекомендую <b>🔄 Синхронизировать форум</b>.",
        reply_markup=_menu_for(message.from_user.id),
        message_effect_id=EFFECT_LIKE,
    )


# ---------------- Глобальная отмена ----------------

@router.message(Command("cancel"))
@router.message(StateFilter(any_state), F.text.casefold() == "отмена")
async def global_cancel(message: types.Message, state: FSMContext):
    """Универсальный выход из любого FSM-состояния. Чистит state и
    возвращает в главное меню. Работает даже если пользователь застрял."""
    current_state = await state.get_state()

    # Закрываем httpx-клиент если внутри 2FA-логина
    if current_state and "LoginForm" in str(current_state):
        data = await state.get_data()
        twofa = data.get("twofa")
        if twofa and twofa.get("client"):
            try:
                await twofa["client"].aclose()
            except Exception:
                pass

    if current_state:
        logger.info("Глобальный /cancel от %s — был в состоянии %s.",
                    describe_user(message.from_user), current_state)
        # Затираем все возможные секреты в FSM
        await state.update_data(
            _save_password=None, _password_temp=None, login=None,
        )
        await state.clear()
        await message.answer(
            "❌ Действие отменено. Возвращаюсь в главное меню.",
            reply_markup=_menu_for(message.from_user.id),
        )
    else:
        await message.answer(
            "Вы и так не находитесь ни в каком сценарии 🙂",
            reply_markup=_menu_for(message.from_user.id),
        )


@router.message(Command("me"))
async def cmd_me(message: types.Message):
    """Показывает информацию о пользователе и его статистику."""
    user = message.from_user
    from src.database import get_user_complaints

    complaints = await get_user_complaints(user.id)
    accepted = sum(1 for c in complaints if c["status"] == "accepted")
    rejected = sum(1 for c in complaints if c["status"] == "rejected")
    review = sum(1 for c in complaints if c["status"] == "review")
    pending = sum(1 for c in complaints if c["status"] == "pending")

    role = "👑 Администратор" if is_admin(user.id) else "👤 Пользователь"

    parts = [
        "<b>Ваш профиль</b>\n",
        f"🆔 <code>{user.id}</code>",
        f"👤 {escape(user.full_name or '—')}",
    ]
    if user.username:
        parts.append(f"📛 @{escape(user.username)}")
    parts.append(f"🛡 Роль: {role}")
    parts.append("")
    parts.append("<b>📊 Ваша статистика жалоб:</b>")
    parts.append(f"   📝 Всего: <b>{len(complaints)}</b>")
    parts.append(f"   ⏳ Ожидание: <b>{pending}</b>")
    parts.append(f"   🔎 На рассмотрении: <b>{review}</b>")
    parts.append(f"   ✅ Принято: <b>{accepted}</b>")
    parts.append(f"   ❌ Отклонено: <b>{rejected}</b>")

    if complaints:
        from src.status_monitor import status_label
        parts.append("\n<b>📜 Последние жалобы:</b>")
        for c in complaints[:5]:
            st = status_label(c.get("status", "pending"))
            target = escape((c.get("nickname") or "—")[:30])
            url = c.get("forum_thread_url") or ""
            link = (f' <a href="{escape(url)}">тема</a>'
                    if url else "")
            parts.append(f"   {st} <b>{target}</b>{link}")
        if len(complaints) > 5:
            parts.append(f"\n<i>…ещё {len(complaints) - 5}. Все — в "
                          "<b>📜 Мои жалобы</b>.</i>")

    await message.answer("\n".join(parts), disable_web_page_preview=True)
