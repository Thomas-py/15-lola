#!/bin/sh
# Si hay credenciales en base64, escribirlas al archivo antes de arrancar
if [ -n "$GOOGLE_SERVICE_ACCOUNT_B64" ]; then
    echo "$GOOGLE_SERVICE_ACCOUNT_B64" | base64 -d > /app/service_account.json
    echo "[entrypoint] service_account.json generado desde env var"
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
