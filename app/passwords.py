import base64
import hashlib
import secrets
import hmac

import bcrypt


def _to_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _from_base64url(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=64,
    )
    return f"scrypt${_to_base64url(salt)}${_to_base64url(derived_key)}"


def _verify_scrypt(password: str, salt_value: str, hash_value: str) -> bool:
    if not salt_value or not hash_value:
        return False
    try:
        expected = _from_base64url(hash_value)
    except Exception:
        return False
    for salt in (_from_base64url(salt_value), salt_value.encode("utf-8")):
        derived_key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            dklen=len(expected),
        )
        if hmac.compare_digest(derived_key, expected):
            return True
    return False


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Verifica contra scrypt (hash propio, formato `scrypt$salt$hash`) o
    bcrypt (hash importado tal cual desde `auth.users.encrypted_password` de
    Supabase durante la migracion de contraseñas, sin re-hashear). El prefijo
    del hash decide el algoritmo -- nunca hace falta saber de antemano cual es."""
    if not stored_hash:
        return False

    if stored_hash.startswith("scrypt$"):
        try:
            _, salt_value, hash_value = stored_hash.split("$", 2)
        except ValueError:
            return False
        return _verify_scrypt(password, salt_value, hash_value)

    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except (ValueError, Exception):
            return False

    return False
