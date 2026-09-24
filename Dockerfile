# Use an official Python runtime as a parent image
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container.
# cursor-sdk is optional for a local venv; the image always includes it so
# LLM_PROVIDER=cursor works without a custom build. The local agent runtime
# is the bundled cursor-sdk-bridge binary, not the Cursor IDE.
COPY requirements.txt requirements-cursor.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cursor.txt \
    && python -c "from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions" \
    && cursor-sdk-bridge --help >/dev/null

# Copy the application code into the container
COPY --chown=10001:10001 bookclub/ ./bookclub/
COPY --chown=10001:10001 bookclub_bot.py .

# Run the application as a dedicated non-root uid/gid.
RUN groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app
USER bot

# Command to run the bot
CMD ["python", "bookclub_bot.py"]
