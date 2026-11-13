FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENT_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --no-create-home agent \
    && mkdir /data \
    && chown agent:agent /data

COPY --chown=agent:agent app ./app
COPY --chown=agent:agent tests ./tests
USER agent:agent

CMD ["python", "-m", "app.serve"]
