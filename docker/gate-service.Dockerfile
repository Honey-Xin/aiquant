# 守门服务镜像（执行域辅助）：只做健康检查与 Freqtrade 暂停/恢复，不下单
FROM python:3.12.8-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src
COPY config /app/config

ENV PYTHONPATH=/app/src \
    GATE_PORT=8080

RUN useradd --create-home --uid 10002 aiquant && mkdir -p /app/logs && chown -R aiquant:aiquant /app
USER aiquant

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "aiquant.gate.service"]
