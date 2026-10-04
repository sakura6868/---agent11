FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAG_USE_ST=0

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data/uploads /app/data/chunks

EXPOSE 8000

# 端口自适应：本地/Render 用 8000，Railway 用其注入的 $PORT，避免端口不匹配
CMD ["sh", "-c", "python -m uvicorn api:app --app-dir src --host 0.0.0.0 --port ${PORT:-8000}"]
