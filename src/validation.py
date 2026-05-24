"""Валидация полей формы жалобы.

Правила форума запрещают:
- нецензурную брань;
- клевету и оскорбления администрации;
- доказательства в ВКонтакте/Одноклассниках (только фото/видео-хостинги).

Также проверяется формат ника, даты, ссылок и длина текстов.
Валидаторы возвращают (ok: bool, value_or_error: str).
"""
import re

# ---------- Фильтр нецензурной лексики ----------

# Корни нецензурных слов. Записываются как regex-фрагменты, потому что слова
# имеют множество форм (приставки, суффиксы, окончания).
# Список не претендует на полноту, но покрывает базовые корни и большинство
# их производных, чтобы аккаунт не получил предупреждение/бан за мат.
_BAD_WORD_ROOTS = [
    r"х[уy][йиеяюёюe]",     # ху*
    r"п[иi][зs3]д",          # пизд*
    r"бл[яa]",               # бля*
    r"бл[яa]д",              # блядь
    r"еб[аaуyиeё]",          # еба-, ебу-, ебё-
    r"еб[лn]",               # ебл*
    r"бьеб",                 # вы*ебать (через ь)
    r"п[еe]рд[оo]",          # пердо*
    r"д[оo]лб[оo]",          # долбо*
    r"м[уy]д[аaоoиie]",      # муд*
    r"гондон",               # гондон
    r"г[оo]вн",              # говн*
    r"с[уy]к[аaиie]",        # сук*
    r"шл[юю][хx]",           # шлюх*
    r"шл[юю]ш",              # шлюш*
    r"тв[аa]р[ьи]",          # твар*
    r"уебок",                # уебок
    r"уеб[аaиi]",            # уеба-, уеби-
    r"чм[оo]шн",             # чмошн*
    r"др[оo]чи",             # дрочи-
    r"др[оo]чн",             # дрочн*
    r"ж[оo]п[аa]",           # жоп*
    r"з[аa]еб",              # заеб*
    r"вз[ьъ]еб",             # взъеб*
    r"наеб",                 # наеб*
    r"под[ъь]еб",            # подъеб*
    r"оп[иi]зден",           # опизден*
    r"мраз",                 # мраз*
    r"п[иi]д[оoаa]р",        # пидор/пидар
    r"пид[оo]р",             # пидор
    r"гнид[аaы]",            # гнид*
    r"бз[дd]",               # бзд*
    r"п[еe]т[уy][хx]",       # петух (как оскорбление)
    r"с[оo]сн",              # сосн*
    r"\bтрах",               # трах* (только в начале слова, чтобы не цеплять «страх»)
    r"мудозвон",             # мудозвон
    r"еблан",                # еблан
    r"очк[оo]",              # очко (контекст бранный)
]

# Латиница часто используется как обход; нормализуем буквы перед проверкой
_LEET_MAP = str.maketrans({
    "@": "а", "a": "а",
    "0": "о", "o": "о",
    "3": "е", "e": "е",
    "u": "у", "y": "у",
    "i": "и", "1": "и",
    "p": "р",
    "x": "х",
    "k": "к",
    "c": "с", "s": "с",
    "b": "в",
    "h": "н",
    "m": "м",
    "t": "т",
    "n": "н",
    "g": "г",
    "z": "з",
    "j": "ж",
    "r": "р",
})


def _normalize(text: str) -> str:
    """Приводит текст к виду, удобному для поиска матных корней:
    - lower-case
    - заменяет latin-буквы и цифры на кириллицу по карте обходов
    - убирает повторяющиеся подряд буквы (хууууй -> хуй)
    - убирает все, кроме букв
    """
    t = text.lower().translate(_LEET_MAP)
    # Сжимаем подряд идущие одинаковые символы (>=3 -> 1) для обхода 'хууууй'
    t = re.sub(r"(.)\1{2,}", r"\1", t)
    # Удаляем все, кроме кириллицы и пробелов
    t = re.sub(r"[^а-яё ]", "", t)
    return t


_BAD_PATTERN = re.compile(r"|".join(_BAD_WORD_ROOTS))

# Маска самоцензуры: 2+ букв и 1+ "звёздочка-подобный" символ вперемешку,
# например 'бл***', 'х*й', 'с*ка', '****ишь'. Это всегда признак мата.
_CENSORED_PATTERN = re.compile(
    r"(?:[a-zа-яё]+[*#@+]+[a-zа-яё*#@+]*[a-zа-яё]"
    r"|[*#@+]{2,}[a-zа-яё]+"
    r"|[a-zа-яё]+[*#@+]{2,})",
    re.IGNORECASE,
)


