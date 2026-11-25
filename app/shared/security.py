import hashlib
import os

from itsdangerous import URLSafeTimedSerializer
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")


def _hash_per_bcrypt(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return pwd_context.hash(_hash_per_bcrypt(password))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(_hash_per_bcrypt(plain_password), hashed_password)


def generate_verification_token(email: str) -> str:
    from flask import current_app

    serializer = URLSafeTimedSerializer(current_app.config["JWT_SECRET_KEY"])
    return serializer.dumps(email, salt=os.environ.get("EMAIL_OTP_SALT"))


def verify_token(token: str, expiration_seconds: int = 3600):
    from flask import current_app

    serializer = URLSafeTimedSerializer(current_app.config["JWT_SECRET_KEY"])
    try:
        return serializer.loads(
            token,
            salt=os.environ.get("EMAIL_OTP_SALT"),
            max_age=expiration_seconds,
        )
    except Exception:
        return None
