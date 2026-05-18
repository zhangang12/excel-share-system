#!/bin/bash
# 进入数据库 psql 交互式 shell
# 用法:  bash db-shell.sh
#        bash db-shell.sh -c "SELECT count(*) FROM projects;"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

DB_USER=$(grep -E '^POSTGRES_USER=' .env.prod 2>/dev/null | cut -d= -f2)
DB_NAME=$(grep -E '^POSTGRES_DB=' .env.prod 2>/dev/null | cut -d= -f2)
DB_USER="${DB_USER:-pms_prod}"
DB_NAME="${DB_NAME:-pms}"

if [[ -t 0 && $# -eq 0 ]]; then
    exec docker compose -f docker-compose.prod.yml --env-file .env.prod exec postgres psql -U "$DB_USER" "$DB_NAME"
else
    exec docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T postgres psql -U "$DB_USER" "$DB_NAME" "$@"
fi
