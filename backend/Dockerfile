FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + system deps (adds ~300MB)
RUN playwright install --with-deps chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "python server.py"]
