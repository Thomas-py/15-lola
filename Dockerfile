FROM python:3.12-slim

WORKDIR /app

# Dependencias primero (cache layer)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código backend
COPY backend/ .

# Frontend servido por FastAPI
COPY frontend/ /frontend/

# Script de inicio
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p uploads

ENTRYPOINT ["/entrypoint.sh"]
