"""Install a prepared @tg release on the existing VDSina host as root.

The archive is produced by Telegram/tools/build_releases.py. Secrets are created
on the host, outside releases; upgrades preserve the existing state and keys.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import time
import urllib.request
import zipfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--release", required=True)
    parser.add_argument("--domain", default="tg-77-238-249-144.sslip.io")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root on the target Linux host")
    if not args.release.replace("-", "").replace(".", "").isalnum():
        raise SystemExit("Invalid release name")
    if not all(c.isalnum() or c in ".-" for c in args.domain):
        raise SystemExit("Invalid domain")
    base = Path("/opt/tg-mcp")
    release = base / "releases" / args.release
    release.mkdir(parents=True, mode=0o755, exist_ok=False)
    with zipfile.ZipFile(args.archive) as archive:
        for entry in archive.infolist():
            target = (release / entry.filename).resolve()
            if not target.is_relative_to(release.resolve()) or entry.filename.startswith("/"):
                raise SystemExit("Unsafe release archive path")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise SystemExit("Symlinks are not allowed in release archive")
        archive.extractall(release)
    assert (release / "src/server.tsx").is_file()
    assert (release / "assets/icon-256.png").stat().st_size < 10000
    bun = "/opt/tg-runtime/bun"
    run(bun, "install", "--production", "--frozen-lockfile", "--ignore-scripts", cwd=release)

    try:
        account = pwd.getpwnam("tg-mcp")
    except KeyError:
        run("useradd", "--system", "--user-group", "--home-dir", "/var/lib/tg-mcp", "--shell", "/usr/sbin/nologin", "tg-mcp")
        account = pwd.getpwnam("tg-mcp")
    state = Path("/var/lib/tg-mcp")
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    os.chown(state, account.pw_uid, account.pw_gid)
    env_file = Path("/etc/tg-mcp.env")
    environment = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                environment[key] = value
    environment.update({
        "ISSUER": f"https://{args.domain}", "PORT": "18768", "BRAND_NAME": "tg",
        "LOG_SERVICE_NAME": "tg-mcp", "DATABASE_PATH": str(state / "cloud.db"),
        "TG_CREDENTIALS_FILE": str(state / "telegram-api.json"),
        "HOME": str(state), "NODE_ENV": "production", "MCP_TELEGRAM_TELEMETRY": "local-only",
        "LOG_USER_IDS": "false", "FREE_TIER_LIMIT": "0", "MCP_TELEGRAM_ENABLE_GROUP_CALLS": "1",
        "MCP_TELEGRAM_ENABLE_QUICK_REPLIES": "1", "MCP_TELEGRAM_ENABLE_STARS": "0",
        "SOURCE_REPO_URL": "https://github.com/Donkiton/MCP-Plag", "ISSUES_LABEL": "GitHub",
        "MCP_DRAIN_HEALTH_DELAY_MS": "1000", "MCP_DRAIN_TIMEOUT_MS": "30000",
    })
    for key in ("SESSION_ENCRYPTION_KEY", "ADMIN_TOKEN", "LOG_HASH_SALT"):
        environment.setdefault(key, secrets.token_hex(32))
    environment.setdefault("TG_SETUP_TOKEN", secrets.token_urlsafe(32))
    # Root-only env file; systemd passes the values to the unprivileged service.
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in sorted(environment.items())))
    env_file.chmod(0o600)

    unit = """[Unit]
Description=Private @tg Telegram MCP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tg-mcp
Group=tg-mcp
WorkingDirectory=/opt/tg-mcp/current
EnvironmentFile=/etc/tg-mcp.env
ExecStart=/opt/tg-runtime/bun src/server.tsx
Restart=on-failure
RestartSec=5
TimeoutStopSec=50
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/tg-mcp
MemoryHigh=384M
MemoryMax=512M
TasksMax=128

[Install]
WantedBy=multi-user.target
"""
    Path("/etc/systemd/system/tg-mcp.service").write_text(unit)
    temporary_link = base / f".current-{args.release}"
    temporary_link.symlink_to(release, target_is_directory=True)
    os.replace(temporary_link, base / "current")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "tg-mcp.service")
    run("systemctl", "restart", "tg-mcp.service")
    for attempt in range(20):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18768/health", timeout=2) as response:
                health = json.load(response)
            if health.get("status") == "ok":
                break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit("Service failed health check; nginx was not changed")

    cert = Path("/etc/letsencrypt/live") / args.domain
    assert (cert / "fullchain.pem").is_file(), "Create the HTTPS certificate before deployment"
    config = f"""# Managed private @tg vhost; existing SNI listener owns public 443.
limit_req_zone $binary_remote_addr zone=tg_requests:1m rate=10r/s;
server {{
    listen 127.0.0.1:8443 ssl;
    server_name {args.domain};
    ssl_certificate {cert}/fullchain.pem;
    ssl_certificate_key {cert}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    access_log off;
    error_log /dev/null crit;
    client_max_body_size 55m;
    client_body_timeout 30s;
    location ^~ /api/ {{ return 404; }}
    location / {{
        limit_req zone=tg_requests burst=40 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:18768;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 950s;
        proxy_connect_timeout 5s;
    }}
}}
"""
    available = Path("/etc/nginx/sites-available/tg-mcp.conf")
    enabled = Path("/etc/nginx/sites-enabled/tg-mcp.conf")
    sni_file = Path("/etc/nginx/stream.d/openai-relay.conf")
    old_sni = sni_file.read_text()
    sni_line = f"    {args.domain} 127.0.0.1:8443;"
    new_sni = old_sni
    if sni_line not in old_sni:
        anchor = "    mailru-77-238-249-144.sslip.io 127.0.0.1:8443;"
        assert anchor in old_sni, "SNI layout changed; inspect before deployment"
        new_sni = old_sni.replace(anchor, anchor + "\n" + sni_line, 1)
    backups = Path("/var/backups/tg-mcp") / args.release
    backups.mkdir(parents=True, mode=0o700)
    (backups / "openai-relay.conf").write_text(old_sni)
    old_vhost = available.read_text() if available.exists() else None
    if old_vhost:
        (backups / "tg-mcp.conf").write_text(old_vhost)
    had_link = enabled.is_symlink()
    available.write_text(config)
    if not enabled.exists():
        enabled.symlink_to(available)
    sni_file.write_text(new_sni)
    try:
        run("nginx", "-t")
    except Exception:
        sni_file.write_text(old_sni)
        if old_vhost is not None:
            available.write_text(old_vhost)
        else:
            if not had_link and enabled.is_symlink():
                enabled.unlink()
            available.unlink(missing_ok=True)
        raise
    run("systemctl", "reload", "nginx")
    print(json.dumps({"release": args.release, "endpoint": f"https://{args.domain}/mcp",
                      "health": health, "telegram_setup": "user_browser_step_required"}))


if __name__ == "__main__":
    main()
