"""A mock OpenID Connect provider for one test: https://localhost:9443, discovery, JWKS, authorize (redirects back at
once as the subject ?sub=, default "alice") and a token endpoint with an RS256 id_token. argv: cert key."""
import base64
import http.server
import json
import ssl
import sys
import time
import urllib.parse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISS, CLIENT = "https://localhost:9443", "abi-test"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CODES: dict = {}


def b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


pub = KEY.public_key().public_numbers()
JWKS = {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256", "n": b64(pub.n), "e": b64(pub.e)}]}
DISC = {"issuer": ISS, "authorization_endpoint": ISS + "/authorize", "token_endpoint": ISS + "/token", "jwks_uri": ISS + "/jwks",
        "response_types_supported": ["code"], "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"]}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def js(self, d, code=200):
        b = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/.well-known/openid-configuration":
            return self.js(DISC)
        if u.path == "/jwks":
            return self.js(JWKS)
        if u.path == "/authorize":
            code = base64.urlsafe_b64encode(time.time_ns().to_bytes(8, "big")).decode().rstrip("=")
            CODES[code] = (q["nonce"], q.get("sub", "alice"))
            self.send_response(302)
            self.send_header("Location", q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]}))
            self.end_headers()
            return None
        return self.js({}, 404)

    def do_POST(self):
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
        nonce, sub = CODES.pop(form.get("code"), (None, None))
        if not nonce:
            return self.js({"error": "invalid_grant"}, 400)
        now = int(time.time())
        tok = jwt.encode({"iss": ISS, "aud": CLIENT, "sub": sub, "nonce": nonce, "iat": now, "exp": now + 300}, KEY, "RS256", {"kid": "k1"})
        return self.js({"access_token": "x", "token_type": "Bearer", "id_token": tok, "expires_in": 300})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 9443), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(sys.argv[1], sys.argv[2])
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
srv.serve_forever()
