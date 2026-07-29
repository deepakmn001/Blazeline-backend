FROM python:3.13-slim

# Prevent Python from writing pyc files
ENV PYTHONDONTWRITEBYTECODE=1

# Flush logs immediately
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System packages
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Collect static
RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3"]