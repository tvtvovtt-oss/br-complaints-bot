import os
import io
import urllib.request
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/roboto/Roboto-Medium.ttf"
FONT_PATH = "assets/font.ttf"
BG_PATH = "assets/bg.jpg"

def ensure_font():
    if not os.path.exists(FONT_PATH):
        try:
            logger.info("Downloading font from %s", FONT_URL)
            urllib.request.urlretrieve(FONT_URL, FONT_PATH)
        except Exception as e:
            logger.error("Failed to download font: %s", e)

def get_font(size: int):
    ensure_font()
    if os.path.exists(FONT_PATH):
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            pass
            
    # Если скачать не удалось, используем системные шрифты (укажем абсолютные пути для Windows)
    fallback_fonts = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        "arial.ttf"
    ]
    for f in fallback_fonts:
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
            
    return ImageFont.load_default()

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
    
    font_large = get_font(56)  # Увеличено
    font_med = get_font(34)    # Увеличено
    font_small = get_font(26)  # Увеличено
    
    # Цвета
    TEXT_MAIN = (255, 255, 255, 255)
    TEXT_DIM = (200, 200, 200, 255)
    ACCENT = (255, 80, 80, 255) # Красный
    SUCCESS = (80, 255, 120, 255) # Зеленый
    GOLD = (255, 215, 0, 255) # Золотой
    
    # 2. Шапка профиля
    name = user_info.get("name", "Игрок")
    # Ограничиваем длину ника, чтобы не сломать вёрстку
    if len(name) > 20: name = name[:18] + "..."
    draw.text((40, 30), name, font=font_large, fill=TEXT_MAIN)
    draw.text((40, 100), f"ID: {user_info.get('id', '???')} | {user_info.get('role', 'Пользователь')}", font=font_small, fill=TEXT_DIM)
    
    # Разделитель
    draw.line([(40, 145), (760, 145)], fill=(255, 255, 255, 50), width=2)
    
    # 3. Колонки со статистикой
    draw.text((40, 170), f"Всего подано: {stats.get('total', 0)}", font=font_med, fill=TEXT_MAIN)
    draw.text((40, 215), f"Ожидают: {stats.get('pending', 0) + stats.get('queue', 0)}", font=font_med, fill=TEXT_DIM)
    draw.text((40, 260), f"На рассмотрении: {stats.get('review', 0)}", font=font_med, fill=(255, 200, 80, 255))
    
    draw.text((450, 170), f"✅ Одобрено: {stats.get('accepted', 0)}", font=font_med, fill=SUCCESS)
    draw.text((450, 215), f"❌ Отклонено: {stats.get('rejected', 0)}", font=font_med, fill=ACCENT)
    
    # 4. Прогресс-бар успешности
    draw.text((40, 315), "Рейтинг успешности", font=font_med, fill=TEXT_MAIN)
    pct = stats.get("success_pct", 0)
    draw.text((670, 315), f"{pct}%", font=font_med, fill=SUCCESS if pct >= 50 else ACCENT)
    
    bar_x, bar_y = 40, 360
    bar_w, bar_h = 720, 24
    # Рисуем подложку
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=12, fill=(50, 50, 50, 255))
    
    # Рисуем заполнение
    if pct > 0:
        fill_w = int((pct / 100) * bar_w)
        # Убедимся, что ширина хотя бы минимальная (для красивого скругления)
        if fill_w < 24: fill_w = 24
        fill_color = SUCCESS if pct >= 50 else (255, 180, 60, 255) if pct >= 20 else ACCENT
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], radius=12, fill=fill_color)
        
    # 5. Медали / Достижения
    accepted = stats.get("accepted", 0)
    medals = []
    if accepted >= 1: medals.append("Новичок")
    if accepted >= 5: medals.append("Следящий")
    if accepted >= 20: medals.append("Детектив")
    if accepted >= 50: medals.append("Гроза сервера")
    if accepted >= 100: medals.append("Легенда")
    
    if medals:
        draw.text((40, 405), "Достижения:", font=font_med, fill=TEXT_MAIN)
        medals_str = " • ".join(medals)
        draw.text((40, 445), medals_str, font=font_small, fill=GOLD)
    else:
        draw.text((40, 405), "Достижения: Пока нет.", font=font_med, fill=TEXT_DIM)
    
    # 6. Конвертация в JPEG
    final_img = bg.convert("RGB")
    buf = io.BytesIO()
    final_img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()
