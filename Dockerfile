FROM python:3.11-slim-bookworm AS builder
RUN apt-get update && apt-get install -y build-essential cmake g++
COPY requirements.txt setup.py pyproject.toml /build/
COPY src /build/src
COPY python /build/python
WORKDIR /build
RUN pip install pybind11 && pip wheel . -w /wheels --no-deps

FROM python:3.11-slim-bookworm
COPY --from=builder /wheels /wheels
RUN pip install /wheels/*.whl && pip install fastapi uvicorn requests click
RUN mkdir -p /data
EXPOSE 8000
ENV AV_DATA_DIR=/data
CMD ["uvicorn", "av_server.server:app", "--host", "0.0.0.0", "--port", "8000"]
