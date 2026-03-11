"""
groq_analyzer.py — AI-анализ маркетов Polymarket через Groq API.

Использует OpenAI-совместимый API Groq (бесплатный ключ: console.groq.com).
Env var: GROQ_API_KEY=gsk_...

Формат входных данных analyze_with_groq(data):
  data = {
      "question":   str,
      "yes_price":  float,   # 0.0 – 1.0
      "no_price":   float,
      "volume_24h": float,
      "liquidity":  float,
      "end_date":   str,     # ISO или "неизвестно"
  }

Возвращает dict:
  {
      "recommendation": "BUY YES" | "BUY NO" | "SKIP",
      "confidence":     "HIGH" | "MEDIUM" | "LOW",
      "reasoning":      str,   # 2-3 предложения на русском
      "edge":           str,   # напр. "~7%" или "0%"
      "risk":           "HIGH" | "MEDIUM" | "LOW",
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
        )
    return _client


_SYSTEM_PROMPT = """Ты — эксперт по предсказательным рынкам Polymarket.
Анализируй кратко и точно. Отвечай строго в JSON без лишнего текста.
Учитывай текущую вероятность, объём торгов, ликвидность и дату закрытия.
Язык поля reasoning: русский."""


def _build_prompt(data: dict) -> str:
    return (
        f"Маркет: {data['question']}\n"
        f"YES: {data['yes_price'] * 100:.0f}%  (${data['yes_price']:.2f})\n"
        f"NO:  {data['no_price']  * 100:.0f}%  (${data['no_price']:.2f})\n"
        f"Объём 24h:    ${data.get('volume_24h', 0):,.0f}\n"
        f"Ликвидность:  ${data.get('liquidity', 0):,.0f}\n"
        f"Закрытие:     {data.get('end_date', 'неизвестно')}\n\n"
        'Ответь ТОЛЬКО валидным JSON (без markdown, без ```):\n'
        '{\n'
        '  "recommendation": "BUY YES" | "BUY NO" | "SKIP",\n'
        '  "confidence":     "HIGH" | "MEDIUM" | "LOW",\n'
        '  "reasoning":      "2-3 предложения на русском",\n'
        '  "edge":           "примерный % преимущества или 0%",\n'
        '  "risk":           "HIGH" | "MEDIUM" | "LOW"\n'
        '}'
    )


async def analyze_with_groq(data: dict) -> dict:
    """Запрашивает анализ у Groq llama-3.3-70b. Бросает исключение при ошибке."""
    client = _get_client()
    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": _build_prompt(data)},
        ],
        temperature=0.2,
        max_tokens=350,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    return json.loads(raw)
