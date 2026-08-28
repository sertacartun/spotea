import secrets

import bcrypt

SESSION_KEY = "user_id"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# A real bcrypt hash of a value nobody will ever type, computed once at
# import time so it costs nothing per request. routers/auth.py's login_submit
# checks the submitted password against this whenever the username doesn't
# match any user, instead of skipping the check entirely — measured at 6.0ms
# (no user found) vs 419.8ms (a real mismatch) before this existed, a 70x
# gap that told an attacker whether a name was registered through timing
# alone, defeating the deliberately generic "Invalid username or password".
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
