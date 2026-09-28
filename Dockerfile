FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for uploads, outputs, database
RUN mkdir -p uploads outputs

# Expose port
EXPOSE 5000

# Environment variables
ENV FLASK_APP=AltText.py
ENV PYTHONUNBUFFERED=1

# Run with Gunicorn (threads & 3600s / 1 hour timeout for large batch processing)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "3600", "AltText:app"]
