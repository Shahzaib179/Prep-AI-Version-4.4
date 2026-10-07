#!/usr/bin/env bash
# Run the full database contract tests against YOUR staging Postgres. Needs: pip install -r requirements.txt
# Usage:  DATABASE_URL="postgresql://..." bash scripts/verify_postgres.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DATABASE_URL:?Set DATABASE_URL to your STAGING Postgres URL first}"
export CONTRACT_ALLOW_RESET=yes
python tests/test_db_contract.py
python tests/test_auth.py
echo "PASS: the database layer works on Postgres."
