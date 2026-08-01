# --- CEMA cUAS Backend image ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (bcrypt/cryptography build)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8001

# NOTE: intentionally single-worker (no --workers). server.py holds
# safety-critical in-process globals (_tx_halted, _arm_tokens,
# _range_authorization, _jam_confirm_tokens, _pending_acks, ingest-health
# tracking dicts) that are NOT shared across processes. Multiple uvicorn
# workers would let e.g. worker A create an arm-token or flip _tx_halted
# while worker B (handling the very next request, load-balanced round-robin
# by uvicorn/the OS) never sees it -- silently breaking the TX-halt/arm/jam/
# gnss-spoof confirm safety gates. Do not add --workers until those globals
# are migrated to a shared store (Redis/Mongo).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"]
