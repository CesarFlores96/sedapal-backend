"""Firma de manifests OTA (Expo Updates code signing).

Algoritmo fijo: RSA PKCS#1 v1.5 + SHA-256 (``rsa-v1_5-sha256``), el mismo que
genera y espera ``expo-updates codesigning:generate``. Verificado contra el
código fuente del cliente nativo instalado en el repo
(``node_modules/expo-updates/android/.../CodeSigningConfiguration.kt``):
``Signature.getInstance("SHA256withRSA")`` sobre los bytes exactos del cuerpo
de la parte multipart, comparado con ``Base64.decode(signature, DEFAULT)``
(base64 estandar, no url-safe).

La clave privada NUNCA vive dentro de este repo ni de D:\\Sedapal: se carga en
tiempo de arranque desde una ruta en disco fijada por ``OTA_SIGNING_PRIVATE_KEY_PATH``.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.config import get_settings

CODE_SIGNING_KEY_ID = "main"
CODE_SIGNING_ALGORITHM = "rsa-v1_5-sha256"


class OtaSigningNotConfigured(RuntimeError):
    pass


@lru_cache
def _load_private_key() -> rsa.RSAPrivateKey:
    settings = get_settings()
    key_path = settings.ota_signing_private_key_path
    if not key_path:
        raise OtaSigningNotConfigured(
            "OTA_SIGNING_PRIVATE_KEY_PATH no esta configurado."
        )

    private_key_bytes = Path(key_path).read_bytes()
    private_key = serialization.load_pem_private_key(private_key_bytes, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise OtaSigningNotConfigured(
            "La clave en OTA_SIGNING_PRIVATE_KEY_PATH no es una clave privada RSA."
        )
    return private_key


def sign_manifest_bytes(body: bytes) -> str:
    """Firma ``body`` (los bytes EXACTOS que se envian en la parte multipart)
    y devuelve el valor listo para el header ``expo-signature``."""

    private_key = _load_private_key()
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    signature_b64 = base64.b64encode(signature).decode("ascii")
    return f'sig="{signature_b64}", keyid="{CODE_SIGNING_KEY_ID}"'
