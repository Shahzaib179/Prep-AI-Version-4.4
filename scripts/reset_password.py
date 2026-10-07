"""Owner tool: set a new password for an account (there is no e-mail reset in this free-tier build).

    export DATABASE_URL="postgresql://..."        # or leave empty for the local SQLite file
    python scripts/reset_password.py student@example.com            # prompts for the new password (hidden)
"""
import getpass, pathlib, sys, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import streamlit  # noqa: F401
except Exception:
    m = types.ModuleType("streamlit"); m.secrets = {}; m.session_state = {}; sys.modules["streamlit"] = m

import auth, db  # noqa: E402

if len(sys.argv) != 2:
    sys.exit(__doc__)
db.init_db()
pw = getpass.getpass("New password: ")
if pw != getpass.getpass("Repeat: "):
    sys.exit("Passwords do not match.")
try:
    print("Password updated; all sessions signed out." if auth.set_password_admin(sys.argv[1], pw) else "No account with that email.")
except auth.AuthError as exc:
    sys.exit(str(exc))
