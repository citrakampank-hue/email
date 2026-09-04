#!/bin/bash
set -e

echo "=== TempMail Bot Starting ==="
echo "Python version: $(python --version)"
echo "Working directory: $(pwd)"

# Check environment
if [ -z "$BOT_TOKEN" ]; then
    echo "ERROR: BOT_TOKEN not set"
    exit 1
fi

if [ -z "$DOMAIN" ]; then
    echo "ERROR: DOMAIN not set"
    exit 1
fi

if [ -z "$EMAIL_WEBHOOK_KEY" ]; then
    echo "WARNING: EMAIL_WEBHOOK_KEY not set, using random"
    export EMAIL_WEBHOOK_KEY=$(python -c "import secrets; print(secrets.token_hex(16))")
fi

# Initialize database
python -c "
from main import Database, Config
db = Database(Config.DATABASE_PATH)
print('Database initialized')
"

# Start application
echo "Starting bot..."
exec python main.py