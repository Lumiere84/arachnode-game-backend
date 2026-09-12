FROM python:3.12-slim

RUN useradd --create-home --uid 1000 gameapp
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /data && chown -R gameapp:gameapp /data /app
USER gameapp

ENV GAME_DATABASE_PATH=/data/game.db
EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "app:app"]

