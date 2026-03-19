# Trading Engine — Multi-stage Docker Build
# 동일 이미지, EXCHANGE 환경변수로 CK/BF 분기

# Stage 1: Dependencies + Test
FROM python:3.12-slim AS test

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 의존성 설치
COPY trading-engine/requirements.txt .
RUN pip install --upgrade pip setuptools wheel && pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY trading-engine/core ./core
COPY trading-engine/adapters ./adapters
COPY trading-engine/api ./api
COPY trading-engine/main.py .
COPY trading-engine/pyproject.toml .
COPY trading-engine/tests ./tests

# 테스트 실행 (API 키 불필요 테스트만)
RUN echo "Running tests..." && \
    python -m pytest tests/ -v --tb=short -m "not requires_api_key" || \
    (echo "Tests failed! Build aborted." && exit 1)

# Stage 2: Production
FROM python:3.12-slim AS production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY trading-engine/requirements.txt .
RUN pip install --upgrade pip setuptools wheel && pip install --no-cache-dir -r requirements.txt

# 소스 복사 (테스트 제외)
COPY trading-engine/core ./core
COPY trading-engine/adapters ./adapters
COPY trading-engine/api ./api
COPY trading-engine/main.py .
COPY trading-engine/pyproject.toml .

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=info

# 포트는 EXCHANGE에 따라 다름 (docker-compose에서 지정)
EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
