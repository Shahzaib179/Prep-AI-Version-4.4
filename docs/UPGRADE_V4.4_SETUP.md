# Prep AI v4.4 - permanent data, real accounts, AI resilience

## What changed
| Area | Before (v4.3) | Now (v4.4) |
|---|---|---|
| Storage | SQLite file + FAISS files, lost on every cloud redeploy | `DATABASE_URL` -> hosted Postgres (Supabase/Neon, free tiers). SQLite stays as the local/offline default |
| Memory vectors | files under `data/memory/` | stored in the database (`memory_vectors`), index rebuilt on login; exact same search results |
| Login | Student name + ID (anyone could type any ID); one shared tutor code | Email + password accounts, scrypt hashing, 5-failure lockout (15 min), change password |
| Tutors | one shared identity, saw every student | own account (invite code), sees only their own quizzes and the students who took them |
| Groq | one call, any error shown to the user | retry + backoff + Retry-After, backup model, concurrency limit, safe messages |
| Schema | ad-hoc `_ensure_column` | versioned migrations (`migrations.py`, table `schema_migrations`) |

## One-time setup (about 15 minutes)
1. **Create the database** (free): Supabase -> New project -> Project Settings -> Database -> Connection string -> *Transaction pooler* (port 6543). Replace `[YOUR-PASSWORD]`.
   Neon works too (use its pooled connection string).
2. **Add secrets** (Streamlit Cloud -> App -> Settings -> Secrets). Never put these in GitHub:
   ```toml
   GROQ_API_KEY = "..."
   TUTOR_ACCESS_CODE = "a-long-private-invite-code"
   DATABASE_URL = "postgresql://postgres.xxxx:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres"
   ```
3. **Check the database layer on a STAGING database** (a separate empty project, not production):
   ```bash
   pip install -r requirements.txt
   DATABASE_URL="postgresql://..." bash scripts/verify_postgres.sh      # must print PASS
   ```
4. **Copy your existing data** (only if you want to keep v4.3 data):
   ```bash
   cp data/prep_ai.db data/prep_ai.backup.db                              # backup first
   DATABASE_URL="postgresql://..." python scripts/migrate_sqlite_to_postgres.py --source data/prep_ai.db --dry-run
   DATABASE_URL="postgresql://..." python scripts/migrate_sqlite_to_postgres.py --source data/prep_ai.db
   ```
   The script is safe to repeat and ends with a row-count check per table.
5. Deploy. Existing students: **Create student account -> enter your old Student ID** to keep your history.
   The first tutor account you create adopts quizzes published under the old shared tutor code.

## Everyday operations
* Forgot password: `DATABASE_URL=... python scripts/reset_password.py user@email.com` (owner only).
* Rollback: remove `DATABASE_URL` from the secrets and redeploy the previous version (it uses SQLite again).
* Backups: Supabase free tier has no point-in-time backup - export periodically (`pg_dump "$DATABASE_URL" > backup.sql`).

## Honest limits
* Postgres was **not** executed in the build sandbox (no Postgres server / PyPI there). All logic is tested on SQLite, the SQL is kept portable and
  checked by a source scan, and `scripts/verify_postgres.sh` runs the same tests on your Postgres. Run it before going live.
* Keeping users signed in after a browser refresh (`ENABLE_PERSISTENT_LOGIN`) is experimental and off by default: it needs a browser test.
* Passwords use scrypt (Python standard library) instead of argon2id: strong, memory-hard, and nothing extra to install.
* An old Student ID can be claimed by whoever registers first with it (v4.3 had no passwords to prove ownership).
* Free tiers can pause or change limits; check your provider's current terms.

## Troubleshooting: "Prep AI cannot reach its database" / PoolTimeout
The app now prints the reason. The usual causes on Streamlit Cloud:
1. **Direct connection string used.** Supabase's `db.PROJECT_REF.supabase.co` host is IPv6-only and Streamlit Cloud has no IPv6. Use the **Transaction pooler** string (host `...pooler.supabase.com`).
2. **Wrong user.** On the pooler the user is `postgres.PROJECT_REF`, not `postgres`.
3. **Special characters in the password** (`@ : / # ? %`) must be URL-encoded (`@` -> `%40`).
4. **Paused project** (free Supabase projects pause after inactivity) - restore it in the dashboard.
