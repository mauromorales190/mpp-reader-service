# mpp-reader MCP server (SSE transport for remote clients like n8n)
# ------------------------------------------------------------------
# Build:   docker build -f mcp/Dockerfile -t mpp-reader-mcp .
# Run:     docker run -p 8765:8765 mpp-reader-mcp
# ------------------------------------------------------------------
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY skill_scripts /app/skill_scripts
COPY mcp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# OPTIONAL: enable native .mpp output via Aspose.Tasks (commercial).
# RUN pip install --no-cache-dir aspose-tasks
# COPY aspose-license.lic /app/aspose-license.lic
# ENV ASPOSE_LICENSE_PATH=/app/aspose-license.lic

COPY mcp/server.py /app/server.py

ENV MPP_SCRIPTS_DIR=/app/skill_scripts \
    PYTHONUNBUFFERED=1

EXPOSE 8765
CMD ["python3", "/app/server.py", "--transport", "sse", "--host", "0.0.0.0", "--port", "8765"]
