FROM python:3.12-slim-bookworm AS builder
RUN apt-get update && apt-get install -y build-essential cmake g++
COPY requirements.txt setup.py pyproject.toml /build/
COPY src /build/src
COPY python /build/python
WORKDIR /build
RUN pip install pybind11 && pip wheel . -w /wheels --no-deps

# Runtime MUST match the builder's Python minor (cp-tag of the built wheel): a cp312 wheel
# is rejected by a 3.11 interpreter — this exact mismatch broke docker-edge and would have
# broken the release.yml image job at the next tag (Probleme.md #69). Keep them in sync.
FROM python:3.12-slim-bookworm
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && \
    pip install --no-cache-dir fastapi uvicorn requests click && \
    mkdir -p /data && chmod 777 /data
EXPOSE 8000
ENV AV_DATA_DIR=/data
CMD ["uvicorn", "av_server.server:app", "--host", "0.0.0.0", "--port", "8000"]
