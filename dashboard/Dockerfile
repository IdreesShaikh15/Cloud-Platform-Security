FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PORT=8090
RUN useradd -u 10003 -m dash
WORKDIR /opt/dashboard
COPY server.py index.html ./
USER 10003
EXPOSE 8090
CMD ["python", "server.py"]
