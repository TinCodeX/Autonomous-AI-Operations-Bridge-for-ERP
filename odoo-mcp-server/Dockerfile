FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install build dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications first to leverage Docker layer caching
COPY requirements.txt .

RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt

# Copy source code
COPY . .

# Launch the agent (runs server.py via stdio internally)
CMD ["python", "telegram_agent.py"]
