FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Playwright needs a few system libraries for Chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Chromium is only needed for the GitHub OAuth capture (app/services/oauth.py)
# and for unattended session renewal. It is off by default because it costs
# roughly 400 MB; accounts can always be added by pasting the cookie string
# instead. Turn it on with --build-arg INSTALL_BROWSER=true when you need
# the OAuth flow or automatic renewal.
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        playwright install chromium; \
    fi

COPY app ./app
COPY tools ./tools
COPY scripts ./scripts

VOLUME ["/app/data", "/app/.browser-profile"]
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
