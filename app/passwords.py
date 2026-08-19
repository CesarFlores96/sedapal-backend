import base64
import hashlib
import secrets
import hmac


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


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False

    try:
      algorithm, salt_value, hash_value = stored_hash.split("$", 2)
    except ValueError:
        return False

    if algorithm != "scrypt" or not salt_value or not hash_value:
        return False

    expected = _from_base64url(hash_value)
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
