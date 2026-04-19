# mpp-reader REST API
# ------------------------------------------------------------------
# Build:   docker build -t mpp-reader-api .
# Run:     docker run -p 8080:8080 -e MPP_API_KEY=changeme mpp-reader-api
# ------------------------------------------------------------------
FROM python:3.11-slim

# Java is required by MPXJ (JPype bridge to the bundled JAR)
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-jre-headless \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Skill scripts are mounted at /app/skill_scripts
COPY skill_scripts /app/skill_scripts

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# OPTIONAL: enable native .mpp output via Aspose.Tasks (requires a commercial license).
# Uncomment and place your license file at /app/aspose-license.lic before building.
# RUN pip install --no-cache-dir aspose-tasks
# COPY aspose-license.lic /app/aspose-license.lic
# ENV ASPOSE_LICENSE_PATH=/app/aspose-license.lic

COPY api/server.py /app/server.py

ENV MPP_SCRIPTS_DIR=/app/skill_scripts \
    MPP_MAX_UPLOAD_MB=50 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
