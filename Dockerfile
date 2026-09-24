FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY pyproject.toml .
COPY app app
COPY api api
COPY brokers brokers
COPY core core
COPY data data
COPY db db
COPY execution execution
COPY portfolio portfolio
COPY risk risk
COPY strategy strategy
COPY alembic alembic
COPY alembic.ini .
COPY deploy/entrypoint.sh /usr/local/bin/trading-service-entrypoint

RUN pip install --no-cache-dir .

RUN chmod 0555 /usr/local/bin/trading-service-entrypoint

ENTRYPOINT ["/usr/local/bin/trading-service-entrypoint"]
CMD ["python", "-m", "app"]
