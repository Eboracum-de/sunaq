import os
from pathlib import Path
import subprocess
import sys

from rag.tls_compat import x509_strict_enabled

ROOT = Path(__file__).resolve().parent.parent


def test_x509_strict_defaults_off_and_can_be_enabled():
    assert x509_strict_enabled({}) is False
    assert x509_strict_enabled({"tls": {"x509_strict": True}}) is True
    assert x509_strict_enabled({"tls": {"x509_strict": False}}) is False


def test_compatibility_mode_clears_strict_for_stdlib_and_urllib3_in_fresh_process():
    code = """
import ssl
from rag.tls_compat import configure_tls_compat
configure_tls_compat({"tls":{"x509_strict":False}})
a = ssl.create_default_context()
assert a.verify_mode == ssl.CERT_REQUIRED
assert a.check_hostname is True
assert not (a.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))
import urllib3.util.ssl_ as u
b = u.create_urllib3_context()
assert not (b.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))
print("ok")
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    run = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, env=env, check=True)
    assert run.stdout.strip() == "ok"


def test_both_install_profiles_default_strict_mode_off_and_allow_opt_in():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ("install/profiles/install-standard.sh", "install/profiles/install-dockerized.sh"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "X509_STRICT=0" in text
        assert "--x509-strict" in text
        assert "--no-x509-strict" in text
