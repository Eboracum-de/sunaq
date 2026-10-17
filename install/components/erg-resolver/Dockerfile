FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONPATH=/app

RUN groupadd --gid 10002 resolver \
    && useradd --uid 10002 --gid 10002 --create-home --home-dir /home/resolver --shell /usr/sbin/nologin resolver

WORKDIR /app

COPY requirements-resolver.txt /tmp/requirements-resolver.txt
RUN pip install --no-cache-dir -r /tmp/requirements-resolver.txt

COPY erg_resolver/ /app/erg_resolver/

USER resolver

CMD ["python", "-m", "uvicorn", "erg_resolver.app:app", "--host", "127.0.0.1", "--port", "8770"]