def contains_profanity(text: str) -> bool:
    """Возвращает True, если в тексте найден один из запрещённых корней
    или явная самоцензура мата (`х*й`, `бл***` и т.п.).
    """
    if not text:
        return False
    # 1. Проверка на самоцензуру: буквы вперемешку с */#/@/+
    if _CENSORED_PATTERN.search(text):
        return True
    # 2. Нормализованная проверка по корням
    norm = _normalize(text)
    if not norm:
        return False
    return bool(_BAD_PATTERN.search(norm))


# ---------- Валидаторы конкретных полей ----------

# Типичный SAMP/RP-ник: имя_фамилия с подчёркиванием, латиница и цифры,
# 3-24 символа в каждой части, в сумме до 30. Ставим мягкие границы чтобы
# покрыть как обычные ники (Bruce_Banner), так и редкие длинные (Iv_Petrov123).
NICK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{2,23}_[A-Za-z][A-Za-z0-9]{1,23}$")

# Гласные (включая Y — в реальных никах часто играет роль гласной: Tommy, Sky)
_VOWELS = set("aeiouy")

# Максимум согласных подряд: реальные ники обычно дают не больше 4 подряд
# (Strzelecki, McKinley). 5+ — почти всегда случайный набор клавиш.
_MAX_CONSONANTS_IN_ROW = 4

# Максимум одного и того же символа подряд: реальные ники "Carrado", "Tommy"
# имеют максимум 2 одинаковые подряд. 4+ — фейк (xxxxxx, aaaaaa).
_MAX_SAME_CHAR_IN_ROW = 3

# Минимальная доля гласных в части ника (для частей длиннее 4 букв)
_MIN_VOWEL_RATIO = 0.18  # 18%

# Клавиатурные ряды и подпоследовательности — почти всегда мусор
_KEYBOARD_ROWS = (
    "qwertyuiop", "asdfghjkl", "zxcvbnm",
    "1234567890",
    "йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю",  # на всякий — кириллицу всё
                                                  # равно регекс отбросит, но пусть
)
# Минимальная длина клавиатурного фрагмента, который считаем подозрительным
_KEYBOARD_MIN_RUN = 4


def _has_keyboard_run(text: str) -> bool:
    """True если в строке встречается ≥4 подряд идущих символов с одного
    ряда клавиатуры (qwer, asdf, zxcv, 1234 и т.п.). Берём как прямую,
    так и обратную последовательность."""
    low = text.lower()
    for row in _KEYBOARD_ROWS:
        for start in range(len(row) - _KEYBOARD_MIN_RUN + 1):
            chunk = row[start:start + _KEYBOARD_MIN_RUN]
            if chunk in low or chunk[::-1] in low:
                return True
    return False


def _is_random_part(part: str) -> tuple[bool, str]:
    """Эвристика «часть ника похожа на случайный набор букв».
    Возвращает (random?, reason) — если True, ник лучше отвергнуть."""
    p = part.lower()
    if not p:
        return False, ""

    # 1. Повторы одной буквы 4+ подряд
    if re.search(rf"(.)\1{{{_MAX_SAME_CHAR_IN_ROW},}}", p):
        return True, "слишком много одинаковых букв подряд"

    # 2. Согласные подряд (без учёта цифр)
    letters_only = re.sub(r"[^a-z]", "", p)
    consonants_run = re.search(
        rf"[bcdfghjklmnpqrstvwxz]{{{_MAX_CONSONANTS_IN_ROW + 1},}}",
        letters_only,
    )
    if consonants_run:
        return True, f"подряд идёт {len(consonants_run.group())} согласных — похоже на набор клавиш"

    # 3. Доля гласных
    if len(letters_only) >= 5:
        vowels = sum(1 for ch in letters_only if ch in _VOWELS)
        ratio = vowels / len(letters_only)
        if vowels == 0:
            return True, "нет ни одной гласной"
        if ratio < _MIN_VOWEL_RATIO:
            return True, f"гласных всего {int(ratio * 100)}% — обычно у имён больше"
    elif len(letters_only) >= 3:
        # Короткие части (3-4 буквы) — должна быть хотя бы одна гласная
        if not any(ch in _VOWELS for ch in letters_only):
            return True, "нет ни одной гласной"

    # 4. Клавиатурные последовательности
    if _has_keyboard_run(p):
        return True, "содержит клавиатурную последовательность (qwerty/asdfgh/12345…)"

    return False, ""

