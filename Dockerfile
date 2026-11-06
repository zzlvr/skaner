FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY docstruc docstruc
COPY bot bot
COPY web web
RUN pip install --no-cache-dir ".[web]"

# The database lives in the volume; the site binds to 0.0.0.0 inside the container.
ENV WEB_DB_URL=sqlite:////data/web.sqlite3 WEB_HOST=0.0.0.0
VOLUME /data
EXPOSE 8000
RUN useradd --create-home skaner && mkdir -p /data && chown skaner /data
USER skaner
CMD ["python", "-m", "web"]
