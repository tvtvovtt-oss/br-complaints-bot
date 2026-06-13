import asyncio
import json
import logging
import os
import re
import time
from html import escape as escape_html
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from src.config import COOKIES_PATH, FORUM_URL, USER_AGENT

logger = logging.getLogger(__name__)

# Хост форума, вычисленный из FORUM_URL — нужен для установки кук на правильный
# домен. Раньше было захардкожено "forum.blackrussia.online" в нескольких местах.
FORUM_HOST = httpx.URL(FORUM_URL).host

# Регулярка для извлечения node_id из ссылки на форум XenForo
# /forums/some-name.123/  или  /forums/123/  или  /categories/some-name.123/
NODE_ID_RE = re.compile(r"(?:forums|categories)/(?:[^/]+\.)?(\d+)/?")

# node_id «Технического раздела» на форуме Black Russia. Это глобальный
# раздел (не привязан к игровому серверу) с дочерними под-форумами —
# «подразделами». Их список бот получает динамически через
# discover_technical_subsections().
TECHNICAL_SECTION_NODE_ID = 22

# Ключевые слова для распознавания категорий жалоб (lowercase)
COMPLAINT_CATEGORY_KEYWORDS = {
    "players": ("жалобы на игроков", "жалоба на игрока"),
    "admins":  ("жалобы на администрацию", "жалоба на администрацию", "жалобы на админ"),
    "leaders": ("жалобы на лидеров", "жалоба на лидера", "жалобы на лидера"),
    "appeals": ("обжалование наказаний", "обжалование наказания", "обжалования"),
}

# Приоритет проверок: более специфичные классы — раньше "players"
_CATEGORY_PRIORITY = ("admins", "leaders", "appeals", "players")

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Origin": FORUM_URL,
    "Referer": f"{FORUM_URL}/",
}

# Используем lxml, если установлен — он в 5-10 раз быстрее html.parser
try:
    import lxml  # noqa: F401
    _PARSER = "lxml"
    logger.debug("BeautifulSoup будет использовать парсер lxml.")
except ImportError:
    _PARSER = "html.parser"


def _soup(html: str) -> BeautifulSoup:
    """Создаёт BeautifulSoup с лучшим доступным парсером."""
    return BeautifulSoup(html, _PARSER)


# ---------------- Куки ----------------

# Кэш кук в памяти, чтобы не читать файл при каждом запросе.
# Сбрасывается через invalidate_cookies_cache() после загрузки нового cookies.json.
_cookies_cache: Optional[dict] = None
_cookies_mtime: float = 0.0

# id активного аккаунта в БД. Заполняется через apply_account_cookies(...).
# Когда форум обновляет xf_session/xf_csrf и эти свежие куки попадают в
# cookies.json через _persist_cookies_from_client — мы сразу зеркалим их
# и в БД для этого account_id. Иначе после рестарта бот загрузит из БД
# старые (просроченные) куки и упадёт на 403.
_active_account_id: Optional[int] = None
# Защита от гонки при одновременном чтении/записи из разных корутин.
# Используем asyncio.Lock; все обращения к кэшу проходят через него.
_cookies_lock = asyncio.Lock()


def load_cookies(use_cache: bool = True) -> dict:
    """Загрузка кук из cookies.json. Кэширует результат до изменения файла.

    Метод синхронный — берёт snapshot кэша. Запись (save_cookies)
    атомарно обновляет и файл, и кэш под Lock'ом, поэтому коллизий нет.
    """
    global _cookies_cache, _cookies_mtime

    if not COOKIES_PATH.exists():
        logger.warning("Файл с куками не найден по пути: %s", COOKIES_PATH)
        return {}

    try:
        mtime = COOKIES_PATH.stat().st_mtime
    except OSError as e:
        logger.warning("Не удалось получить mtime cookies.json: %s", e)
        mtime = 0.0

    # snapshot кэша без блокировки — для чтения это безопасно (dict-ссылка
    # атомарна в CPython). Если данные стали невалидны — следующий вызов
    # возьмёт новые. Запись идёт под Lock, так что сам dict не пересобирается.
    cached = _cookies_cache
    cached_mtime = _cookies_mtime
    if use_cache and cached is not None and mtime == cached_mtime:
        return dict(cached)  # копия чтобы вызывающий не мог модифицировать кэш

    try:
        with open(COOKIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        cookies_dict: dict = {}
        if isinstance(data, list):
            for cookie in data:
                if "name" in cookie and "value" in cookie:
                    cookies_dict[cookie["name"]] = cookie["value"]
        elif isinstance(data, dict):
            cookies_dict = data

        _cookies_cache = cookies_dict
        _cookies_mtime = mtime
        logger.debug("Загружено %d кук из %s.", len(cookies_dict), COOKIES_PATH.name)
        return dict(cookies_dict)
    except json.JSONDecodeError as e:
        logger.error("Файл cookies.json содержит некорректный JSON: %s", e)
        return {}
    except Exception as e:
        logger.exception("Не удалось прочитать файл с куками: %s", e)
        return {}


def _atomic_write_json(path, data: dict) -> None:
    """Атомарно пишет JSON: сначала во временный файл рядом, затем
    os.replace() — атомарная замена на одной ФС. Так конкурентная запись
    из разных корутин/тасков или падение процесса посреди dump не оставит
    обрезанный/битый cookies.json.
    """
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


async def save_cookies_async(cookies_dict: dict) -> None:
    """Асинхронная запись кук в файл и обновление кэша под Lock."""
    global _cookies_cache, _cookies_mtime
    async with _cookies_lock:
        try:
            _atomic_write_json(COOKIES_PATH, cookies_dict)
            _cookies_cache = dict(cookies_dict)
            _cookies_mtime = COOKIES_PATH.stat().st_mtime
            logger.debug("Куки сохранены в файл (%d записей).", len(cookies_dict))
        except Exception as e:
            logger.exception("Не удалось сохранить куки в файл: %s", e)


def save_cookies(cookies_dict: dict) -> None:
    """Синхронная обёртка над save_cookies_async для обратной совместимости.
    Если вызывается из async-контекста — лучше использовать save_cookies_async.
    """
    global _cookies_cache, _cookies_mtime
    try:
        _atomic_write_json(COOKIES_PATH, cookies_dict)
        _cookies_cache = dict(cookies_dict)
        _cookies_mtime = COOKIES_PATH.stat().st_mtime
        logger.debug("Куки сохранены в файл (%d записей).", len(cookies_dict))
    except Exception as e:
        logger.exception("Не удалось сохранить куки в файл: %s", e)


def invalidate_cookies_cache() -> None:
    """Сбрасывает кэш кук — вызывается после ручной перезаписи cookies.json."""
    global _cookies_cache, _cookies_mtime
    _cookies_cache = None
    _cookies_mtime = 0.0


def apply_account_cookies(cookies: dict, account_id: int | None = None) -> None:
    """Записывает куки в cookies.json и обновляет кэш в памяти.
    Используется при переключении между несколькими форумными аккаунтами —
    после вызова все запросы к форуму пойдут от имени этих кук.

    Если передан `account_id` — запоминаем его, чтобы свежие куки от форума
    автоматически попадали в БД именно для этого аккаунта (через
    `_persist_cookies_from_client`). Это критично: без этого свежие куки
    после публикации остаются только в cookies.json, а при переключении
    аккаунта — теряются.
    """
    global _active_account_id
    save_cookies(cookies)
    # save_cookies сам обновляет кэш, но для надёжности
    invalidate_cookies_cache()
    load_cookies()  # прогреть кэш
    _active_account_id = account_id


def get_active_account_id() -> int | None:
    """Текущий account_id, чьи куки активны в cookies.json. None если
    сессия загружена напрямую (импорт из cookies.json без БД)."""
    return _active_account_id


def _make_client(timeout: float = 20.0) -> httpx.AsyncClient:
    """Создаёт httpx-клиент с куками и общими заголовками. Куки берутся из кэша.

    ВАЖНО: после `async with _make_client() as c: ...` нужно вызвать
    `_persist_cookies_from_client(c)` или использовать обёртку `_session()`,
    чтобы свежие куки от форума попали в cookies.json. Иначе `xf_session`,
    обновлённая форумом во время запросов, потеряется.
    """
    return httpx.AsyncClient(
        cookies=load_cookies(),
        headers=HEADERS,
        follow_redirects=True,
        timeout=timeout,
        http2=False,  # форум стабильно работает по HTTP/1.1, не плодим зависимости
    )


class _session:
    """Async context-manager: открывает клиент, по выходу автоматически
    сливает свежие куки в cookies.json. Использовать вместо _make_client()
    везде, где сессия может быть обновлена форумом.
    """

    def __init__(self, timeout: float = 20.0):
        self._client = _make_client(timeout=timeout)

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        try:
            _persist_cookies_from_client(self._client)
        finally:
            await self._client.aclose()


# ---------------- Авторизация ----------------

def _extract_user_id(html: str) -> int:
    """Извлечение ID авторизованного пользователя из HTML XenForo.
    Использует regex по сырому HTML, потому что BeautifulSoup+lxml на
    нестандартной разметке Black Russia (meta до <html>) может терять
    атрибуты <html> тэга.
    """
    m_html = _HTML_TAG_RE.search(html)
    if not m_html:
        return 0
    attrs = m_html.group(1)
    m_logged = _LOGGED_IN_RE.search(attrs)
    if not m_logged or m_logged.group(1) != "true":
        return 0
    m_uid = _USER_ID_RE.search(attrs)
    if m_uid:
        return int(m_uid.group(1))
    return 1  # logged-in=true, но id не нашёлся — считаем авторизованным


def _extract_username(soup: BeautifulSoup) -> str:
    """Достаёт имя авторизованного пользователя из шапки XenForo."""
    tag = soup.find("span", class_="p-navgroup-linkText")
    if tag and tag.text.strip():
        return tag.text.strip()

    avatar = soup.find("span", class_="avatar")
    if avatar and avatar.parent:
        sibling = avatar.parent.find("span")
        if sibling and sibling.text.strip():
            return sibling.text.strip()

    for a in soup.find_all("a", href=True):
        if "/members/" in a["href"]:
            text = a.text.strip()
            if text:
                return text

    return "Авторизован (Имя не найдено)"


async def _resolve_username(client: httpx.AsyncClient) -> str:
    """Пытается выяснить имя авторизованного пользователя через GET / .
    Если упирается в DDoS-Guard заглушку — обновляет R3ACTLB и пробует ещё раз.
    Возвращает имя или плейсхолдер."""
    try:
        r = await client.get(FORUM_URL)
        r = await _ensure_no_ddos(client, r, FORUM_URL)
        return _extract_username(_soup(r.text))
    except httpx.RequestError as e:
        logger.debug("Не удалось получить имя пользователя: %s", e)
        return "Авторизован"


# ---------------- Авторизация ----------------

LOGIN_URL = f"{FORUM_URL}/login/"
LOGIN_POST_URL = f"{FORUM_URL}/login/login"
TWO_STEP_URL = f"{FORUM_URL}/login/two-step"

# Регулярка для извлечения трёх hex-строк AES (a, b, c) из заглушки DDoS-Guard
_DDOS_KEYS_RE = re.compile(
    r'"([0-9a-f]{32})"\s*,\s*'
    r'"([0-9a-f]{32})"\s*,\s*'
    r'"([0-9a-f]{32})"'
)

# Атрибуты <html> тэга, надёжнее чем через BeautifulSoup, потому что lxml
# может ломаться на нестандартной разметке Black Russia (meta до <html>).
_HTML_TAG_RE = re.compile(r"<html\b([^>]*)>", re.IGNORECASE | re.DOTALL)
_LOGGED_IN_RE = re.compile(r'data-logged-in\s*=\s*"([^"]*)"', re.IGNORECASE)
_USER_ID_RE = re.compile(r'data-user-id\s*=\s*"(\d+)"', re.IGNORECASE)
_CSRF_RE = re.compile(r'data-csrf\s*=\s*"([^"]+)"', re.IGNORECASE)


async def _solve_ddos_guard() -> Optional[str]:
    """Решает JS-челлендж DDoS-Guard и возвращает значение cookie R3ACTLB.
    Возвращает None если страница не оказалась челленджем или решить не вышло.

    Заглушка содержит три 32-символьные hex-строки a, b, c. Cookie вычисляется
    как hex(AES-128-CBC.decrypt(ciphertext=c, key=a, iv=b)).

    ВАЖНО: эта функция делает СВОЙ httpx-запрос. DDoS-Guard может выдать
    другой набор a/b/c для каждой новой сессии (зависит от IP/User-Agent/
    cookie). Поэтому если ты вызывающий уже имеешь HTML challenge-заглушки —
    лучше используй `_solve_from_html(html)` напрямую: ключи будут
    совпадать с твоей сессией.
    """
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, follow_redirects=False, timeout=15.0,
        ) as client:
            r = await client.get(FORUM_URL)
            return _solve_from_html(r.text)
    except httpx.RequestError as e:
        logger.warning("Сетевая ошибка при загрузке заглушки DDoS-Guard: %s", e)
        return None


