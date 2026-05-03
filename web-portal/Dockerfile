FROM python:3.11-slim

WORKDIR /app

ARG TARGETARCH

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl at build time so tenant onboarding works even if runtime
# container networking cannot reach dl.k8s.io.
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64|arm64) ;; \
      *) echo "Unsupported architecture for kubectl: $arch" >&2; exit 1 ;; \
    esac; \
    kube_version="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${kube_version}/bin/linux/${arch}/kubectl"; \
    chmod 0755 /usr/local/bin/kubectl; \
    kubectl version --client=true

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
