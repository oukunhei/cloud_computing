FROM python:3.11-slim

WORKDIR /app

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.30.6
ARG KUBECTL_BASE_URL=https://dl.k8s.io/release

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates bash \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl at build time when the configured download endpoint is
# reachable. Do not fail the whole image build if the CDN is blocked; the
# portal can still start and the entrypoint has a runtime fallback.
RUN set -u; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64|arm64) ;; \
      *) echo "Unsupported architecture for kubectl: $arch" >&2; exit 1 ;; \
    esac; \
    kubectl_url="${KUBECTL_BASE_URL}/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    echo "Downloading kubectl from ${kubectl_url}"; \
    if curl -fSL --retry 3 --connect-timeout 10 --max-time 120 -o /usr/local/bin/kubectl "${kubectl_url}"; then \
        chmod 0755 /usr/local/bin/kubectl; \
        kubectl version --client=true; \
    else \
        echo "WARNING: Could not download kubectl during image build. The portal will still build."; \
        rm -f /usr/local/bin/kubectl; \
    fi

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
