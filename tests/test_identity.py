import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from quota_window.identity import ISSUER, IdentityError, verify_identity


class IdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid="synthetic-key", alg="RS256", use="sig")

    def token(self, **overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "synthetic-client", "sub": "synthetic-subject", "iat": now,
                  "exp": now + 600, "nonce": "synthetic-nonce",
                  "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account"}}
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "synthetic-key"})

    def request(self, url):
        if url.endswith("openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": ISSUER + "/synthetic-jwks"}
        self.assertEqual(url, ISSUER + "/synthetic-jwks")
        return {"keys": [self.jwk]}

    def test_real_rsa_signature_and_nonce_are_verified(self):
        identity = verify_identity(self.token(), "synthetic-client", nonce="synthetic-nonce", request=self.request)
        self.assertEqual(identity.subject, "synthetic-subject")
        self.assertEqual(identity.account_id, "synthetic-account")
        self.assertNotIn("synthetic-subject", repr(identity))

    def test_forged_signature_is_rejected(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        forged = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "synthetic-key"})
        with self.assertRaises(IdentityError):
            verify_identity(forged, "synthetic-client", request=self.request)

    def test_issuer_audience_expiry_nonce_and_subject_are_bound(self):
        for overrides, extra in (({"iss": "https://evil.test"}, {}), ({"aud": "other"}, {}),
                ({"exp": int(time.time()) - 60}, {}), ({"nonce": "other"}, {"nonce": "synthetic-nonce"}),
                ({"sub": "other"}, {"subject": "synthetic-subject"}),
                ({"https://api.openai.com/auth": {}}, {})):
            with self.subTest(overrides=overrides), self.assertRaises(IdentityError):
                verify_identity(self.token(**overrides), "synthetic-client", request=self.request, **extra)

    def test_untrusted_jwks_source_is_never_requested(self):
        calls = []

        def request(url):
            calls.append(url)
            return {"issuer": ISSUER, "jwks_uri": "https://evil.test/keys"}

        with self.assertRaises(IdentityError):
            verify_identity(self.token(), "synthetic-client", request=request)
        self.assertEqual(calls, [ISSUER + "/.well-known/openid-configuration"])

    def test_ambiguous_keys_and_unsigned_tokens_are_rejected(self):
        def request(url):
            value = self.request(url)
            return {"keys": [self.jwk, self.jwk]} if "keys" in value else value

        with self.assertRaises(IdentityError):
            verify_identity(self.token(), "synthetic-client", request=request)
        unsigned = jwt.encode({"sub": "synthetic"}, key=None, algorithm="none")
        with self.assertRaises(IdentityError):
            verify_identity(unsigned, "synthetic-client", request=self.request)


if __name__ == "__main__":
    unittest.main()
