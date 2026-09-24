# Resilience agent + centralized baseline controller (same image, different command).
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -u 10002 -m agent && mkdir -p /var/log/resilience /tmp/cr-sim && chown agent /var/log/resilience /tmp/cr-sim
WORKDIR /opt/agent
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY resilience/ ./resilience/
USER 10002
EXPOSE 50051 8081
ENTRYPOINT ["python", "-m", "resilience"]
CMD ["agent"]
