#!/bin/sh
# Solo escribir desde env var si el archivo no existe o está vacío
if [ ! -s /app/service_account.json ] && [ -n "$GOOGLE_SERVICE_ACCOUNT_B64" ]; then
    python3 -c "
import base64, os
b64 = os.environ['GOOGLE_SERVICE_ACCOUNT_B64'].strip()
b64 += '=' * (-len(b64) % 4)
open('/app/service_account.json', 'w').write(base64.b64decode(b64).decode('utf-8'))
"
    echo "[entrypoint] service_account.json escrito desde env var"
else
    echo "[entrypoint] usando service_account.json existente"
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
