FROM python:3.12-alpine
WORKDIR /app
COPY app/ /app/
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8099
CMD ["python", "/app/server.py"]