def _solve_from_html(html: str) -> Optional[str]:
    """Решает challenge на уже полученном HTML (без отдельного запроса).

    Это правильный путь, когда тот же httpx-клиент только что получил
    заглушку: ключи a/b/c из этой заглушки соответствуют сессии клиента,
    итоговый R3ACTLB DDoS-Guard примет.
    """
    if "slowAES" not in html and "vddosw3data" not in html:
        return None
    m = _DDOS_KEYS_RE.search(html)
    if not m:
        # Поднято с DEBUG до WARNING: это решающий признак того, что формат
        # челленджа сменился (наша регулярка из трёх 32-hex строк больше не
        # подходит). Логируем срез страницы вокруг сигнатуры, чтобы увидеть
        # новый формат ключей и не гадать вслепую.
        anchor = html.find("vddosw3data")
        if anchor < 0:
            anchor = html.find("slowAES")
        snippet = html[max(0, anchor - 100): anchor + 400] if anchor >= 0 else html[:400]
        # Сколько вообще длинных hex-строк на странице и какой они длины —
        # покажет, сменилась ли длина ключа (было 32).
        hex_lens = sorted({len(h) for h in re.findall(r'"([0-9a-fA-F]{8,})"', html)})
        logger.warning(
            "DDoS-Guard: AES-ключи не найдены регуляркой (формат челленджа мог "
            "смениться). Длины hex-строк на странице: %s. Срез: %r",
            hex_lens or "—", snippet,
        )
        return None
    a_hex, b_hex, c_hex = m.group(1), m.group(2), m.group(3)

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
    except ImportError:
        logger.warning("Для решателя DDoS-Guard нужен пакет cryptography.")
        return None

    try:
        key = bytes.fromhex(a_hex)
        iv = bytes.fromhex(b_hex)
        ct = bytes.fromhex(c_hex)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv),
                         backend=default_backend())
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ct) + decryptor.finalize()
        return plaintext.hex()
    except Exception as e:
        logger.warning("Не удалось расшифровать DDoS-Guard challenge: %s", e)
        return None


async def _ensure_no_ddos(client: httpx.AsyncClient,
                            response: httpx.Response,
                            url: str,
                            *, max_retries: int = 2,
                            persist_cookie: bool = False) -> httpx.Response:
    """Если в ответе DDoS-Guard заглушка — решает challenge ИЗ ЭТОГО HTML
    (правильные ключи для текущей сессии), ставит R3ACTLB на этого же
    клиента и повторяет запрос. До max_retries раз.

    `persist_cookie=True` — дополнительно сохранит свежий R3ACTLB в
    cookies.json.

    ВАЖНО: между установкой R3ACTLB и retry-запросом ставим небольшую
    задержку. JS-заглушка сама ждёт 5 секунд (`setTimeout(...,5000)`)
    перед редиректом — DDoS-Guard может проверять «не делает ли клиент
    запрос мгновенно», что выдаёт автомат и приводит к HTTP 403 на
    следующем запросе. 5.5 секунд — с запасом.
    """
    DDOS_DELAY = 5.5
    for _ in range(max_retries):
        text = response.text
        # DDoS-Guard может отдавать challenge как с HTTP 200, так и с 403.
        # Поэтому проверяем по сигнатуре HTML, а не по статусу.
        if "vddosw3data" not in text and "slowAES" not in text:
            return response
        fresh = _solve_from_html(text)
        if not fresh:
            return response
        client.cookies.set("R3ACTLB", fresh, domain=FORUM_HOST, path="/")
        if persist_cookie:
            existing = load_cookies()
            save_cookies({**existing, "R3ACTLB": fresh})
        logger.info("DDoS-Guard challenge решён, жду %.1f с перед retry GET %s",
                     DDOS_DELAY, url)
        # Ждём 5+ секунд — иначе DDoS-Guard видит «бот ретраит мгновенно»
        # и на следующем запросе выдаёт 403.
        await asyncio.sleep(DDOS_DELAY)
        try:
            response = await client.get(url)
        except httpx.RequestError as e:
            logger.warning("Ретрай после DDoS-Guard упал: %s", e)
            return response
    return response


