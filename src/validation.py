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
    r"ху[йиеяюёе]",          # ху*
    r"пизд",                 # пизд*
    r"бля",                  # бля* / бляд*
    r"блят",                 # блять
    r"блиад",                # bliad → блиад
    r"еба",                  # еба-/ебать (схватит и «ебало»)
    r"ебу",                  # ебу-
    r"ебё",                  # ебё-
    r"ебл",                  # ебл*
    r"ебн",                  # ебн* / ебник
    r"ебш",                  # ебш*
    r"ебнут",                # ебнут*
    r"ёбн",                  # ёбн*
    r"ёб[аиу]",              # ёба-, ёби-, ёбу-
    r"бьеб",                 # вы*ебать (через ь)
    r"пердо",                # пердо*
    r"долбо",                # долбо*
    r"муда",                 # муд*
    r"муди",                 # муд*
    r"мудо",                 # мудо*
    r"мудак",                # мудак
    r"гондон",               # гондон
    r"гандон",               # гандон (вариант)
    r"говн",                 # говн*
    r"сук[аиоы]",            # сук*
    r"шлух",                 # шлух* (после нормализации `lu`→лу)
    r"шлюх",                 # шлюх*
    r"шлюш",                 # шлюш*
    r"шалав",                # шалав*
    r"шмар[аи]",             # шмар*
    r"стерв",                # стерв*
    r"твар",                 # твар*
    r"уебок",                # уебок
    r"уеба",                 # уеба
    r"уеби",                 # уеби
    r"уебн",                 # уебн*
    r"уебищ",                # уебищ*
    r"чмошн",                # чмошн*
    r"чмо\b",                # чмо как отдельное слово
    r"дроч",                 # дроч*
    r"жоп[аеуы]",            # жоп*
    r"заеб",                 # заеб*
    r"взъеб",                # взъеб*
    r"наеб",                 # наеб*
    r"подъеб",               # подъеб*
    r"опизден",              # опизден*
    r"мраз",                 # мраз*
    r"пид[оа]р",             # пидор/пидар/пидорас
    r"пидорас",              # отдельно длинная форма
    r"педер",                # педер*аст
    r"гнид[аы]",             # гнид*
    r"петух",                # петух (как оскорбление)
    r"\bтрах",               # трах* (только в начале слова, чтобы не цеплять «страх»)
    r"мудозвон",             # мудозвон
    r"еблан",                # еблан
    r"еблищ",                # еблищ*
    r"\bхрен",               # хрен (нейтральный «hren», но в начале слова)
    r"обоср",                # обоср*
    r"дрист",                # дрист*
    r"\bхер[нов]",           # хер*
    r"\bлох",                # лох (только в начале слова)
    r"очко\b",               # очко как отдельное слово (бранный контекст)
]

# Латиница часто используется как обход (транслит). Нормализуем сначала
# диграфы, потом одиночные буквы.

# Диграфы — комбинации латинских букв, кодирующих один кириллический звук.
# Применяются в первую очередь (порядок важен — сначала длинные!).
_LEET_DIGRAPHS = [
    ("shh", "щ"),
    ("yo",  "ё"), ("jo",  "ё"),
    ("yu",  "ю"), ("ju",  "ю"),
    ("ya",  "я"), ("ja",  "я"),
    ("ye",  "е"), ("je",  "е"),
    ("zh",  "ж"),
    ("kh",  "х"),
    ("ch",  "ч"),
    ("sh",  "ш"),
    ("ts",  "ц"),
    ("ye",  "е"),
]

# Одиночные буквы / цифры → кириллица.
# Транслит: фонетический («Petya» → «петя», а не визуальный «ретя»).
_LEET_MAP = str.maketrans({
    "@": "а", "a": "а",
    "0": "о", "o": "о",
    "3": "е", "e": "е",
    "u": "у", "y": "у",
    "i": "и", "1": "и",
    "p": "п",
    "x": "х",
    "k": "к",
    "c": "к",   # фонетический транслит «c» обычно «к» (Carrado→карадо)
    "s": "с",
    "b": "б",
    "h": "х",
    "m": "м",
    "t": "т",
    "n": "н",
    "g": "г",
    "z": "з",
    "j": "ж",
    "r": "р",
    "d": "д",
    "f": "ф",
    "l": "л",
    "v": "в",
    "w": "в",
    "q": "к",
})


