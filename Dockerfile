FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lead_bot.py .
COPY dashboard.html .
CMD ["python", "lead_bot.py"]
