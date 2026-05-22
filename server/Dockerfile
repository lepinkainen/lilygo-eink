FROM python:3.12-slim

# uv is the fastest way to resolve+install Pillow/numpy/requests.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps first so layer is cached across code edits.
COPY pyproject.toml ./
RUN uv sync --no-dev

# App code + assets.
COPY app.py metno.py ha.py render.py ./
COPY fonts ./fonts
COPY icons ./icons

# config.toml is bind-mounted from the host at /app/config.toml.
EXPOSE 8080

ENV PYTHONUNBUFFERED=1
CMD ["uv", "run", "--no-sync", "app.py"]