def _normalize(text: str) -> str:
    """Приводит текст к виду, удобному для поиска матных корней:
    - lower-case
    - сначала заменяет диграфы (sh→ш, kh→х, ya→я и т.д.)
    - потом одиночные latin-буквы и цифры на кириллицу по карте обходов
    - схлопывает повторяющиеся подряд буквы (хууууй → хуй)
    - убирает всё, кроме букв и пробелов (но «_» становится пробелом, чтобы
      «Bruce_Banner» не склеивалось в «брусебаннер» где «еба» создаёт ложный
      позитив).
    """
    t = text.lower().replace("_", " ")
    # Сначала диграфы — иначе sh схлопнется в «сн» по одиночным заменам.
    for src, dst in _LEET_DIGRAPHS:
        t = t.replace(src, dst)
    t = t.translate(_LEET_MAP)
    # Сжимаем подряд идущие одинаковые символы (>=3 → 1) для обхода 'хууууй'
    t = re.sub(r"(.)\1{2,}", r"\1", t)
    # Удаляем всё, кроме кириллицы и пробелов
    t = re.sub(r"[^а-яё ]", " ", t)
    # Сжимаем повторные пробелы
    t = re.sub(r"\s+", " ", t).strip()
    return t


_BAD_PATTERN = re.compile(r"|".join(_BAD_WORD_ROOTS))

# Маска самоцензуры: 2+ букв и 1+ "звёздочка-подобный" символ вперемешку,
# например 'бл***', 'х*й', 'с*ка', '****ишь', 'ху*' (буквы перед/после/между *).
_CENSORED_PATTERN = re.compile(
    r"(?:"
    r"[a-zа-яё]+[*#@+]+[a-zа-яё*#@+]*[a-zа-яё]"  # «х*й», «с*ка», «бл***ишь»
    r"|[*#@+]{2,}[a-zа-яё]+"                      # «**ишь»
    r"|[a-zа-яё]+[*#@+]{2,}"                      # «бл***»
    r"|[a-zа-яё]{2,}[*#@+]"                       # «ху*», «бл*»
    r")",
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

# Типичный SAMP/RP-ник: имя_фамилия с подчёркиванием, латиница и цифры.
# Минимум 2 символа в каждой части (Mr_McKinley, Iv_Petrov), максимум 24.
# Реальная фильтрация «случайности» — в эвристике _is_random_part() ниже.
NICK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,23}_[A-Za-z][A-Za-z0-9]{1,23}$")

# Гласные (включая Y — в реальных никах часто играет роль гласной: Tommy, Sky)
_VOWELS = set("aeiouy")

# Максимум согласных подряд: реальные ники обычно дают не больше 4 подряд
# (Strzelecki, McKinley). 5+ — почти всегда случайный набор клавиш.
_MAX_CONSONANTS_IN_ROW = 4

# Максимум одного и того же символа подряд: реальные ники "Carrado", "Tommy"
# имеют максимум 2 одинаковые подряд. 4+ — фейк (xxxxxx, aaaaaa).
_MAX_SAME_CHAR_IN_ROW = 3

# Минимальная доля гласных в части ника (для частей длиннее 4 букв).
# 16% было слишком жёстко: «Strazh» (1/6=16.6%) ловился. Ставим 15%, но
# реальный мусор «dnfhagj» (14%) всё равно не проходит.
_MIN_VOWEL_RATIO = 0.15

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
# Краткие сути типа «DM», «DB» — норма для BR. Минимум 2 символа.
MIN_SUMMARY_LEN = 2
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
