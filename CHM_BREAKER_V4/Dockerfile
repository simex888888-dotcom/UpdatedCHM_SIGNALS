FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код бота
COPY . .

# По умолчанию БД хранится в рабочей директории /app.
# На хостингах с persistent volume задай переменную окружения DB_PATH:
#   DB_PATH=/persistent/chm_bot.db  (путь к постоянному диску хостинга)
ENV DB_PATH=/app/chm_bot.db

CMD ["python3", "bot.py"]
