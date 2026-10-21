"""OpenAI ID-token verification; access tokens remain opaque bearer values."""

from dataclasses import dataclass
import secrets
from urllib.parse import urlsplit

from .transport import request_json


ISSUER = "https://auth.openai.com"


class IdentityError(Exception):
    pass


@dataclass(frozen=True, repr=False)
class Identity:
    subject: str
    account_id: str


def verify_identity(encoded, client_id, *, nonce=None, subject=None, request=None):
    try:
        import jwt
    except ImportError:
        raise IdentityError("Identity verification requires the dependencies in requirements.txt.") from None
    request = request or request_json
    metadata = request(ISSUER + "/.well-known/openid-configuration")
    try:
        url = metadata.get("jwks_uri")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (metadata.get("issuer") != ISSUER or parsed is None or parsed.scheme != "https"
                or parsed.hostname != "auth.openai.com" or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError("untrusted issuer or key source")
        header = jwt.get_unverified_header(encoded)
        algorithm, kid = header.get("alg"), header.get("kid")
        if algorithm not in {"RS256", "ES256"} or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
            raise ValueError("unsupported signing key")
        published = request(url).get("keys")
        if not isinstance(published, list) or any(not isinstance(key, dict) for key in published):
            raise ValueError("invalid keys")
        matches = [key for key in published if key.get("kid") == kid and key.get("use", "sig") == "sig"
                   and key.get("alg", algorithm) == algorithm]
        if len(matches) != 1:
            raise ValueError("missing or ambiguous key")
        key = jwt.PyJWK.from_dict(matches[0], algorithm=algorithm).key
        claims = jwt.decode(encoded, key, algorithms=[algorithm], issuer=ISSUER, audience=client_id,
                            options={"require": ["iss", "aud", "sub", "exp", "iat"]})
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("nonce mismatch")
        current_subject = claims.get("sub")
        if not isinstance(current_subject, str) or not current_subject or (subject is not None and current_subject != subject):
            raise ValueError("identity changed")
        auth = claims.get("https://api.openai.com/auth")
        account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
        if (not isinstance(account, str) or not 1 <= len(account) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in account)):
            raise ValueError("missing account routing identity")
        return Identity(current_subject, account)
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
        raise IdentityError("OpenAI ID-token verification failed; credentials were not activated.") from None
