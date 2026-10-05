FROM python:3.11

# Install dependencies
RUN apt-get update && \
    apt-get install -y nginx sqlite3 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Unprivileged user that runs the proxy processes (supervisord and the nginx master stay root)
RUN useradd --system --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt supervisor

# Copy app files
COPY src/ ./src/
COPY nginx.conf /etc/nginx/nginx.conf
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY scripts/ /app/scripts/
RUN chmod -R +x /app/scripts/


VOLUME /app/mlb_logos

# Add this before the COPY commands
RUN mkdir -p /app/mlb_logos
RUN mkdir -p /app/nfl_logos
# Must exist at build time: the app user can't create it under /app at runtime
RUN mkdir -p /app/nba_logos

# Add this after your COPY commands (assuming you have a local mlb_logos directory)
COPY mlb_logos/ /app/mlb_logos/
RUN chmod -R 755 /app/mlb_logos
COPY nfl_logos/ /app/nfl_logos/
RUN chmod -R 755 /app/nfl_logos


# Create cache directory and symlink for cross-platform compatibility
RUN mkdir -p /var/cache/timezone_proxy && \
    mkdir -p /opt/render/project/persistent/timezone_cache  # Render-specific path

# Symlink to Render's persistent disk if it exists, else use local path
RUN ln -sfn /opt/render/project/persistent/timezone_cache /var/cache/timezone_proxy || \
    echo "Running in non-Render environment; using local volume for cache."

EXPOSE 80

# Ensure no reload in production
ENV RELOAD=0

# Give the app user write access to the timezone cache (also fixes volumes created by older root-only images)
CMD ["sh", "-c", "chown -R app:app /var/cache/timezone_proxy/ || true; exec supervisord -c /etc/supervisor/conf.d/supervisord.conf"]
