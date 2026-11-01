# AI 标签域镜像（研究域）：无下单权限、无 OKX 私钥
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
    LABEL_PORT=8081

# 该镜像不接收 OKX 私钥：compose 里只注入只读相关变量。
RUN useradd --create-home --uid 10001 aiquant && mkdir -p /app/logs && chown -R aiquant:aiquant /app
USER aiquant

EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "aiquant.label_service.main"]
