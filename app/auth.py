import secrets

import bcrypt

SESSION_KEY = "user_id"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# Checked against on unknown usernames so login timing doesn't reveal which names exist.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
