FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG INSTALL_OCR=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && if [ "$INSTALL_OCR" = "true" ]; then apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng; fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 5051

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5051"]
