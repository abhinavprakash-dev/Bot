FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libgl1 \
    libopus0 \
    libsodium23 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py music_panel.py ./
COPY cogs/ ./cogs/
COPY botcore/ ./botcore/
COPY assets/kryo-embed/ ./assets/kryo-embed/
COPY assets/jtc/ ./assets/jtc/
COPY assets/antinuke_premium_preview.gif assets/protections_premium_preview.gif assets/protections_embed_preview.png ./assets/

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]