async def forum_login(login: str, password: str) -> dict:
    """Логинится на форум по логину/паролю.

    Возвращает один из вариантов:
    - {"status": "ok", "username": str, "cookies": dict}
    - {"status": "2fa", "providers": list[str], "csrf": str, "client": httpx.AsyncClient,
       "provider": str, "two_step_url": str}
       — форум требует код подтверждения; код уже отправлен на email/totp.
       Клиент остаётся открытым, его передаём в forum_submit_2fa() и он же
       закрывается там. Если 2FA отменили — вызовите client.aclose() сами.
    - {"status": "error", "message": str}
    """
    logger.info("Начинаю вход на форум по паролю (логин: %r).", login)

    # Стартуем со старого R3ACTLB из cookies.json если есть. Если нет —
    # запустим клиент без него; warmup-запрос на главную сам получит
    # JS-челлендж, мы решим его НА ТОЙ ЖЕ СЕССИИ. Это критично: если
    # пытаться получить R3ACTLB из отдельного httpx-запроса, ключи a/b/c
    # привязаны к той сессии (IP+TLS-fingerprint), и нашему клиенту они
    # не подойдут — DDoS-Guard ответит 403.
    initial_cookies: dict = {}
    existing = load_cookies()
    if existing.get("R3ACTLB"):
        initial_cookies["R3ACTLB"] = existing["R3ACTLB"]

    client = httpx.AsyncClient(
        cookies=initial_cookies,
        headers=HEADERS, follow_redirects=True, timeout=20.0,
    )

    # При любом раннем return нужно закрыть клиент, чтобы не утекало соединение.
    # Только успешный 2FA-ответ оставляет клиент открытым (его закроет submit_2fa).
    keep_open = False
    try:
        # 0. Прогрев главной — DDoS-Guard либо отдаст реальную страницу
        # (R3ACTLB подходит), либо пришлёт JS-челлендж, который мы решим
        # НА ТОЙ ЖЕ СЕССИИ.
        try:
            warmup = await client.get(FORUM_URL)
        except httpx.RequestError as e:
            logger.warning("Прогрев главной упал: %s", e)
            return {"status": "error",
                    "message": f"Сетевая ошибка при прогреве: {e}"}
        warmup = await _ensure_no_ddos(client, warmup, FORUM_URL,
                                          persist_cookie=True)
        if "vddosw3data.js" in warmup.text or "slowAES" in warmup.text:
            logger.error("DDoS-Guard challenge не разруливается за 2 попытки.")
            return {"status": "error",
                    "message": (
                        "DDoS-Guard форума не пропускает запросы. "
                        "Возможно изменилась защита либо IP в чёрном списке."
                    )}

        # Маленькая пауза — браузер тоже не молниеносно навигирует
        await asyncio.sleep(0.3)

        # 1. Получаем CSRF и стартовые куки с /login/
        # ВАЖНО: DDoS-Guard на BR может отдать challenge с HTTP 403 (а не 200).
        # Поэтому сначала пробуем разрулить challenge на ответе, потом проверяем
        # статус. Если и после этого 403 — значит реальный отказ доступа.
        r = await client.get(LOGIN_URL)
        r = await _ensure_no_ddos(client, r, LOGIN_URL, persist_cookie=True)

        # Если DDoS-Guard всё ещё держит challenge — пробуем альтернативный
        # путь /index.php?login/ — в некоторых конфигурациях BR XenForo
        # этот path не блокируется DDoS-Guard'ом.
        # URL формы входа — может смениться на alt-path если /login/ заблокирован.
        active_login_get_url = LOGIN_URL
        active_login_post_url = LOGIN_POST_URL

        if (r.status_code == 403
                or "vddosw3data" in r.text or "slowAES" in r.text):
            alt_login_url = f"{FORUM_URL}/index.php?login/"
            logger.warning("LOGIN_URL вернул %s — пробую альтернативный path %s",
                            r.status_code, alt_login_url)
            r2 = await client.get(alt_login_url)
            r2 = await _ensure_no_ddos(client, r2, alt_login_url,
                                          persist_cookie=True)
            if r2.status_code == 200 and "vddosw3data" not in r2.text:
                r = r2
                active_login_get_url = alt_login_url
                # POST тоже должен идти на alt-path — иначе DDoS-Guard режет его 403
                active_login_post_url = f"{FORUM_URL}/index.php?login/login"
                logger.info("Альтернативный login path сработал, POST → %s",
                            active_login_post_url)

        if r.status_code != 200:
            logger.error("HTTP %s при загрузке страницы входа", r.status_code)
            return {"status": "error",
                    "message": (
                        f"Форум вернул HTTP {r.status_code} на странице входа.\n\n"
                        "Скорее всего IP сервера в чёрном списке DDoS-Guard "
                        "(или временный rate-limit). Подождите 30-60 минут "
                        "или экспортируйте свежий <code>cookies.json</code> "
                        "из браузера и пришлите боту."
                    )}

        if "vddosw3data.js" in r.text or "slowAES" in r.text:
            logger.error("DDoS-Guard заглушка остаётся даже после нового R3ACTLB.")
            return {"status": "error",
                    "message": (
                        "Форум упорно показывает DDoS-Guard защиту. "
                        "Возможно, сменился алгоритм или ваш IP в чёрном списке. "
                        "Попробуйте экспортировать <code>cookies.json</code> "
                        "из браузера и прислать боту."
                    )}

        csrf = _extract_csrf(r.text)
        if not csrf:
            logger.error("CSRF-токен не найден на %s.", active_login_get_url)
            return {"status": "error", "message": "Не удалось получить CSRF-токен формы входа."}

        # 2. Отправляем форму входа
        _post_uri = active_login_post_url.replace(FORUM_URL, "") or "/login/login"
        payload = {
            "login": login,
            "password": password,
            "remember": "1",
            "register": "0",
            "_xfToken": csrf,
            "_xfRedirect": f"{FORUM_URL}/",
            "_xfRequestUri": _post_uri,
            "_xfWithData": "1",
            "_xfResponseType": "json",
        }
        ajax = {**HEADERS, "X-Requested-With": "XMLHttpRequest",
                "Referer": active_login_get_url}
        r2 = await client.post(active_login_post_url, data=payload, headers=ajax)
        logger.debug("POST %s -> HTTP %s, content-type %s",
                     active_login_post_url, r2.status_code,
                     r2.headers.get("content-type"))

        try:
            resp_json = r2.json()
        except (ValueError, json.JSONDecodeError):
            resp_json = None

        if resp_json:
            errors = resp_json.get("errors")
            if errors:
                msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
                logger.warning("Форум вернул ошибку входа: %s", msg)
                return {"status": "error", "message": msg}

            html_content = (resp_json.get("html") or {}).get("content", "")
            redirect_url = resp_json.get("redirect", "")

            # Иногда XenForo на ошибочный логин/капчу/блок не пишет в errors,
            # а кладёт текст в html.content внутри блока .blockMessage--error.
            # Без этого парсинга мы возвращаем гипотетическое «неверный
            # логин» хотя на самом деле форум сказал что-то конкретное.
            if html_content and not redirect_url:
                err_soup = _soup(html_content)
                err_block = err_soup.find(class_=re.compile(
                    r"blockMessage.*error|input-error|formRow-explain.*error"
                ))
                if err_block:
                    err_text = err_block.get_text(" ", strip=True)
                    if err_text:
                        logger.warning("Форум вернул ошибку (HTML-блок): %s",
                                        err_text)
                        return {"status": "error", "message": err_text}

            if "two-step" in redirect_url or "two-step" in html_content:
                # Загружаем страницу 2FA в этом же клиенте, оставляем его открытым
                two_step_data = await _start_two_step(client, redirect_url, html_content)
                if two_step_data["status"] == "2fa":
                    keep_open = True
                    two_step_data["client"] = client
                return two_step_data

            if redirect_url:
                redirect_abs = _to_abs(redirect_url)
                final = await client.get(redirect_abs)
                final = await _ensure_no_ddos(client, final, redirect_abs,
                                                 persist_cookie=True)
                if _extract_user_id(final.text):
                    username = _extract_username(_soup(final.text))
                    logger.info("Вход успешен (без 2FA): «%s».", username)
                    return {"status": "ok", "username": username,
                            "cookies": _flatten_cookies(client)}

        # Фолбэк: проверим главную форума на data-logged-in
        r3 = await client.get(FORUM_URL)
        r3 = await _ensure_no_ddos(client, r3, FORUM_URL, persist_cookie=True)
        if _extract_user_id(r3.text):
            username = _extract_username(_soup(r3.text))
            logger.info("Вход успешен (фолбэк): «%s».", username)
            return {"status": "ok", "username": username,
                    "cookies": _flatten_cookies(client)}

        # Если URL после редиректа содержит two-step — 2FA требуется
        if "two-step" in str(r2.url):
            two_step_data = await _start_two_step(client, str(r2.url), r2.text)
            if two_step_data["status"] == "2fa":
                keep_open = True
                two_step_data["client"] = client
            return two_step_data

        # Логируем что именно прислал форум — чтобы понять причину
        if resp_json is not None:
            logger.warning("Вход не удался. JSON-ответ форума: %r",
                           str(resp_json)[:600])
        else:
            preview = r2.text[:600].replace("\n", " ")
            logger.warning("Вход не удался. Не-JSON ответ HTTP %s: %s",
                           r2.status_code, preview)
        return {"status": "error",
                "message": (
                    "Форум не пустил (но и явной ошибки не вернул).\n\n"
                    "Возможные причины:\n"
                    "• Неверный логин или пароль (проверьте раскладку и пробелы).\n"
                    "• Форум требует капчу или действие в браузере "
                    "(тогда экспортируйте <code>cookies.json</code>).\n"
                    "• Свежие подробности — в логах сервера."
                )}

    except httpx.RequestError as e:
        logger.error("Сетевая ошибка при входе: %s", e)
        return {"status": "error", "message": f"Ошибка сети: {e}"}
    except Exception as e:
        logger.exception("Непредвиденная ошибка входа на форум")
        return {"status": "error", "message": f"Ошибка: {e}"}
    finally:
        if not keep_open:
            await client.aclose()


def _to_abs(url: str) -> str:
    """Превращает относительный URL форума в абсолютный."""
    if not url:
        return FORUM_URL
    if url.startswith("http"):
        return url
    return FORUM_URL + (url if url.startswith("/") else "/" + url)


def _flatten_cookies(client: httpx.AsyncClient) -> dict:
    """Возвращает {name: value} для домена форума.

    Если на одно имя в jar лежат несколько записей (XenForo при логине
    может прислать два Set-Cookie: 'xf_session=deleted' и затем настоящую
    новую сессию), берём ПОСЛЕДНЮЮ непустую — это самая свежая.
    Также игнорируем явно "удалённые" значения вроде 'deleted' или пустой строки.
    """
    forum_host = httpx.URL(FORUM_URL).host
    # Собираем все валидные значения по имени, берём последнее
    by_name: dict[str, str] = {}
    for cookie in client.cookies.jar:
        if not cookie.domain or forum_host not in cookie.domain:
            continue
        val = cookie.value
        # Отбрасываем удалённые/пустые куки
        if not val or val == "deleted":
            # Если в jar уже было нормальное значение — не затираем им deleted
            continue
        by_name[cookie.name] = val  # перезапись = последнее значение побеждает
    return by_name


def _persist_cookies_from_client(client: httpx.AsyncClient) -> None:
    """Сливает текущее состояние jar клиента в cookies.json.

    Сохраняет существующие записи, обновляя/добавляя свежие. Безопасно
    вызывать часто — на старые значения просто перезаписывает.

    Зеркалирование в БД для конкретного аккаунта делается явно через
    `update_account_cookies(account_id, cookies)` после успешной операции.
    Здесь мы НЕ зеркалим в БД, потому что параллельные операции с
    разными аккаунтами могли бы записать чужие куки в чужую запись
    (race на глобальной _active_account_id).
    """
    fresh = _flatten_cookies(client)
    if not fresh:
        return
    existing = load_cookies()
    merged = {**existing, **fresh}
    if merged == existing:
        return
    save_cookies(merged)


