FROM python:3.12-slim
WORKDIR /app
COPY services/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services /app/services
COPY contracts /app/contracts
ENV PYTHONPATH=/app/services
