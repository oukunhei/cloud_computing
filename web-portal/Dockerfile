FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY web-portal/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn

# Copy project files
COPY . /app/
RUN chmod +x /app/web-portal/entrypoint.sh /app/onboard-team.sh

ENV KUBECONFIG=/app/kubeconfig
ENV FLASK_APP=web-portal/app.py
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["bash", "/app/web-portal/entrypoint.sh"]
