# Use the official lightweight Python image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GRADIO_SERVER_NAME="0.0.0.0" \
    GRADIO_SERVER_PORT=7860

WORKDIR /app

# Install system utilities needed during build
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and all their system dependencies (libraries for Chromium)
RUN playwright install --with-deps chromium

# Copy the application code
COPY . .

# Initialize the SQLite database schema
RUN python -c "import database; database.init_db('prices.db')"

# Expose the port Gradio runs on
EXPOSE 7860

# Start the application
CMD ["python", "app.py"]