async def _start_two_step(client: httpx.AsyncClient, redirect_url: str,
                           html_content: str) -> dict:
    """Загружает страницу /login/two-step, парсит форму и возвращает
    данные для последующей отправки кода."""
    target = _to_abs(redirect_url) if redirect_url else TWO_STEP_URL
    logger.info("Форум требует 2FA. Загружаю страницу подтверждения: %s", target)

    page = await client.get(target)
    # DDoS-Guard может на странице 2FA снова отдать challenge — решим
    # на ТОЙ ЖЕ сессии (иначе R3ACTLB не подойдёт и будет HTTP 403).
    page = await _ensure_no_ddos(client, page, target, persist_cookie=True)

    # Если /login/two-step заблокирован DDoS-Guard'ом — пробуем alt-path.
    # Тот же трюк что с /index.php?login/ для страницы входа.
    if page.status_code == 403 or "vddosw3data" in page.text or "slowAES" in page.text:
        # Вычленяем query-string из оригинального URL чтобы не потерять
        # _xfRedirect и remember параметры
        parsed = httpx.URL(target)
        _raw_qs = parsed.query  # bytes в httpx
        _qs_str = (_raw_qs.decode() if isinstance(_raw_qs, bytes) else _raw_qs) or ""
        # КРИТИЧНО: в форме index.php?ROUTE параметры присоединяются через "&",
        # а НЕ через второй "?". Иначе query-string получается
        # "login/two-step?_xfRedirect=...&remember=1" — XenForo видит "=" в
        # сегменте маршрута, считает маршрут пустым и роутит на главную доски.
        # На GET это маскировалось redirect'ом pending-2FA (httpx его
        # отслеживает и форма всё же грузилась), но AJAX-POST с кодом уходил
        # на главную: ответ {html: индекс доски, status: ok} без redirect, и
        # сессия не активировалась («код принят, но логина нет»).
        qs = ("&" + _qs_str) if _qs_str else ""
        alt_two_step = f"{FORUM_URL}/index.php?login/two-step{qs}"
        logger.warning("2FA URL вернул %s — пробую alt-path %s",
                       page.status_code, alt_two_step)
        page2 = await client.get(alt_two_step)
        page2 = await _ensure_no_ddos(client, page2, alt_two_step, persist_cookie=True)
        if page2.status_code == 200 and "vddosw3data" not in page2.text:
            page = page2
            target = alt_two_step
            logger.info("Alt 2FA path сработал: %s", target)

    if page.status_code == 403:
        logger.error("HTTP 403 на странице 2FA даже после DDoS-Guard challenge и alt-path.")
        return {"status": "error",
                "message": ("Форум вернул HTTP 403 на странице 2FA. "
                            "Возможно, IP сервера в чёрном списке "
                            "DDoS-Guard или временный rate-limit.")}
    if page.status_code != 200:
        logger.error("HTTP %s на странице 2FA.", page.status_code)
        return {"status": "error",
                "message": f"HTTP {page.status_code} при загрузке страницы 2FA."}

    soup = _soup(page.text)
    csrf = _extract_csrf(page.text)
    if not csrf:
        # На странице two-step может быть отдельный input
        form = soup.find("form")
        if form:
            inp = form.find("input", {"name": "_xfToken"})
            if inp:
                csrf = inp.get("value")
    if not csrf:
        return {"status": "error", "message": "CSRF-токен на странице 2FA не найден."}

    # Извлекаем доступные провайдеры (email, totp, backup)
    providers: list[str] = []
    for inp in soup.find_all("input", {"name": "provider"}):
        val = inp.get("value")
        if val and val not in providers:
            providers.append(val)
    # Если кнопок-провайдеров нет, ищем по data-attribute
    if not providers:
        for el in soup.find_all(attrs={"data-provider": True}):
            val = el.get("data-provider")
            if val and val not in providers:
                providers.append(val)

    # Выбор по умолчанию: email > totp > что есть
    default_provider = None
    for pref in ("email", "totp", "backup"):
        if pref in providers:
            default_provider = pref
            break
    if default_provider is None:
        default_provider = providers[0] if providers else "email"

    logger.info("2FA: доступны провайдеры %s, выбран по умолчанию «%s».",
                providers or ["?"], default_provider)

    return {
        "status": "2fa",
        "providers": providers or ["email"],
        "provider": default_provider,
        "csrf": csrf,
        "two_step_url": target,
    }


async def forum_submit_2fa(state: dict, code: str,
                            trust_device: bool = True) -> dict:
    """Отправляет код 2FA. state — dict от forum_login() со status='2fa'.

    state['client'] — открытый httpx.AsyncClient с куками от логина.
    Этот метод закроет клиент перед возвратом (в любом случае).
    """
    code = code.strip().replace(" ", "")
    logger.info("Отправляю 2FA-код (провайдер: %s, длина кода: %d).",
                state.get("provider"), len(code))

    client: httpx.AsyncClient = state["client"]
    csrf = state["csrf"]
    provider = state["provider"]
    two_step_url = state.get("two_step_url", TWO_STEP_URL)

    # По умолчанию клиент закрывается в finally. Но если код можно ввести
    # повторно (неверный код, не активировалась сессия) — оставляем клиент
    # открытым: FSM остаётся в waiting_for_2fa_code, и следующая попытка
    # должна идти по той же сессии. Иначе client.post упадёт на закрытом
    # клиенте. Закроет его _login_cancel/global_cancel или успешный возврат.
    keep_open = False

    try:
        # _xfRequestUri должен указывать на ЛОГИЧЕСКИЙ роут two-step, а не на
        # "/index.php". Когда страница 2FA пришла через alt-path
        # (/index.php?login/two-step?...), httpx.URL(...).path == "/index.php" —
        # и XenForo, получив POST с _xfRequestUri=/index.php, не находит
        # отложенного 2FA-пользователя в контексте и просто редиректит на
        # /login/, не активируя сессию (код «принят», но логина нет).
        _ts_path = httpx.URL(two_step_url).path or ""
        if "two-step" in _ts_path:
            _two_step_uri = _ts_path
        else:
            # alt-path: реальный роут зашит в query (?login/two-step?...)
            _two_step_uri = "/login/two-step"
        payload = {
            "code": code,
            "provider": provider,
            "trust": "1" if trust_device else "0",
            "remember": "1",
            "confirm": "1",
            "_xfToken": csrf,
            "_xfRedirect": f"{FORUM_URL}/",
            "_xfRequestUri": _two_step_uri,
            "_xfWithData": "1",
            "_xfResponseType": "json",
        }
        ajax = {
            **HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": two_step_url,
        }
        r = await client.post(two_step_url, data=payload, headers=ajax)
        logger.info("2FA POST %s -> HTTP %s (_xfRequestUri=%s, провайдер=%s)",
                    two_step_url, r.status_code, _two_step_uri, provider)

        try:
            resp_json = r.json()
        except (ValueError, json.JSONDecodeError):
            resp_json = None

        # XenForo на правильный 2FA-код возвращает JSON с пустым errors и
        # redirect="/" (или другим URL). На неправильный код — errors с описанием.
        # Это и есть надёжный признак успеха, парсить главную незачем.
        if resp_json:
            errors = resp_json.get("errors")
            if errors:
                msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
                logger.warning("Форум отверг 2FA-код: %s", msg)
                # Сессия 2FA ещё жива — пользователь может ввести код заново.
                # НЕ закрываем клиент, иначе следующий POST упадёт.
                keep_open = True
                return {"status": "error", "message": msg}

            redirect_url = resp_json.get("redirect")
            # redirect — ГЛАВНЫЙ диагностический признак. Если форум принял код
            # и реально залогинил — redirect ведёт на "/" или профиль. Если же
            # POST не нашёл отложенного 2FA-состояния в сессии (сломалась
            # преемственность сессии или роут) — XenForo без ошибки редиректит
            # обратно на /login/. Логируем на INFO, чтобы видеть в проде.
            logger.info("2FA принят форумом без ошибок. redirect=%r, ключи ответа: %s",
                        redirect_url, sorted(resp_json.keys()))
            if not redirect_url:
                # РЕШАЮЩАЯ ДИАГНОСТИКА: форум вернул html-перерисовку вместо
                # redirect. errors пуст, но логина нет — настоящая причина
                # лежит в этом html (например «срок действия кода истёк»,
                # «неверный код», скрытое поле, требование капчи и т.п.).
                # Раньше мы его выбрасывали. Достаём content/h1 и логируем.
                _html = resp_json.get("html")
                if isinstance(_html, dict):
                    _content = _html.get("content") or _html.get("h1") or ""
                else:
                    _content = _html or ""
                _txt = re.sub(r"<[^>]+>", " ", str(_content))
                _txt = re.sub(r"\s+", " ", _txt).strip()
                logger.warning(
                    "2FA: форум вернул html без redirect (status=%r). Текст диалога: %r",
                    resp_json.get("status"), _txt[:600],
                )
            if redirect_url:
                # Дёргаем redirect, чтобы XenForo дописал в jar финальные куки
                # (иногда новые xf_session/xf_user приходят именно на этом шаге)
                if redirect_url.startswith("/"):
                    redirect_url = FORUM_URL + redirect_url
                logger.debug("Делаю GET по redirect: %s", redirect_url)
                try:
                    rr = await client.get(redirect_url)
                    rr = await _ensure_no_ddos(client, rr, redirect_url,
                                               persist_cookie=True)
                except httpx.RequestError as e:
                    logger.debug("GET redirect упал, но 2FA уже принят: %s", e)

            cookies_dict = _flatten_cookies(client)

            # XenForo выдаёт xf_user (долгоживущий «remember me») только когда
            # сервер реально дописал постоянную сессию. Часто после 2FA в jar
            # лежит лишь xf_session — это ВАЛИДНАЯ сессия, просто без remember-
            # куки. Поэтому отсутствие xf_user — не провал: проверяем настоящий
            # признак авторизации, data-logged-in=true на главной, и оттуда же
            # берём имя пользователя (одним запросом).
            home_html = None
            try:
                r_home = await client.get(FORUM_URL)
                r_home = await _ensure_no_ddos(client, r_home, FORUM_URL,
                                               persist_cookie=True)
                home_html = r_home.text
                cookies_dict = _flatten_cookies(client)
            except httpx.RequestError as e:
                logger.debug("GET главной после 2FA упал: %s", e)

            logged_in = bool(home_html and _extract_user_id(home_html))

            if not logged_in and "xf_user" not in cookies_dict:
                logger.warning(
                    "После 2FA сессия не подтверждена: data-logged-in!=true и нет "
                    "xf_user. Куки: %s",
                    ", ".join(sorted(cookies_dict.keys())) or "—",
                )
                # Приглашаем повторить ввод — значит клиент должен пережить
                # этот возврат, иначе следующий POST упадёт на закрытом клиенте.
                keep_open = True
                return {"status": "error",
                        "message": ("Форум принял код, но сессия не активировалась. "
                                    "Попробуйте ещё раз — возможно, код устарел.")}

            if "xf_user" not in cookies_dict:
                logger.warning(
                    "2FA пройден, но форум не выдал xf_user (в jar только %s). "
                    "Сессия рабочая; после истечения xf_session понадобится "
                    "повторный вход.",
                    ", ".join(sorted(cookies_dict.keys())) or "—",
                )

            username = (_extract_username(_soup(home_html)) if home_html
                        else await _resolve_username(client))
            logger.info("2FA пройден, аккаунт: «%s».", username)
            return {"status": "ok", "username": username, "cookies": cookies_dict}

        # JSON не вернулся вообще — fallback на парсинг главной
        final = await client.get(FORUM_URL)
        final = await _ensure_no_ddos(client, final, FORUM_URL)

        if _extract_user_id(final.text):
            username = _extract_username(_soup(final.text))
            logger.info("2FA пройден (через парсинг главной): «%s».", username)
            return {"status": "ok", "username": username,
                    "cookies": _flatten_cookies(client)}

        soup = _soup(final.text)
        html_tag = soup.find("html")
        logged = html_tag.get("data-logged-in") if html_tag else "?"
        logger.warning("После 2FA: data-logged-in=%r, кук в jar: %s",
                       logged, ", ".join(sorted(_flatten_cookies(client).keys())))
        # Приглашаем повторить — оставляем клиент живым для следующей попытки.
        keep_open = True
        return {"status": "error",
                "message": "Код принят, но форум не активировал сессию. "
                           "Попробуйте ещё раз — возможно, код устарел."}

    except httpx.RequestError as e:
        logger.error("Сетевая ошибка при отправке 2FA-кода: %s", e)
        return {"status": "error", "message": f"Ошибка сети: {e}"}
    except Exception as e:
        logger.exception("Непредвиденная ошибка 2FA")
        return {"status": "error", "message": f"Ошибка: {e}"}
    finally:
        # Закрываем клиент, КРОМЕ случаев когда пользователь может ввести код
        # заново (keep_open=True): тогда FSM остаётся в waiting_for_2fa_code и
        # следующая попытка пойдёт по этой же сессии. Клиент в итоге закроют
        # _login_cancel/global_cancel при выходе из сценария.
        if not keep_open:
            await client.aclose()


