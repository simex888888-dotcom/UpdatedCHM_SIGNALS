"""
groq_analyzer.py — Глубокий AI-анализ маркетов Polymarket через Groq API.

Использует OpenAI-совместимый API Groq (бесплатный ключ: console.groq.com).
Env var: GROQ_API_KEY=gsk_...

Входные данные analyze_with_groq(data):
  data = {
      "question":    str,
      "yes_price":   float,   # 0.0 – 1.0
      "no_price":    float,
      "volume_24h":  float,
      "liquidity":   float,
      "end_date":    str,
      "description": str,     # опционально — описание условий разрешения
      "category":    str,     # опционально — крипто / политика / спорт и т.д.
  }

Возвращает dict:
  {
      "recommendation":      "BUY YES" | "BUY NO" | "SKIP",
      "confidence":          "HIGH" | "MEDIUM" | "LOW",
      "edge":                str,           # "~12%" или "0%"
      "risk":                "HIGH" | "MEDIUM" | "LOW",
      "main_thesis":         str,           # главный тезис, 2-3 предл.
      "probability_verdict": str,           # переоценён / недооценён / справедлив
      "yes_scenario":        str,           # что должно случиться для YES
      "no_scenario":         str,           # что должно случиться для NO
      "key_risk":            str,           # главный риск для рекомендованной ставки
  }
"""

import json
import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.getenv("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
            timeout=25.0,  # не ждём больше 25 секунд
        )
    return _client


_SYSTEM_PROMPT = """Ты — профессиональный аналитик предсказательных рынков (prediction markets).

Твоя задача — глубокий фундаментальный анализ маркета Polymarket:
1. Оценить, правильно ли рынок оценивает вероятность события.
2. Найти неэффективность: где рынок переплачивает или недоплачивает.
3. Объяснить КОНКРЕТНО, почему событие произойдёт или нет — с логикой и аргументами.
4. Описать оба сценария (YES и NO) с триггерами.
5. Выявить главный риск для рекомендованной позиции.

Правила:
- Опирайся на свои знания о политике, экономике, крипторынке, спорте, геополитике.
- Используй базовые ставки (base rates): как часто такие события происходят исторически.
- Оценивай смещение рынка: участники часто переоценивают драматичные события и недооценивают стабильность.
- Учитывай временной горизонт: чем дальше дата закрытия — тем выше неопределённость.
- Отвечай ТОЛЬКО валидным JSON. Язык всех полей: русский."""


def _build_prompt(data: dict) -> str:
    desc = data.get("description", "").strip()
    cat  = data.get("category", "").strip()

    extra = ""
    if cat:
        extra += f"Категория: {cat}\n"
    if desc and len(desc) > 20:
        # Обрезаем описание до разумного размера
        extra += f"Условия разрешения: {desc[:600]}\n"

    days_left = ""
    end = data.get("end_date", "")
    if end and end != "неизвестно":
        try:
            from datetime import date
            end_d = date.fromisoformat(end[:10])
            delta = (end_d - date.today()).days
            if delta >= 0:
                days_left = f" (осталось {delta} дней)"
        except Exception:
            pass

    return (
        f"Маркет: {data['question']}\n"
        f"YES: {data['yes_price'] * 100:.1f}%  (${data['yes_price']:.3f})\n"
        f"NO:  {data['no_price']  * 100:.1f}%  (${data['no_price']:.3f})\n"
        f"Объём 24ч:    ${data.get('volume_24h', 0):,.0f}\n"
        f"Ликвидность:  ${data.get('liquidity', 0):,.0f}\n"
        f"Закрытие:     {end}{days_left}\n"
        + extra +
        "\n"
        "Проведи ГЛУБОКИЙ анализ. Ответь ТОЛЬКО валидным JSON (без markdown, без ```):\n"
        "{\n"
        '  "recommendation":      "BUY YES" | "BUY NO" | "SKIP",\n'
        '  "confidence":          "HIGH" | "MEDIUM" | "LOW",\n'
        '  "edge":                "~X% преимущества или 0%",\n'
        '  "risk":                "HIGH" | "MEDIUM" | "LOW",\n'
        '  "main_thesis":         "2-4 предложения: ПОЧЕМУ именно эта сторона выгодна — '
        'с конкретными аргументами, логикой, базовыми ставками",\n'
        '  "probability_verdict": "Рынок [переоценивает YES / недооценивает YES / '
        'справедливо оценён] потому что ...",\n'
        '  "yes_scenario":        "Что конкретно должно произойти чтобы YES выиграл",\n'
        '  "no_scenario":         "Что конкретно должно произойти чтобы NO выиграл",\n'
        '  "key_risk":            "Главный риск для рекомендованной ставки"\n'
        "}"
    )


async def analyze_with_groq(data: dict) -> dict:
    """Глубокий анализ через Groq llama-3.3-70b. Бросает исключение при ошибке."""
    client = _get_client()
    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": _build_prompt(data)},
        ],
        temperature=0.3,
        max_tokens=900,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    result = json.loads(raw)

    # Обратная совместимость: поле "reasoning" для старого кода
    if "reasoning" not in result and "main_thesis" in result:
        result["reasoning"] = result["main_thesis"]

    return result
