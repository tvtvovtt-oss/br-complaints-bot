import os
import io
import urllib.request
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Inter — современный шрифт с полной поддержкой кириллицы.
# Раньше использовался Roboto Medium, в котором нет кириллических глифов
# и Pillow падал на дефолтный гарнитур — отсюда "сломанный" шрифт в карточке.
FONT_MEDIUM_PATH = "assets/Inter-Medium.ttf"
FONT_SEMIBOLD_PATH = "assets/Inter-SemiBold.ttf"
FONT_URL = (
    "https://github.com/rsms/inter/releases/download/v4.0/Inter-4.0.zip"
)
BG_PATH = "assets/bg.jpg"


def _download_fonts() -> None:
    """Скачивает архив Inter и распаковывает Medium/SemiBold в assets/."""
    if os.path.exists(FONT_MEDIUM_PATH) and os.path.exists(FONT_SEMIBOLD_PATH):
        return
    import zipfile
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
        logger.info("Downloading Inter font from %s", FONT_URL)
        urllib.request.urlretrieve(FONT_URL, tmp_path)
        with zipfile.ZipFile(tmp_path) as z:
            for member in ("extras/ttf/Inter-Medium.ttf",
                           "extras/ttf/Inter-SemiBold.ttf"):
                out_name = os.path.basename(member)
                if os.path.exists(out_name):
                    continue
                with z.open(member) as src, open(out_name, "wb") as dst:
                    dst.write(src.read())
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    except Exception as e:
        logger.error("Failed to download Inter font: %s", e)


def get_font(size: int, weight: str = "medium"):
    """Возвращает шрифт Inter нужного размера и веса.

    weight: "medium" (по умолчанию) или "semibold".
    """
    _download_fonts()
    target = FONT_SEMIBOLD_PATH if weight == "semibold" else FONT_MEDIUM_PATH
    if os.path.exists(target):
        try:
            return ImageFont.truetype(target, size)
        except Exception:
            pass

    # Fallback на системные шрифты с кириллицей (на Windows почти всегда есть).
    fallback_fonts = [
        r"C:\Windows\Fonts\segoeuib.ttf",  # Segoe UI Bold
        r"C:\Windows\Fonts\segoeui.ttf",   # Segoe UI
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
    ]
    for f in fallback_fonts:
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass

    return ImageFont.load_default()


def _fit_text_width(draw: ImageDraw.ImageDraw, text: str, font,
                    max_width: int) -> str:
    """Если текст не помещается в max_width, обрезает с многоточием."""
    if not text:
        return text
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


def generate_profile_card(user_info: dict, stats: dict) -> bytes:
    """Генерирует карточку профиля и возвращает байты JPEG."""
    # 1. Загрузка фона
    try:
        if os.path.exists(BG_PATH):
            bg = Image.open(BG_PATH).convert("RGBA")
        else:
            bg = Image.new("RGBA", (800, 500), (30, 30, 40, 255))
    except Exception as e:
        logger.error("Failed to load background image: %s", e)
        bg = Image.new("RGBA", (800, 500), (30, 30, 40, 255))

    # Подгоняем размер под 800x500
    target_size = (800, 500)
    bg = bg.resize(target_size, Image.Resampling.LANCZOS)

    # Добавляем тёмное затемнение, чтобы белый текст читался лучше
    overlay = Image.new("RGBA", target_size, (0, 0, 0, 160))
    bg = Image.alpha_composite(bg, overlay)

    draw = ImageDraw.Draw(bg)

    # Inter: заголовок пожирнее, остальное — Medium.
    font_xl = get_font(54, weight="semibold")
    font_large = get_font(34, weight="semibold")
    font_med = get_font(30, weight="medium")
    font_small = get_font(24, weight="medium")

    # Цвета
    TEXT_MAIN = (255, 255, 255, 255)
    TEXT_DIM = (200, 200, 200, 255)
    ACCENT = (255, 80, 80, 255)        # Красный
    SUCCESS = (80, 255, 120, 255)      # Зеленый
    WARN = (255, 200, 80, 255)         # Жёлтый
    GOLD = (255, 215, 0, 255)          # Золотой

    # 2. Шапка профиля
    name = user_info.get("name", "Игрок")
    # Укорачиваем имя, чтобы не сломать вёрстку карточки
    name = _fit_text_width(draw, name, font_xl, 720)
    draw.text((40, 28), name, font=font_xl, fill=TEXT_MAIN)
    sub = f"ID: {user_info.get('id', '???')} | {user_info.get('role', 'Пользователь')}"
    draw.text((40, 96), sub, font=font_small, fill=TEXT_DIM)

    # Разделитель
    draw.line([(40, 142), (760, 142)], fill=(255, 255, 255, 50), width=2)

    # 3. Колонки со статистикой
    draw.text((40, 168), f"Всего подано: {stats.get('total', 0)}",
              font=font_large, fill=TEXT_MAIN)
    draw.text((40, 212), f"Ожидают: {stats.get('pending', 0) + stats.get('queue', 0)}",
              font=font_med, fill=TEXT_DIM)
    draw.text((40, 252), f"На рассмотрении: {stats.get('review', 0)}",
              font=font_med, fill=WARN)

    draw.text((450, 168), f"✅ Одобрено: {stats.get('accepted', 0)}",
              font=font_med, fill=SUCCESS)
    draw.text((450, 212), f"❌ Отклонено: {stats.get('rejected', 0)}",
              font=font_med, fill=ACCENT)

    # 4. Прогресс-бар успешности
    draw.text((40, 308), "Рейтинг успешности", font=font_large, fill=TEXT_MAIN)
    pct = stats.get("success_pct", 0)
    pct_text = f"{pct}%"
    pct_w = draw.textlength(pct_text, font=font_large)
    draw.text((760 - pct_w, 308), pct_text, font=font_large,
              fill=SUCCESS if pct >= 50 else ACCENT)

    bar_x, bar_y = 40, 354
    bar_w, bar_h = 720, 24
    # Рисуем подложку
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                           radius=12, fill=(50, 50, 50, 255))

    # Рисуем заполнение
    if pct > 0:
        fill_w = int((pct / 100) * bar_w)
        if fill_w < 24:
            fill_w = 24
        fill_color = (SUCCESS if pct >= 50
                      else (255, 180, 60, 255) if pct >= 20
                      else ACCENT)
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                               radius=12, fill=fill_color)

    # 5. Медали / Достижения
    accepted = stats.get("accepted", 0)
    medals = []
    if accepted >= 1:
        medals.append("Новичок")
    if accepted >= 5:
        medals.append("Следящий")
    if accepted >= 20:
        medals.append("Детектив")
    if accepted >= 50:
        medals.append("Гроза сервера")
    if accepted >= 100:
        medals.append("Легенда")

    if medals:
        draw.text((40, 400), "Достижения:", font=font_large, fill=TEXT_MAIN)
        medals_str = " • ".join(medals)
        medals_str = _fit_text_width(draw, medals_str, font_small, 720)
        draw.text((40, 440), medals_str, font=font_small, fill=GOLD)
    else:
        draw.text((40, 400), "Достижения: Пока нет.",
                  font=font_large, fill=TEXT_DIM)

    # 6. Конвертация в JPEG
    final_img = bg.convert("RGB")
    buf = io.BytesIO()
    final_img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
