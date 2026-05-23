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
    r"трах",                 # трах*
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
            "Неверный формат никнейма. Ожидается `Имя_Фамилия` латиницей "
            "(например: `Bruce_Banner`)."
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
