FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/forecastfoundry/.local/bin:${PATH}"

RUN useradd --create-home --shell /usr/sbin/nologin forecastfoundry \
    && install -d -o forecastfoundry -g forecastfoundry /app /data
WORKDIR /app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels forecastfoundry \
    && rm -rf /wheels
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config

USER forecastfoundry
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
