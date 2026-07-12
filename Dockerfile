FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# مسیر دیتابیس -- روی Railway حتماً یک Volume با Mount Path دقیقاً همین مسیر (/data) وصل کن
# تا اطلاعات با هر ری‌دیپلوی از بین نره. روی docker-compose محلی هم همین مسیر با یک
# پوشه‌ی لوکال (./data) نگه داشته میشه. توجه: دستور VOLUME داکر عمداً اینجا نیست، چون
# Railway با اون دستور کار نمی‌کنه و می‌گه از Volume خودش استفاده کن.
ENV DB_PATH=/data/hesabkitab.db

CMD ["python", "bot.py"]
