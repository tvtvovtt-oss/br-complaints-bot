# Минимальный Docker-образ для Telegram-бота.
# Используется хостингами вроде justrunmy.app, которые собирают image
# из репозитория автоматически. Образ только runtime, без dev-инструментов.

FROM python:3.12-slim

# Ставим минимум для lxml/cryptography/matplotlib (на slim их нет).
# Делаем это в одном RUN, чтобы слой был меньше.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости, чтобы pip-кеш переиспользовался при правках кода
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Затем сам код
COPY . .

# Не буферизуем stdout — иначе логи не появляются в реальном времени
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Стандартная папка для shared-данных у некоторых хостингов
RUN mkdir -p /app/shared

CMD ["python", "main.py"]