async def check_auth() -> tuple[bool, str]:
    """Проверка авторизации на форуме (использует активные куки из cookies.json).
    Возвращает (успех, имя_пользователя/описание_ошибки)."""
    return await _check_auth_with_cookies(load_cookies())


async def check_auth_for_cookies(cookies: dict) -> tuple[bool, str]:
    """Проверка авторизации с конкретным набором куков (без записи в cookies.json).
    Используется для проверки нескольких аккаунтов за один проход."""
    return await _check_auth_with_cookies(cookies)


async def _check_auth_with_cookies(cookies: dict) -> tuple[bool, str]:
    started = time.monotonic()
    logger.info("Проверяю авторизацию на форуме %s ...", FORUM_URL)

    if not cookies:
        logger.warning("Файл cookies.json пуст или отсутствует.")
        return False, "Куки не загружены."

    if "xf_user" not in cookies:
        names = ", ".join(sorted(cookies.keys())) or "—"
        if "xf_session" not in cookies:
            # Без xf_session И без xf_user сессии заведомо нет — не тратим запрос.
            logger.warning("В куках нет ни xf_user, ни xf_session. Имеется: %s", names)
            return False, (
                f"В файле нет ни <code>xf_user</code>, ни <code>xf_session</code>.\n"
                f"Найдено: <code>{names}</code>"
            )
        # xf_session есть — это может быть валидная сессия без «remember me».
        # Не отказываем заранее: ниже GET / проверит data-logged-in по факту.
        logger.info("В куках нет xf_user, но есть xf_session — проверяю сессию запросом.")

    async with httpx.AsyncClient(
        cookies=cookies, headers=HEADERS, follow_redirects=True, timeout=15.0,
    ) as client:
        try:
            response = await client.get(FORUM_URL)
            elapsed = time.monotonic() - started
            logger.debug("GET %s -> HTTP %s за %.2f с", FORUM_URL, response.status_code, elapsed)

            # Сначала пробуем решить DDoS-Guard challenge (если он есть) —
            # только после этого проверяем статус-код. Иначе 403 от DDoS-Guard
            # сразу роняет проверку, не дав шанса на solve.
            response = await _ensure_no_ddos(client, response, FORUM_URL,
                                                persist_cookie=True)

            if response.status_code != 200:
                logger.warning("Форум вернул HTTP %s при проверке авторизации.", response.status_code)
                return False, f"Форум вернул ошибку HTTP {response.status_code}."

            html = response.text

            user_id = _extract_user_id(html)
            if user_id == 0:
                # Подробная диагностика, чтобы пользователь понял в чём дело
                soup = _soup(html)
                html_tag = soup.find("html")
                logged_in_attr = html_tag.get("data-logged-in") if html_tag else None
                title_tag = soup.find("title")
                title = title_tag.text.strip() if title_tag else "?"

                names = ", ".join(sorted(cookies.keys()))
                logger.warning(
                    "Форум не считает сессию авторизованной. data-logged-in=%r, "
                    "title=%r, имеющиеся куки: %s",
                    logged_in_attr, title, names,
                )
                return False, (
                    "Форум не принял сессию (<code>data-logged-in != true</code>).\n\n"
                    "Возможные причины:\n"
                    "• <b>Куки истекли</b> — экспортируйте свежие из браузера.\n"
                    "• <b>User-Agent не совпадает</b> с браузером, где брали куки. "
                    f"Сейчас в .env: <code>{escape_html(USER_AGENT)}</code>\n"
                    "• Куки взяты из режима инкогнито — после закрытия они умирают."
                )

            updated = _flatten_cookies(client)
            if updated:
                save_cookies({**cookies, **updated})

            username = _extract_username(_soup(html))
            logger.info("Авторизация успешна: пользователь «%s» (user_id=%s).",
                        username, user_id)
            return True, username

        except httpx.RequestError as e:
            logger.error("Сетевая ошибка при проверке авторизации: %s", e)
            return False, f"Ошибка сети: {e}"
        except Exception as e:
            logger.exception("Неизвестная ошибка проверки авторизации")
            return False, f"Ошибка: {e}"


# ---------------- Публикация жалобы ----------------

def _extract_csrf(soup_or_html) -> Optional[str]:
    """Достаёт CSRF-токен XenForo. Сначала из <html data-csrf> через regex
    (надёжно при битой разметке), потом из формы через BeautifulSoup.

    Принимает либо BeautifulSoup, либо строку HTML.
    """
    if isinstance(soup_or_html, str):
        html = soup_or_html
        soup = _soup(html)
    else:
        soup = soup_or_html
        html = None

    # 1. Через regex по <html ...>
    if html is None:
        # Восстановим исходный HTML из soup (приблизительно). Лучше передавать
        # сразу строку — это путь по умолчанию.
        try:
            html = str(soup)
        except Exception:
            html = ""

    m_html = _HTML_TAG_RE.search(html)
    if m_html:
        m_csrf = _CSRF_RE.search(m_html.group(1))
        if m_csrf:
            return m_csrf.group(1)

    # 2. Запасной способ — input в форме
    csrf_input = soup.find("input", {"name": "_xfToken"})
    if csrf_input:
        return csrf_input.get("value")
    return None


def _extract_required_prefix(soup: BeautifulSoup) -> Optional[str]:
    """Возвращает prefix_id, который форум обязательно требует выбрать.
    Берёт первый ненулевой <option>. None — если префикс не нужен."""
    select_tag = soup.find("select", {"name": "prefix_id"})
    if not select_tag:
        return None

    available: list[tuple[str, str]] = []
    for opt in select_tag.find_all("option"):
        val = (opt.get("value") or "").strip()
        text = opt.get_text(strip=True)
        if val and val != "0":
            available.append((val, text))

    if not available:
        return None

    chosen, label = available[0]
    logger.info("Раздел требует префикс. Авто-выбор: id=%s, «%s». Доступно вариантов: %d.",
                chosen, label, len(available))
    if len(available) > 1:
        others = ", ".join(f"{v}={t!r}" for v, t in available[1:])
        logger.debug("Прочие доступные префиксы: %s", others)
    return chosen


async def post_complaint(section_id: int, title: str, message: str,
                           cookies: dict | None = None) -> tuple[bool, str]:
    """Публикация темы (жалобы) в указанный раздел форума.

    Если `cookies` переданы — публикация идёт ИМЕННО с этими куками,
    а cookies.json не используется и не перезаписывается. Это нужно для
    параллельной работы пула (queue_processor): несколько корутин могут
    публиковать одновременно от имени разных аккаунтов, не мешая друг другу.

    Если `cookies=None` — старое поведение: куки читаются из cookies.json,
    свежие куки пишутся обратно в cookies.json.

    Возвращает (успех, результат). При успехе — ссылка на тему. При
    ошибке — текст; для ошибок «нужен перелогин» (редирект на /login/)
    префикс ответа = 'AUTH: ', для «нет прав / DDoS-Guard в разделе» —
    'NOPERM: '. Хелперы `is_auth_error()` / `is_noperm_error()`.
    """
    started = time.monotonic()
    logger.info("Публикую жалобу в раздел node_id=%s. Заголовок: «%s», длина тела: %d симв.",
                section_id, title, len(message))

    use_session = cookies is None
    if use_session:
        cookies = load_cookies()
        if not cookies:
            logger.warning("Невозможно отправить жалобу: куки отсутствуют.")
            return False, "Отсутствуют куки. Загрузите файл cookies.json."
    else:
        if not cookies:
            return False, "AUTH: переданы пустые куки."

    post_url = f"{FORUM_URL}/forums/{section_id}/post-thread"

    if use_session:
        client_ctx = _session()
    else:
        client_ctx = httpx.AsyncClient(
            cookies=cookies, headers=HEADERS,
            follow_redirects=True, timeout=20.0,
        )

    async with client_ctx as client:
        try:
            # 1. Загружаем форму создания темы (CSRF + список префиксов)
            logger.debug("Шаг 1/3: запрашиваю форму создания темы — %s", post_url)
            get_response = await client.get(post_url)

            # DDoS-Guard? Решаем на ТОМ ЖЕ HTML, не делаем отдельный запрос.
            get_response = await _ensure_no_ddos(
                client, get_response, post_url,
                persist_cookie=use_session,
            )

            # Редирект на /login/ — куки протухли, нужен перелогин.
            if "/login/" in str(get_response.url):
                logger.warning("При открытии формы (раздел %s) редирект "
                                "на /login/ — куки протухли.", section_id)
                return False, ("AUTH: сессия истекла. Куки протухли, "
                                "нужен повторный /login.")

            if get_response.status_code != 200:
                if get_response.status_code == 403:
                    # 403 после успешного challenge — куки валидны, но в
                    # этом разделе аккаунт не имеет прав, либо DDoS-Guard
                    # на хостинге режет POST-метод. Возвращаем NOPERM —
                    # вызывающий просто пробует другой аккаунт.
                    logger.warning("HTTP 403 при открытии формы (раздел %s).",
                                    section_id)
                    return False, (
                        f"NOPERM: HTTP 403 в разделе {section_id}. "
                        "Нет прав на публикацию в этом разделе либо "
                        "DDoS-Guard блокирует запрос с IP сервера."
                    )
                logger.error("HTTP %s при открытии формы создания темы.", get_response.status_code)
                return False, f"Не удалось открыть страницу создания темы. HTTP {get_response.status_code}."

            soup = _soup(get_response.text)

            csrf_token = _extract_csrf(get_response.text)
            if not csrf_token:
                logger.error("CSRF-токен не найден на странице создания темы.")
                return False, "Не удалось найти CSRF-токен защиты XenForo на странице."
            logger.debug("Шаг 2/3: CSRF-токен получен (длина %d).", len(csrf_token))

            prefix_id = _extract_required_prefix(soup)

            payload = {
                "title": title,
                "message": message,
                "_xfToken": csrf_token,
                "_xfRequestUri": f"/forums/{section_id}/post-thread",
                "_xfWithData": "1",
                "_xfResponseType": "json",
            }
            if prefix_id is not None:
                payload["prefix_id"] = prefix_id

            ajax_headers = {**HEADERS, "X-Requested-With": "XMLHttpRequest"}
            logger.debug("Шаг 3/3: POST %s (AJAX, %d полей)...", post_url, len(payload))
            post_response = await client.post(post_url, data=payload, headers=ajax_headers)

            if use_session:
                updated = _flatten_cookies(client)
                if updated:
                    save_cookies({**cookies, **updated})

            elapsed = time.monotonic() - started
            if post_response.status_code != 200:
                logger.error("Форум вернул HTTP %s при отправке жалобы (за %.2f с).",
                             post_response.status_code, elapsed)
                return False, f"Ошибка отправки формы. HTTP {post_response.status_code}."

            try:
                result_json = post_response.json()
            except (ValueError, json.JSONDecodeError):
                snippet = post_response.text[:200].replace("\n", " ")
                logger.error("Форум вернул не-JSON ответ. Начало: %s", snippet)
                return False, f"Форум вернул некорректный ответ (не JSON): {snippet}"

            if "errors" in result_json:
                errors = result_json["errors"]
                if isinstance(errors, list):
                    error_msg = "; ".join(errors)
                elif isinstance(errors, dict):
                    error_msg = "; ".join(str(v) for v in errors.values())
                else:
                    error_msg = str(errors)
                logger.warning("Форум вернул ошибку валидации формы: %s", error_msg)
                return False, f"Ошибка форума: {error_msg}"

            redirect_url = result_json.get("redirect")
            if redirect_url:
                if redirect_url.startswith("/"):
                    redirect_url = FORUM_URL + redirect_url
                logger.info("Жалоба успешно опубликована за %.2f с. URL темы: %s",
                            elapsed, redirect_url)
                return True, redirect_url

            logger.warning("Форум принял запрос, но не вернул URL новой темы. Ответ: %s",
                           str(result_json)[:300])
            return False, "Тема отправлена, но форум не вернул ссылку перенаправления."

        except httpx.RequestError as e:
            logger.error("Сетевая ошибка при отправке жалобы: %s", e)
            return False, f"Ошибка сети при связи с форумом: {e}"
        except Exception as e:
            logger.exception("Неизвестная ошибка при отправке жалобы")
            return False, f"Внутренняя ошибка отправки: {e}"


