"""Preserve optional certificate-policy data supplied by the build Python runtime.

Some Python distributions load this file when creating any SSLContext. certifi
alone is not sufficient: the runtime's existing trust policy must travel with it.
Stock Python has no such file and needs no extra data here.
"""
from pathlib import Path
import ssl

policy = Path(ssl.__file__).with_name("codex-rejected-cas.pem")
datas = [(str(policy), ".")] if policy.is_file() else []
