FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir -r requirements.lock && python -m pip install --no-cache-dir --no-deps . && useradd --system --uid 10001 --home /app sb && mkdir -p /data && chown sb:sb /data

USER sb
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "svoibumagi", "serve", "--host", "0.0.0.0", "--data-dir", "/data"]
