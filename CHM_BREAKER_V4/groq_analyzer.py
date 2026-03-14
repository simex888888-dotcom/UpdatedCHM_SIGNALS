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
      "main_thesis":         str,           # главный тезис, 3-5 предл.
      "probability_verdict": str,           # переоценён / недооценён / справедлив
      "yes_scenario":        str,           # что должно случиться для YES
      "no_scenario":         str,           # что должно случиться для NO
      "key_risk":            str,           # главный риск для рекомендованной ставки
      "historical_base":     str,           # базовая ставка / исторический контекст
      "smart_money":         str,           # что говорит динамика объёма и ликвидности
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
            timeout=30.0,
        )
    return _client


_SYSTEM_PROMPT = """Ты — топовый аналитик предсказательных рынков с многолетним опытом.

Твоя задача — ГЛУБОКИЙ фундаментальный анализ маркета Polymarket. Думай как профессиональный трейдер, ищи неэффективности рынка.

ОБЯЗАТЕЛЬНО выполни все шаги:

1. **Базовая ставка** — как часто подобные события происходили исторически? Назови конкретные цифры или прецеденты.

2. **Оценка неэффективности** — рынок переоценивает или недооценивает? На сколько процентов? Почему участники ошибаются?

3. **Смарт-мани сигнал** — что говорит объём (высокий = сильное убеждение рынка) и ликвидность (низкая = малая уверенность)?

4. **Сценарии с триггерами** — опиши КОНКРЕТНЫЕ события, даты, условия, которые решат исход. Не общие слова, а факты.

5. **Ключевой риск** — что может внезапно изменить расчёт? Чёрный лебедь, скрытое условие в резолюции?

6. **Временной фактор** — учти оставшееся время. Близкий дедлайн = выше уверенность, дальний = больше неопределённость.

Правила:
- Мыслишь как Байс (Bayes): обновляй prior по имеющимся данным.
- Учитывай смещения рынка: толпа переоценивает яркие сенсационные события, недооценивает скучный статус-кво.
- Будь конкретен: называй имена, даты, цифры, законы, организации.
- Отвечай ТОЛЬКО валидным JSON без markdown и ```. Все текстовые поля — на русском языке."""


def _build_prompt(data: dict) -> str:
    desc = data.get("description", "").strip()
    cat  = data.get("category", "").strip()

    extra = ""
    if cat:
        extra += f"Категория: {cat}\n"
    if desc and len(desc) > 20:
        extra += f"Условия разрешения: {desc[:800]}\n"

    days_left = ""
    end = data.get("end_date", "")
    if end and end != "неизвестно":
        try:
            from datetime import date
            end_d = date.fromisoformat(end[:10])
            delta = (end_d - date.today()).days
            if delta >= 0:
                days_left = f" (осталось {delta} дн.)"
            else:
                days_left = f" (истёк {abs(delta)} дн. назад)"
        except Exception:
            pass

    vol   = data.get("volume_24h", 0)
    liq   = data.get("liquidity", 0)
    yes_p = data["yes_price"]
    no_p  = data["no_price"]

    # Сигнал ликвидности
    liq_signal = ""
    if liq > 0 and vol > 0:
        ratio = vol / liq if liq else 0
        if ratio > 0.5:
            liq_signal = "⚡ Высокая активность (объём > 50% ликвидности)"
        elif liq < 5000:
            liq_signal = "⚠️ Низкая ликвидность — осторожно"

    return (
        f"Маркет: {data['question']}\n"
        f"YES: {yes_p * 100:.1f}%  (${yes_p:.3f})\n"
        f"NO:  {no_p  * 100:.1f}%  (${no_p:.3f})\n"
        f"Объём 24ч:    ${vol:,.0f}\n"
        f"Ликвидность:  ${liq:,.0f}  {liq_signal}\n"
        f"Закрытие:     {end}{days_left}\n"
        + extra +
        "\n"
        "Проведи ГЛУБОКИЙ профессиональный анализ. Ответь ТОЛЬКО валидным JSON:\n"
        "{\n"
        '  "recommendation":      "BUY YES" | "BUY NO" | "SKIP",\n'
        '  "confidence":          "HIGH" | "MEDIUM" | "LOW",\n'
        '  "edge":                "~X% преимущества (твоя оценка вероятности vs цена рынка)",\n'
        '  "risk":                "HIGH" | "MEDIUM" | "LOW",\n'
        '  "main_thesis":         "3-5 предложений: ПОЧЕМУ именно эта сторона выгодна. '
        'Конкретные аргументы, логика, числа, факты. Не общие слова.",\n'
        '  "probability_verdict": "Точная оценка: рынок ставит X%, ты считаешь реальная вероятность Y% — '
        'потому что [конкретная причина с данными]",\n'
        '  "yes_scenario":        "КОНКРЕТНЫЕ триггеры для YES: что должно произойти, когда, '
        'какие события/решения/данные",\n'
        '  "no_scenario":         "КОНКРЕТНЫЕ триггеры для NO: что должно произойти, когда, '
        'какие события/решения/данные",\n'
        '  "key_risk":            "Главный скрытый риск: неочевидное условие резолюции, '
        'форс-мажор, ошибка в расчёте",\n'
        '  "historical_base":     "Базовая ставка: как часто подобное происходило исторически — '
        'конкретные прецеденты и цифры",\n'
        '  "smart_money":         "Что говорит объём и ликвидность о настроении рынка"\n'
        "}"
    )


async def analyze_with_groq(data: dict) -> dict:
    """Глубокий анализ через Groq. Бросает исключение при ошибке."""
    client = _get_client()
    response = await client.chat.completions.create(
        model="llama-3.3-70b-specdec",   # speculative decoding — быстрее versatile
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": _build_prompt(data)},
        ],
        temperature=0.25,
        max_tokens=1600,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    result = json.loads(raw)

    # Обратная совместимость
    if "reasoning" not in result and "main_thesis" in result:
        result["reasoning"] = result["main_thesis"]

    return result
