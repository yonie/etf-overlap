FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="ETF Overlap Analyzer"
LABEL org.opencontainers.image.description="Analyze overlap between ETFs"
LABEL org.opencontainers.image.version="1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV FLASK_DEBUG=false

RUN groupadd --gid 1000 etf && \
    useradd --uid 1000 --gid etf --shell /bin/false --create-home etf

WORKDIR /app

COPY etf_web/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip cache purge

COPY etf_overlap.py .
COPY isin_normalizer.py .
COPY etf_web/ etf_web/

RUN mkdir -p /app/data && \
    chown -R etf:etf /app && \
    chmod 750 /app/data

USER etf

EXPOSE 3003

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3003/health')" || exit 1

CMD ["python", "etf_web/app.py"]