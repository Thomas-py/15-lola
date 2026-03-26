FROM python:3.12-slim

WORKDIR /app

# Dependencias primero (cache layer)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código backend
COPY backend/ .

# Frontend servido por FastAPI
COPY frontend/ /frontend/

RUN mkdir -p uploads

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