# Допустимые форматы даты:
#   15.05.2026
#   15.05.2026 19:30
#   15/05/2026 19:30
#   2026-05-15
#   2026-05-15 19:30
DATE_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"        # 15.05.2026
    r"|\d{4}[./-]\d{1,2}[./-]\d{1,2}"         # 2026-05-15
    r")"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*$"   # опц. время
)

URL_RE = re.compile(r"https?://[^\s,;]+", re.IGNORECASE)

# Запрещённые правилами хостинги (соцсети)
FORBIDDEN_HOSTS = (
    "vk.com", "vk.ru", "m.vk.com",
    "ok.ru", "m.ok.ru", "odnoklassniki.ru",
)

MIN_DESCRIPTION_LEN = 10
MAX_DESCRIPTION_LEN = 4000
MIN_SUMMARY_LEN = 3
MAX_SUMMARY_LEN = 80


def validate_nickname(text: str) -> tuple[bool, str]:
    text = text.strip()
    if not text:
        return False, "Никнейм не может быть пустым."
    if contains_profanity(text):
        return False, "В никнейме обнаружена нецензурная лексика."
    if not NICK_RE.match(text):
        return False, (
            "Неверный формат никнейма. Ожидается <code>Имя_Фамилия</code> "
            "латиницей (например: <code>Bruce_Banner</code>)."
        )

    # Эвристика: похож ли ник на случайный набор клавиш
    parts = text.split("_", 1)
    for idx, part in enumerate(parts, start=1):
        is_random, reason = _is_random_part(part)
        if is_random:
            label = "имя" if idx == 1 else "фамилия"
            return False, (
                f"Похоже на случайный набор букв ({label}: «{part}»): "
                f"{reason}.\n"
                f"Введите реальный игровой ник в формате "
                f"<code>Имя_Фамилия</code> (например: <code>Bruce_Banner</code>)."
            )

    return True, text


def validate_date(text: str) -> tuple[bool, str]:
    text = text.strip()
    if not text:
        return False, "Дата не может быть пустой."
    if not DATE_RE.match(text):
        return False, (
            "Неверный формат даты. Примеры: `15.05.2026`, `15.05.2026 19:30`, "
            "`2026-05-15 19:30`."
        )
    return True, text


def validate_summary(text: str) -> tuple[bool, str]:
    text = text.strip()
    if len(text) < MIN_SUMMARY_LEN:
        return False, f"Слишком короткая суть (минимум {MIN_SUMMARY_LEN} символа)."
    if len(text) > MAX_SUMMARY_LEN:
        return False, (
            f"Суть слишком длинная (максимум {MAX_SUMMARY_LEN} символов). "
            "Это поле идёт в заголовок темы — сократите."
        )
    if "|" in text:
        return False, "Символ `|` запрещён — он используется как разделитель в заголовке."
    if "\n" in text:
        return False, "Краткая суть должна быть в одну строку."
    if contains_profanity(text):
        return False, "В сути обнаружена нецензурная лексика — она запрещена правилами форума."
    return True, text


def validate_description(text: str) -> tuple[bool, str]:
    text = text.strip()
    if len(text) < MIN_DESCRIPTION_LEN:
        return False, (
            f"Слишком короткое описание (минимум {MIN_DESCRIPTION_LEN} символов). "
            "Опишите ситуацию подробнее."
        )
    if len(text) > MAX_DESCRIPTION_LEN:
        return False, f"Описание слишком длинное (максимум {MAX_DESCRIPTION_LEN} символов)."
    if contains_profanity(text):
        return False, (
            "В описании обнаружена нецензурная лексика — это запрещено правилами форума "
            "и может привести к отказу в жалобе."
        )
    return True, text


def validate_proof(text: str) -> tuple[bool, str]:
    text = text.strip()
    if not text:
        return False, "Прикрепление доказательств обязательно."

    urls = URL_RE.findall(text)
    if not urls:
        return False, (
            "Не найдено ни одной ссылки. Загрузите доказательства на фото/видео-хостинг "
            "(YouTube, Imgur, Yapix, Postimages и т.п.) и пришлите ссылку."
        )

    forbidden = []
    for url in urls:
        url_lower = url.lower()
        for host in FORBIDDEN_HOSTS:
            if host in url_lower:
                forbidden.append(host)
                break

    if forbidden:
        unique = ", ".join(sorted(set(forbidden)))
        return False, (
            f"Ссылки на {unique} запрещены правилами форума. "
            "Залейте доказательства на YouTube/Imgur/Yapix/Postimages."
        )

    if contains_profanity(text):
        return False, "В тексте с доказательствами обнаружена нецензурная лексика."

    return True, text
