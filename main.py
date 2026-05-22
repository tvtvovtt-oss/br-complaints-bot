"""Точка входа для хостингов, которые запускают `python main.py`.

Эквивалентно `python -m src.bot`. Корень проекта сам добавляется в sys.path,
поэтому импорт `from src.config import ...` работает в обоих случаях.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.bot import main  # noqa: E402
import asyncio  # noqa: E402

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
