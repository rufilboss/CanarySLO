FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY canary_operator.py .

ENTRYPOINT ["kopf", "run", "--all-namespaces", "canary_operator.py"]