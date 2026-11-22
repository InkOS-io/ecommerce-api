"""Transport for the storefront example. Uses only Python's standard library."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class StorefrontError(Exception):
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers
        super().__init__(f"HTTP {status}: {body}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # A misconfigured endpoint must never forward a credential to a second host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Storefront:
    def __init__(self, base_url, token):
        parsed = urllib.parse.urlsplit(base_url)
        local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and local)):
            raise ValueError("Use HTTPS, or HTTP on loopback for a local sandbox.")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a base URL without user information, query or fragment.")
        self.base = base_url.rstrip("/")
        self.token = token
        self.http = urllib.request.build_opener(NoRedirect)

    @classmethod
    def from_env(cls):
        return cls(os.environ["INKOS_BASE"], os.environ["INKOS_TOKEN"])

    def auth_headers(self):
        return {"Authorization": "Bearer " + self.token}

    def call(self, method, path, body=None, key=None):
        headers = {"Accept": "application/json", **self.auth_headers()}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.http.open(req, timeout=20) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                payload = error.read()
                try:
                    problem = json.loads(payload)
                except (ValueError, UnicodeError):
                    problem = {"message": "Non-JSON error from the configured host."}
                raise StorefrontError(error.code, problem, error.headers) from None
