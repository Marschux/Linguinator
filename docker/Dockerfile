FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# One tesseract package per CORE_LANGUAGES entry: without its traineddata a language cannot be
# read at all, and OCR is supposed to cover what the UI offers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates poppler-utils tesseract-ocr fonts-dejavu-core fonts-noto-core fonts-noto-cjk \
       tesseract-ocr-eng tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa tesseract-ocr-ita \
       tesseract-ocr-nld tesseract-ocr-por tesseract-ocr-pol tesseract-ocr-rus tesseract-ocr-ukr \
       tesseract-ocr-ces tesseract-ocr-swe tesseract-ocr-dan tesseract-ocr-fin tesseract-ocr-ell \
       tesseract-ocr-hun tesseract-ocr-bul tesseract-ocr-slk tesseract-ocr-est tesseract-ocr-ara \
       tesseract-ocr-chi-sim tesseract-ocr-jpn tesseract-ocr-heb tesseract-ocr-hin \
       tesseract-ocr-vie tesseract-ocr-ind tesseract-ocr-tur tesseract-ocr-sqi \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.7.1 \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 5051

CMD ["python", "-m", "app.server"]