async def fetch_thread_admin_comment(
    thread_url: str,
    cookies: dict | None = None,
) -> Optional[str]:
    """Открывает страницу темы и возвращает текст последнего поста (комментарий
    администратора). None — если тема недоступна или ответа ещё нет."""
    if not thread_url:
        return None

    if cookies is not None:
        client_ctx = httpx.AsyncClient(
            cookies=cookies, headers=HEADERS,
            follow_redirects=True, timeout=15.0,
        )
    else:
        client_ctx = _session(timeout=15.0)

    try:
        async with client_ctx as client:
            r = await client.get(thread_url)
            if r.status_code == 403:
                return None
            if r.status_code != 200:
                return None
            r = await _ensure_no_ddos(client, r, thread_url)
            if "/login/" in str(r.url):
                return None
            return _extract_last_admin_comment(_soup(r.text))
    except Exception:
        logger.exception("Ошибка при получении комментария админа для %s", thread_url)
        return None


def is_auth_error(error_text: str) -> bool:
    """Ошибка означает «нужен ручной перелогин» (редирект на /login/)."""
    if not error_text:
        return False
    return error_text.startswith("AUTH:") or error_text.startswith("AUTH ")


def is_noperm_error(error_text: str) -> bool:
    """Ошибка означает «нет прав в разделе / DDoS-Guard режет path».
    Куки валидны, нужно просто пробовать другой аккаунт."""
    if not error_text:
        return False
    return error_text.startswith("NOPERM:") or error_text.startswith("NOPERM ")


# ---------------- Автообнаружение структуры форума ----------------

def _extract_node_id(href: str) -> Optional[int]:
    """Извлекает числовой node_id из ссылки на форум XenForo."""
    if not href:
        return None
    match = NODE_ID_RE.search(href)
    return int(match.group(1)) if match else None


def _extract_last_admin_comment(soup) -> Optional[str]:
    """Извлекает текст последнего поста в теме.

    На XenForo после вердикта админ обычно пишет ответ типа «Жалоба
    одобрена/отклонена потому что...». Это последняя
    <article class="message"> в списке постов; тело — в
    <div class="bbWrapper">.

    Если в теме всего один пост (только сам OP), комментария админа нет —
    возвращаем None.
    """
    try:
        articles = soup.find_all("article", class_=re.compile(r"\bmessage\b"))
        if not articles or len(articles) <= 1:
            return None

        last = articles[-1]
        body = last.find("div", class_=re.compile(r"bbWrapper"))
        if not body:
            return None

        text = body.get_text(" ", strip=True)
        if not text:
            return None

        # Обрезаем чтобы не лопнуть лимит Telegram-сообщения
        if len(text) > 1500:
            text = text[:1500] + "..."
        return text
    except Exception:
        logger.exception("Ошибка извлечения комментария админа")
        return None


def _classify_category(text: str) -> Optional[str]:
    """Классифицирует название подраздела жалоб по ключевым словам."""
    lower = text.lower()
    for key in _CATEGORY_PRIORITY:
        for keyword in COMPLAINT_CATEGORY_KEYWORDS[key]:
            if keyword in lower:
                return key
    return None


async def discover_servers() -> tuple[bool, list[tuple[str, int]] | str]:
    """Сканирует главную страницу форума и собирает список серверов.

    Сервера именуются как "Сервер №01 | RED" — имя берём после '|'.
    Возвращает (успех, [(имя, node_id), ...] в порядке с форума или текст_ошибки).
    """
    if not load_cookies():
        logger.warning("Сканирование серверов прервано: куки не загружены.")
        return False, "Куки не загружены."

    logger.info("Сканирую главную страницу форума на предмет списка серверов...")

    async with _session() as client:
        try:
            response = await client.get(FORUM_URL)
            if response.status_code != 200:
                logger.error("HTTP %s при загрузке главной страницы.", response.status_code)
                return False, f"HTTP {response.status_code} при загрузке главной страницы."

            response = await _ensure_no_ddos(client, response, FORUM_URL,
                                                persist_cookie=True)

            servers = _parse_servers_from_html(response.text)
            if not servers:
                logger.warning("На главной странице форума не нашлось ни одного сервера.")
                return False, "Не найдено ни одного сервера на главной странице форума."

            logger.info("Найдено серверов: %d. Первый: %s, последний: %s.",
                        len(servers), servers[0][0], servers[-1][0])
            return True, servers

        except httpx.RequestError as e:
            logger.error("Сетевая ошибка при сканировании серверов: %s", e)
            return False, f"Ошибка сети: {e}"
        except Exception as e:
            logger.exception("Ошибка при сканировании списка серверов")
            return False, f"Ошибка: {e}"


def _parse_servers_from_html(html: str) -> list[tuple[str, int]]:
    soup = _soup(html)
    servers: list[tuple[str, int]] = []
    seen: set[int] = set()
    for title_tag in soup.find_all(["h3", "h4"], class_="node-title"):
        a_tag = title_tag.find("a", href=True)
        if not a_tag:
            continue
        text = a_tag.text.strip()
        if "|" not in text:
            continue
        name = text.split("|")[-1].strip()
        if not name:
            continue
        node_id = _extract_node_id(a_tag["href"])
        if node_id and node_id not in seen:
            seen.add(node_id)
            servers.append((name, node_id))
    return servers


def _parse_subforums_from_html(html: str) -> list[tuple[str, int]]:
    """Извлекает дочерние под-форумы (подразделы) со страницы раздела.
    Возвращает [(имя, node_id), ...] в порядке с форума, без дублей."""
    soup = _soup(html)
    subs: list[tuple[str, int]] = []
    seen: set[int] = set()
    for title_tag in soup.find_all(["h3", "h4"], class_="node-title"):
        a_tag = title_tag.find("a", href=True)
        if not a_tag:
            continue
        name = a_tag.text.strip()
        if not name:
            continue
        node_id = _extract_node_id(a_tag["href"])
        if node_id and node_id not in seen:
            seen.add(node_id)
            subs.append((name, node_id))
    return subs


async def discover_technical_subsections() -> tuple[bool, list[tuple[str, int]] | str]:
    """Сканирует страницу «Технического раздела» (node 22) и собирает список
    его дочерних под-форумов (подразделов).

    Возвращает (успех, [(имя, node_id), ...] в порядке с форума | текст_ошибки).
    Каждый подраздел — отдельный форумный node, в котором создаётся тема.
    """
    if not load_cookies():
        logger.warning("Дискавери техраздела прерван: куки не загружены.")
        return False, "Куки не загружены."

    url = f"{FORUM_URL}/forums/{TECHNICAL_SECTION_NODE_ID}/"
    logger.info("Сканирую технический раздел: %s", url)
    async with _session() as client:
        try:
            response = await client.get(url)
            response = await _ensure_no_ddos(client, response, url,
                                              persist_cookie=True)
            if response.status_code != 200:
                logger.error("HTTP %s при загрузке техраздела.", response.status_code)
                return False, f"HTTP {response.status_code} при загрузке техраздела."

            subs = _parse_subforums_from_html(response.text)
            if not subs:
                logger.warning("В техническом разделе не найдено подразделов.")
                return False, "Подразделы технического раздела не найдены."

            logger.info("Технический раздел: найдено подразделов %d.", len(subs))
            return True, subs
        except httpx.RequestError as e:
            logger.error("Сетевая ошибка при сканировании техраздела: %s", e)
            return False, f"Ошибка сети: {e}"
        except Exception as e:
            logger.exception("Ошибка при сканировании техраздела")
            return False, f"Ошибка: {e}"


def _parse_categories_from_soup(soup: BeautifulSoup) -> dict[str, tuple[str, int]]:
    """Извлекает подразделы жалоб со страницы (схема с node-title)."""
    categories: dict[str, tuple[str, int]] = {}
    for title_tag in soup.find_all(["h3", "h4"], class_="node-title"):
        a_tag = title_tag.find("a", href=True)
        if not a_tag:
            continue
        text = a_tag.text.strip()
        key = _classify_category(text)
        if key and key not in categories:
            node_id = _extract_node_id(a_tag["href"])
            if node_id:
                categories[key] = (text, node_id)
    return categories


