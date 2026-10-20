FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# For the shop on Kubernetes: the agent runs catalog commands with kubectl.
ARG TARGETARCH
ADD --chmod=755 https://dl.k8s.io/release/v1.31.4/bin/linux/${TARGETARCH}/kubectl /usr/local/bin/kubectl

WORKDIR /app

# Install dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group agent-server --no-install-project

COPY langgraph.json ./
COPY src ./src
RUN uv sync --frozen --no-dev --group agent-server
