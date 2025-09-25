FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install Python deps (кэшируется если requirements.txt не изменился)
COPY telegram_bot/requirements.txt ./telegram_bot/requirements.txt
RUN pip install --no-cache-dir -r telegram_bot/requirements.txt

# Copy project (копируется только если код изменился)
COPY . .

# Run bot
CMD ["python", "-m", "telegram_bot.main"]