def _find_complaints_link(soup: BeautifulSoup) -> Optional[str]:
    """Находит ссылку на подраздел/категорию 'Жалобы' на странице сервера."""
    for a_tag in soup.find_all("a", href=True):
        if a_tag.text.strip() not in ("Жалобы", "Раздел жалоб"):
            continue
        href = a_tag["href"]
        if "/forums/" not in href and "/categories/" not in href:
            continue
        if href.startswith("/"):
            return FORUM_URL + href
        if href.startswith("http"):
            return href
        return f"{FORUM_URL}/{href}"
    return None


async def _discover_categories_with_client(
    client: httpx.AsyncClient,
    server_node_id: int,
) -> tuple[bool, dict[str, tuple[str, int]] | str]:
    """То же что discover_complaint_categories, но переиспользует переданный клиент.
    Полезно при массовой синхронизации, чтобы не плодить новые соединения."""
    try:
        server_url = f"{FORUM_URL}/forums/{server_node_id}/"
        response = await client.get(server_url)
        if response.status_code != 200:
            logger.warning("Сервер node=%s: HTTP %s при загрузке раздела.",
                           server_node_id, response.status_code)
            return False, f"HTTP {response.status_code} при загрузке раздела сервера."

        response = await _ensure_no_ddos(client, response, server_url)
        soup = _soup(response.text)

        # Схема A: подкатегории жалоб лежат прямо на странице сервера
        categories = _parse_categories_from_soup(soup)
        if categories:
            logger.debug("Сервер node=%s: схема A (категории на верхнем уровне).",
                         server_node_id)

        # Схема B: ищем ссылку на 'Жалобы' и заходим внутрь
        if not categories:
            complaints_url = _find_complaints_link(soup)
            if complaints_url:
                logger.debug("Сервер node=%s: схема B → %s", server_node_id, complaints_url)
                response2 = await client.get(complaints_url)
                response2 = await _ensure_no_ddos(client, response2, complaints_url)
                if response2.status_code == 200:
                    categories = _parse_categories_from_soup(_soup(response2.text))
                else:
                    logger.warning("Сервер node=%s: HTTP %s при заходе в раздел жалоб.",
                                   server_node_id, response2.status_code)

        if not categories:
            logger.warning("Сервер node=%s: не удалось найти подразделы жалоб.", server_node_id)
            return False, "Подразделы жалоб не найдены."

        logger.info("Сервер node=%s: найдено категорий жалоб %d (%s).",
                    server_node_id, len(categories), ", ".join(sorted(categories.keys())))
        return True, categories

    except httpx.RequestError as e:
        logger.error("Сервер node=%s: сетевая ошибка — %s", server_node_id, e)
        return False, f"Ошибка сети: {e}"
    except Exception as e:
        logger.exception("Сервер node=%s: ошибка при сканировании категорий", server_node_id)
        return False, f"Ошибка: {e}"


async def discover_complaint_categories(
    server_node_id: int,
) -> tuple[bool, dict[str, tuple[str, int]] | str]:
    """Public-обёртка: открывает разовый клиент и сканирует категории сервера."""
    if not load_cookies():
        return False, "Куки не загружены."
    async with _session() as client:
        return await _discover_categories_with_client(client, server_node_id)


async def discover_all_complaint_categories(
    servers: list[tuple[str, int]],
    concurrency: int = 6,
    progress: Optional[callable] = None,
) -> dict[int, dict[str, tuple[str, int]]]:
    """Параллельно сканирует категории жалоб для всех серверов одним клиентом.

    Возвращает {server_node_id: categories_dict_или_None}. Серверы без категорий
    в результат не попадают.

    progress(idx, total, name, ok) вызывается после обработки каждого сервера —
    это позволяет хендлеру отображать живой прогресс.
    """
    if not load_cookies():
        return {}

    semaphore = asyncio.Semaphore(concurrency)
    result: dict[int, dict[str, tuple[str, int]]] = {}
    total = len(servers)
    done = 0

    async with _session() as client:

        async def worker(idx: int, name: str, node_id: int):
            nonlocal done
            async with semaphore:
                ok, cats = await _discover_categories_with_client(client, node_id)
            done += 1
            ok_flag = bool(ok and isinstance(cats, dict) and cats)
            if ok_flag:
                result[node_id] = cats  # type: ignore[assignment]
            if progress is not None:
                try:
                    await progress(done, total, name, ok_flag)
                except Exception:
                    logger.debug("progress callback бросил исключение — игнорирую.",
                                 exc_info=True)

        await asyncio.gather(*[
            worker(i, name, nid) for i, (name, nid) in enumerate(servers, 1)
        ])

    return result


# ---------------- Проверка статуса темы ----------------

# Сопоставление текста префикса темы (lowercase) на стандартный статус.
# Статусы в боте:
#   "pending"  — Ожидание (или префикс не найден)
#   "accepted" — принята/одобрена/удовлетворена
#   "rejected" — отклонена/отказ
#   "closed"   — закрыта (без явного решения)
_STATUS_KEYWORDS = {
    "accepted": (
        "принят", "принято", "принята",
        "одобрен", "одобрено", "одобрена",
        "удовлетвор",
        "выполнен", "выполнено", "выполнена",
        "наказан", "наказание выдано",
        "рассмотрено",
    ),
    "rejected": (
        "отклонен", "отклонён", "отклонено", "отклонена",
        "отказ", "отказано", "отказана",
        "не принят",
        "недостаточно",
        "истёк срок", "истек срок",
    ),
    "closed":   (
        "закрыт", "закрыто", "закрыта",
        "архив",
    ),
    "pending":  (
        "ожидание", "ожидан",
        "новая", "новое",
        "в работе",
        "рассматривается", "рассмотрении",
    ),
}


