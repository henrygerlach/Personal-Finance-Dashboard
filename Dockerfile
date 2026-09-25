FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false POETRY_NO_INTERACTION=1

# Without --all-extras: the Comdirect client is an optional extra from a private
# repository over SSH, so the image builds with no credentials. Only the live
# account balances are missing without it.
RUN pip install --no-cache-dir poetry
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root && rm -rf /root/.cache

COPY app ./app
COPY scripts ./scripts
RUN mkdir -p logs backups

EXPOSE 8000
# --proxy-headers: tailscale serve terminates HTTPS in front of this.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
