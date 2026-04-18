ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache python3 py3-pip \
 && python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir --upgrade pip

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

WORKDIR /app
COPY app /app

# HA addons run as root by Supervisor convention: /data/options.json is
# mounted root-owned and the addon user must be able to read it. Dropping
# privileges needs an s6 init script that chowns /data first; see #14.

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1:8000/healthz || exit 1

CMD ["python", "main.py"]
