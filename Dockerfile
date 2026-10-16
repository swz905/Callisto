FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY kairos ./kairos
COPY agents ./agents
COPY queries ./queries
COPY sim ./sim
RUN pip install --no-cache-dir -e .
EXPOSE 8000
