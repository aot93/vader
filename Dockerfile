# Vader — Tape Archive Control Application
# For the "real" hardware backend the container must run on the Linux VM that
# owns the DDA-attached HBA, with --privileged (or explicit --device passthrough
# for /dev/sg* and /dev/nst*) and the mtx / ltfs / mt-st toolchain available.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Tape toolchain — only exercised when HARDWARE_BACKEND=real.
RUN apt-get update && apt-get install -y --no-install-recommends \
        mt-st mtx sg3-utils ltfs lsscsi \
    && rm -rf /var/lib/apt/lists/* || true

WORKDIR /opt/vader
COPY requirements.lock.txt .
RUN pip install -r requirements.lock.txt

COPY . .

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
