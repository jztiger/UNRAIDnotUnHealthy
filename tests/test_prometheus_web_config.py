"""Tests for the Prometheus web-config generator (cont-init script).

It must (a) emit basic_auth_users only when a bcrypt hash is set, (b) write the
hash LITERALLY (bcrypt's `$2b$...$` must survive — no shell expansion), and
(c) emit a valid no-auth config when the hash is empty.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "rootfs/etc/cont-init.d/03-prometheus-web-config"


def _run(tmp_path, env):
    out = tmp_path / "web-config.yml"
    full_env = {**os.environ, "PROM_WEB_CONFIG_PATH": str(out), **env}
    subprocess.run(["bash", str(SCRIPT)], env=full_env, check=True)
    return out.read_text()


def test_emits_basic_auth_when_hash_set(tmp_path):
    bcrypt = "$2b$10$abcdefghijklmnopqrstuv0123456789012345678901234567890ab"
    text = _run(tmp_path, {
        "PROMETHEUS_REMOTE_WRITE_USER": "closet-pi",
        "PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT": bcrypt,
    })
    assert "basic_auth_users:" in text
    assert "closet-pi:" in text
    # Hash must appear verbatim, $-chars intact.
    assert bcrypt in text


def test_no_auth_when_hash_empty(tmp_path):
    text = _run(tmp_path, {
        "PROMETHEUS_REMOTE_WRITE_USER": "closet-pi",
        "PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT": "",
    })
    assert "basic_auth_users" not in text
    assert text.strip() == "{}"


def test_writes_self_scrape_password_file(tmp_path):
    pw = tmp_path / "pw"
    out = tmp_path / "web-config.yml"
    env = {
        **os.environ,
        "PROM_WEB_CONFIG_PATH": str(out),
        "PROM_SELF_SCRAPE_PW_FILE": str(pw),
        "PROMETHEUS_REMOTE_WRITE_USER": "closet-pi",
        "PROMETHEUS_REMOTE_WRITE_PASSWORD_BCRYPT": "$2b$10$x",
        "PROM_BASIC_AUTH_PASSWORD": "s3cret",
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True)
    assert pw.read_text() == "s3cret"