async def fetch_complaint_status(
    thread_url: str,
    cookies: dict | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Заходит на страницу темы и определяет её статус.

    Если передан `cookies` — использует именно их (актуально когда жалобу
    подавал не активный аккаунт; на BR темы видят только автор и модераторы).
    Иначе берёт активные куки из cookies.json.

    Возвращает (status, prefix_text, admin_comment).
    - status: 'pending'/'accepted'/'rejected'/'closed' или None
    - prefix_text: текст префикса с форума (для логов)
    - admin_comment: текст последнего ответа админа в теме (если статус
      финальный) — будет показан в уведомлении пользователю.
    """
    if not thread_url:
        return None, None, None

    # Создаём клиент: либо со специальными куками, либо стандартный
    if cookies is not None:
        client_ctx = httpx.AsyncClient(
            cookies=cookies, headers=HEADERS,
            follow_redirects=True, timeout=15.0,
        )
    else:
        client_ctx = _session(timeout=15.0)

    # async with сам закроет клиент в __aexit__ — повторный aclose не нужен.
    try:
        async with client_ctx as client:
            try:
                r = await client.get(thread_url)
                if r.status_code == 403:
                    logger.info("Тема %s — HTTP 403 (нет прав видеть тему).",
                                thread_url)
                    return None, None, None
                if r.status_code != 200:
                    logger.debug("Тема %s — HTTP %s.", thread_url, r.status_code)
                    return None, None, None
                # DDoS-Guard? Решаем на той же сессии
                r = await _ensure_no_ddos(client, r, thread_url)
                html = r.text

                # Если переадресовало в /login/ — значит куки не подходят
                if "/login/" in str(r.url) or "log-in" in html.lower()[:5000]:
                    logger.info("Тема %s — куки не пускают (редирект на login).",
                                thread_url)
                    return None, None, None

                soup = _soup(html)

                # Список фраз, которые гарантированно не являются префиксом
                # темы (это плейсхолдеры/служебные подписи XenForo).
                BLACKLIST = (
                    "искать только в заголовках",
                    "поиск",
                    "filter by",
                    "title only",
                )

                def _is_real_prefix(text: str) -> bool:
                    if not text:
                        return False
                    if not (2 <= len(text) <= 40):
                        return False
                    lowered = text.lower().strip()
                    return not any(b in lowered for b in BLACKLIST)

                # XenForo пишет префикс ВНУТРИ заголовка темы:
                #   <h1 class="p-title-value">
                #     <a class="labelLink"><span class="label label--Red">Принято</span></a>
                #     Bruce_Banner | nRP Drive
                #   </h1>
                # Поэтому ищем именно в .p-title-value, а не по всей странице.
                prefix_text = None
                source = None  # для отладки откуда взяли префикс

                title_block = soup.find(class_="p-title-value")
                if title_block is None:
                    title_block = soup.find("h1", class_=re.compile(r"p-title|title"))
                if title_block is None:
                    title_block = soup.find("h1")

                if title_block is not None:
                    # 1. <span class="label ...">префикс</span> внутри h1
                    for span in title_block.find_all(
                        "span", class_=re.compile(r"\blabel\b")
                    ):
                        text = span.get_text(strip=True)
                        if _is_real_prefix(text):
                            prefix_text = text
                            source = "title.span.label"
                            break

                    # 2. <a class="labelLink"><span>...</span></a>
                    if not prefix_text:
                        for a in title_block.find_all(
                            "a", class_=re.compile(r"label")
                        ):
                            text = a.get_text(strip=True)
                            if _is_real_prefix(text):
                                prefix_text = text
                                source = "title.a.label"
                                break

                    # 3. data-prefix-id у элементов внутри заголовка
                    if not prefix_text:
                        for el in title_block.find_all(attrs={"data-prefix-id": True}):
                            text = el.get_text(strip=True)
                            if _is_real_prefix(text):
                                prefix_text = text
                                source = "title.data-prefix-id"
                                break

                # 4. Запасной путь — meta og:title (XenForo туда тоже префикс
                # пишет: <meta property="og:title" content="Принято - Bruce | DM">)
                if not prefix_text:
                    og = soup.find("meta", attrs={"property": "og:title"})
                    if og and og.get("content"):
                        og_title = og["content"]
                        # Префикс часто идёт первым словом перед разделителем
                        m = re.match(
                            r"^\s*([А-Яа-яЁё][А-Яа-яЁё\s]{2,30}?)\s*[\-—\|:]",
                            og_title,
                        )
                        if m:
                            candidate = m.group(1).strip()
                            if _is_real_prefix(candidate):
                                prefix_text = candidate
                                source = "og:title"

                logger.info("Тема %s: префикс=%r (источник: %s)",
                             thread_url, prefix_text, source)

                # Достаём последний пост (если он от админа/модератора —
                # это комментарий по жалобе, его и пришлём пользователю).
                admin_comment = _extract_last_admin_comment(soup)

                if prefix_text:
                    lowered = prefix_text.lower()
                    for status, keywords in _STATUS_KEYWORDS.items():
                        if any(k in lowered for k in keywords):
                            # Для нефинальных статусов (pending) комментарий
                            # не нужен — нечего пользователю слать
                            comment = admin_comment if status != "pending" else None
                            return status, prefix_text, comment
                    logger.info("Тема %s: префикс «%s» не распознан, считаю pending.",
                                 thread_url, prefix_text)
                    return "pending", prefix_text, None

                # Префикса нет — может тема уже закрыта (lock-icon)
                if soup.find(class_=re.compile(r"is-locked|threadClosed|locked")):
                    return "closed", None, admin_comment

                return "pending", None, None

            except httpx.RequestError as e:
                logger.warning("Сетевая ошибка при проверке статуса темы %s: %s",
                               thread_url, e)
                return None, None, None
            except Exception as e:
                logger.exception("Ошибка проверки статуса темы %s: %s", thread_url, e)
                return None, None, None
    except Exception:
        # Защита от непредвиденных ошибок при создании/закрытии клиента
        logger.exception("Ошибка работы клиента при проверке статуса темы %s",
                         thread_url)
        return None, None, None


# ---------------- Удаление и редактирование тем на форуме ----------------

# Регулярки для извлечения id темы и id первого поста
_THREAD_ID_RE = re.compile(r"/threads/(?:[^/]+\.)?(\d+)/?")
_POST_ID_RE = re.compile(r"data-content=\"post-(\d+)\"")
_FIRST_POST_ID_RE = re.compile(
    r"<article[^>]*\bdata-content=\"post-(\d+)\"", re.IGNORECASE
)


def _extract_thread_id(thread_url: str) -> int | None:
    """Извлекает числовой id темы из URL вида /threads/some-name.123456/."""
    m = _THREAD_ID_RE.search(thread_url or "")
    return int(m.group(1)) if m else None


async def _get_first_post_id(client: httpx.AsyncClient,
                              thread_url: str) -> int | None:
    """Открывает страницу темы и берёт id первого поста."""
    r = await client.get(thread_url)
    if r.status_code != 200:
        logger.warning("Тема %s — HTTP %s, post_id не получен.",
                       thread_url, r.status_code)
        return None
    r = await _ensure_no_ddos(client, r, thread_url)
    m = _FIRST_POST_ID_RE.search(r.text)
    return int(m.group(1)) if m else None


async def delete_thread(thread_url: str,
                         reason: str = "Удалено автором",
                         cookies: dict | None = None) -> tuple[bool, str]:
    """Удаляет тему на форуме (мягкое удаление в XenForo).

    Если `cookies` переданы — операция идёт ИМЕННО с этими куками, а
    cookies.json не читается и не перезаписывается. Это исключает гонку на
    глобальном cookies.json при параллельной работе нескольких сценариев
    (другой админ/фоновый запрос мог бы переписать файл между нашими
    запросами, и POST ушёл бы под чужим аккаунтом → 403).

    Если `cookies=None` — старое поведение: куки из cookies.json.

    Возвращает (успех, сообщение). Удалить может только автор темы или модератор.
    """
    use_session = cookies is None
    if use_session:
        cookies = load_cookies()
        if not cookies:
            return False, "Нет кук — невозможно удалить тему."
    elif not cookies:
        return False, "Переданы пустые куки — невозможно удалить тему."

    thread_id = _extract_thread_id(thread_url)
    if not thread_id:
        return False, f"Не удалось извлечь id темы из URL: {thread_url}"

    delete_url = f"{FORUM_URL}/threads/{thread_id}/delete"
    logger.info("Удаляю тему #%s на форуме (%s).", thread_id, thread_url)

    if use_session:
        client_ctx = _session()
    else:
        client_ctx = httpx.AsyncClient(
            cookies=cookies, headers=HEADERS,
            follow_redirects=True, timeout=20.0,
        )

    async with client_ctx as client:
        try:
            # 1. Открываем страницу подтверждения удаления — получаем CSRF
            r = await client.get(delete_url)
            if r.status_code != 200:
                if r.status_code == 403:
                    return False, ("Доступ запрещён (HTTP 403). Тема не принадлежит "
                                   "активному аккаунту, или окно удаления закрыто "
                                   "правилами форума.")
                return False, f"HTTP {r.status_code} при открытии формы удаления."

            r = await _ensure_no_ddos(client, r, delete_url)
            csrf = _extract_csrf(r.text)
            if not csrf:
                return False, "Не удалось получить CSRF-токен для удаления."

            # 2. Шлём подтверждение
            payload = {
                "reason": reason,
                "hard_delete": "0",  # soft delete (можно восстановить модератору)
                "_xfToken": csrf,
                "_xfRequestUri": f"/threads/{thread_id}/delete",
                "_xfWithData": "1",
                "_xfResponseType": "json",
            }
            ajax = {**HEADERS, "X-Requested-With": "XMLHttpRequest",
                    "Referer": delete_url}
            r2 = await client.post(delete_url, data=payload, headers=ajax)

            try:
                resp_json = r2.json()
            except (ValueError, json.JSONDecodeError):
                snippet = r2.text[:200].replace("\n", " ")
                return False, f"Форум вернул не-JSON: {snippet}"

            errors = resp_json.get("errors")
            if errors:
                msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
                logger.warning("Форум отказал в удалении темы #%s: %s",
                               thread_id, msg)
                return False, f"Форум отказал: {msg}"

            logger.info("Тема #%s успешно удалена.", thread_id)
            return True, "Тема удалена с форума."

        except httpx.RequestError as e:
            logger.error("Сетевая ошибка при удалении темы: %s", e)
            return False, f"Ошибка сети: {e}"
        except Exception as e:
            logger.exception("Непредвиденная ошибка удаления темы")
            return False, f"Ошибка: {e}"


async def edit_thread_post(thread_url: str, new_message: str,
                             new_title: str | None = None,
                             cookies: dict | None = None) -> tuple[bool, str]:
    """Редактирует первый пост темы (тело жалобы).

    Если new_title задан — попутно меняет заголовок темы (метод XenForo
    разный, поэтому делаем двумя запросами).

    Если `cookies` переданы — операция идёт ИМЕННО с этими куками, без
    чтения/перезаписи cookies.json (исключает гонку на глобальном файле
    при параллельной работе). Если `cookies=None` — куки из cookies.json.

    Возвращает (успех, сообщение).
    """
    use_session = cookies is None
    if use_session:
        cookies = load_cookies()
        if not cookies:
            return False, "Нет кук — невозможно отредактировать."
    elif not cookies:
        return False, "Переданы пустые куки — невозможно отредактировать."

    thread_id = _extract_thread_id(thread_url)
    if not thread_id:
        return False, f"Не удалось извлечь id темы из URL: {thread_url}"

    logger.info("Редактирую тему #%s.", thread_id)

    if use_session:
        client_ctx = _session()
    else:
        client_ctx = httpx.AsyncClient(
            cookies=cookies, headers=HEADERS,
            follow_redirects=True, timeout=20.0,
        )

    async with client_ctx as client:
        try:
            # 1. Получаем post_id первого поста
            post_id = await _get_first_post_id(client, thread_url)
            if not post_id:
                return False, "Не удалось получить id первого поста темы."

            # 2. Открываем форму редактирования поста — для CSRF
            edit_url = f"{FORUM_URL}/posts/{post_id}/edit"
            r = await client.get(edit_url)
            if r.status_code != 200:
                if r.status_code == 403:
                    return False, ("Доступ запрещён (HTTP 403). Тему может "
                                   "редактировать только автор, и не позже срока, "
                                   "установленного администрацией форума.")
                return False, f"HTTP {r.status_code} при открытии формы редактирования."
            r = await _ensure_no_ddos(client, r, edit_url)
            csrf = _extract_csrf(r.text)
            if not csrf:
                return False, "Не удалось получить CSRF-токен."

            # 3. Сохраняем новое тело сообщения
            payload = {
                "message": new_message,
                "_xfToken": csrf,
                "_xfRequestUri": f"/posts/{post_id}/edit",
                "_xfWithData": "1",
                "_xfResponseType": "json",
            }
            ajax = {**HEADERS, "X-Requested-With": "XMLHttpRequest",
                    "Referer": edit_url}
            r2 = await client.post(f"{FORUM_URL}/posts/{post_id}/save",
                                     data=payload, headers=ajax)
            try:
                resp_json = r2.json()
            except (ValueError, json.JSONDecodeError):
                snippet = r2.text[:200].replace("\n", " ")
                return False, f"Форум вернул не-JSON: {snippet}"

            errors = resp_json.get("errors")
            if errors:
                msg = "; ".join(errors) if isinstance(errors, list) else str(errors)
                return False, f"Форум отказал: {msg}"

            # 4. (Опционально) меняем заголовок темы
            if new_title:
                edit_thread_url = f"{FORUM_URL}/threads/{thread_id}/edit"
                rt = await client.get(edit_thread_url)
                if rt.status_code == 200:
                    csrf2 = _extract_csrf(rt.text) or csrf
                    title_payload = {
                        "title": new_title,
                        "_xfToken": csrf2,
                        "_xfRequestUri": f"/threads/{thread_id}/edit",
                        "_xfWithData": "1",
                        "_xfResponseType": "json",
                    }
                    ajax_t = {**HEADERS, "X-Requested-With": "XMLHttpRequest",
                              "Referer": edit_thread_url}
                    rt2 = await client.post(edit_thread_url, data=title_payload,
                                              headers=ajax_t)
                    try:
                        rj = rt2.json()
                        if rj.get("errors"):
                            logger.warning("Не удалось обновить заголовок темы #%s: %s",
                                           thread_id, rj["errors"])
                    except (ValueError, json.JSONDecodeError):
                        logger.debug("Заголовок темы изменён, но ответ не JSON.")

            logger.info("Тема #%s (post_id=%s) успешно отредактирована.",
                        thread_id, post_id)
            return True, "Тема обновлена на форуме."

        except httpx.RequestError as e:
            logger.error("Сетевая ошибка при редактировании темы: %s", e)
            return False, f"Ошибка сети: {e}"
        except Exception as e:
            logger.exception("Непредвиденная ошибка редактирования темы")
            return False, f"Ошибка: {e}"



