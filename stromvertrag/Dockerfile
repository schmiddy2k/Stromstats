FROM python:3.12-alpine
RUN pip install --no-cache-dir 'websocket-client>=1.8,<2'
WORKDIR /app
COPY app/ /app/
COPY integration/stromvertrag_price/ /integration/stromvertrag_price/
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8099
CMD ["python", "/app/server.py"]
