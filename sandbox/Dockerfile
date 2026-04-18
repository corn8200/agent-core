FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_MAJOR=20

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git tini \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get purge -y gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit @anthropic-ai/claude-code@2.1.114

RUN pip install --no-cache-dir \
      'claude-agent-sdk>=0.1.50' \
      'anthropic>=0.39' \
      'requests>=2.31' \
      'beautifulsoup4>=4.12' \
      'feedparser>=6.0' \
      'pushover-complete>=1.1'

RUN groupadd --gid 1001 agent \
 && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash agent \
 && mkdir -p /app /home/agent/.claude \
 && chown -R agent:agent /home/agent /app

USER agent
WORKDIR /app

ENV HOME=/home/agent \
    PATH=/usr/local/bin:/usr/bin:/bin:/home/agent/.local/bin \
    ANTHROPIC_API_KEY="" \
    ANTHROPIC_AUTH_TOKEN=""

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["claude", "--version"]
