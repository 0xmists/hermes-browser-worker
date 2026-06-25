FROM mcr.microsoft.com/playwright/python:v1.54.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway assigns PORT dynamically; fallback to 8080
ENV PORT=8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "$PORT"]
