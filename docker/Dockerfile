FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates poppler-utils tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 5051

CMD ["python", "-m", "app.server"]
