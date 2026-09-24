# Mock healthcare app (all roles). stdlib only -> no pip install needed.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ROOT=/app PORT=8080
RUN useradd -u 10001 -m app
# Copy exactly apps/src -> /app so the runtime hashes match k8s/generated/known-good-hashes.json
COPY --chown=10001:10001 src/ /app/
USER 10001
WORKDIR /app
EXPOSE 8080
CMD ["python", "app.py"]
