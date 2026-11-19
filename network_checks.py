"""Credential-free smoke checks for the actual frozen app, without opening the UI."""
import json
import ssl

import certifi
import requests


def check_https(online=False):
    context = ssl.create_default_context(cafile=certifi.where())
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("HTTPS certificate verification is not enabled")
    if not context.get_ca_certs():
        raise RuntimeError("No trusted certificate authorities were bundled")
    result = {"tls": "ok", "trusted_ca_count": len(context.get_ca_certs())}
    if online:
        for name, url in (("discogs", "https://api.discogs.com/"), ("audd", "https://api.audd.io/")):
            response = requests.get(url, headers={"User-Agent": "FlipsideFlow/1.0"},
                                    timeout=(5, 10), allow_redirects=False)
            if response.status_code >= 500 or 300 <= response.status_code < 400:
                raise RuntimeError(f"{name} returned HTTP {response.status_code}")
            result[name] = response.status_code
    return result


def main(online=False):
    try:
        print(json.dumps(check_https(online)), flush=True)
        return 0
    except Exception as exc:
        # No request URLs, environment variables or credentials in diagnostics.
        print(json.dumps({"tls": "failed", "error_type": type(exc).__name__}), flush=True)
        return 1
