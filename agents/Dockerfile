FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python scripts/extract_showdown_data.py --output-dir /app/data

CMD ["python", "-u", "queue_worker.py"]
