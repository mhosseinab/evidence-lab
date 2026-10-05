FROM node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS dashboard
WORKDIR /workspace
RUN npm install --global pnpm@12.6.0
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/dashboard/package.json apps/dashboard/package.json
RUN pnpm install --frozen-lockfile
COPY tooling/typescript tooling/typescript
COPY apps/dashboard apps/dashboard
RUN pnpm --filter @evidence-lab/dashboard run build

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.17
COPY pyproject.toml uv.lock Taskfile.yml package.json pnpm-workspace.yaml pnpm-lock.yaml biome.json ./
COPY apps/evidence-lab apps/evidence-lab
COPY apps/dashboard apps/dashboard
COPY --from=dashboard /workspace/apps/dashboard/dist apps/dashboard/dist
COPY tooling tooling
COPY configs configs
COPY data data
COPY docs docs
COPY README.md ./
RUN uv sync --locked --all-packages --group dev --no-editable \
    && uv pip check \
    && useradd --create-home --uid 10001 evidence \
    && mkdir -p /app/artifacts /app/policies \
    && chown -R evidence:evidence /app/artifacts /app/policies
ENV PATH="/app/.venv/bin:$PATH"
USER evidence
EXPOSE 8000
CMD ["evidence-lab", "serve", "--config", "/app/configs/runtime.yaml"]
