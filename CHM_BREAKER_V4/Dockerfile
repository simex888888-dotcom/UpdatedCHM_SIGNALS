FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота
COPY . .

# Директория для постоянной БД (монтируется как volume)
RUN mkdir -p /data

# DB хранится в /data — переживает любой редеплой
ENV DB_PATH=/data/chm_bot.db

CMD ["python3", "bot.py"]
