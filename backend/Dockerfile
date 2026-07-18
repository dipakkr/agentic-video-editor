# Backend image: FastAPI + agents + media tooling.
FROM python:3.11-slim

# ffmpeg/ffprobe are required for probing, proxies, and rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
# Install core + worker deps (media/ml extras are heavy; layer them in a separate image
# or install at runtime for GPU nodes).
RUN pip install --no-cache-dir -e ".[worker,llm]"
COPY ave ./ave

ENV AVE_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "ave.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
