FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 PORT=8080 DATA_DIR=/data COOKIE_SECURE=true
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lab ./lab
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "-m", "lab.server"]
