"""
Невидимый водяной знак в сигналах через zero-width Unicode символы.

Кодирует user_id пользователя (до 2^40 ≈ 1 трлн) в 40 невидимых
символов, вставленных после первого символа текста.

Символы:
  U+200B  ZERO WIDTH SPACE         = бит 0
  U+200C  ZERO WIDTH NON-JOINER   = бит 1

Применение:
  - Если текст сигнала скопирован и слит — admin может определить
    кто именно слил, вставив текст в /decode_wm.
  - Watermark не виден глазу ни в одном Telegram-клиенте.
  - Не ломает HTML parse_mode (вставляется вне тегов).
"""

_ZW0  = '\u200B'   # бит 0
_ZW1  = '\u200C'   # бит 1
_BITS = 40          # до 1_099_511_627_776 — достаточно для Telegram user_id


def wm_encode(user_id: int) -> str:
    """Кодирует user_id в строку из 40 zero-width символов."""
    return ''.join(_ZW1 if (user_id >> i) & 1 else _ZW0 for i in range(_BITS))


def wm_inject(text: str, user_id: int) -> str:
    """Вставляет невидимый водяной знак после первого символа текста."""
    wm = wm_encode(user_id)
    if len(text) < 2:
        return text + wm
    return text[:1] + wm + text[1:]


def wm_decode(text: str) -> int | None:
    """
    Извлекает user_id из текста с водяным знаком.
    Возвращает None если водяной знак не найден или неполный.
    """
    bits = []
    for ch in text:
        if ch == _ZW0:
            bits.append(0)
        elif ch == _ZW1:
            bits.append(1)
    if len(bits) != _BITS:
        return None
    return sum(b << i for i, b in enumerate(bits))
