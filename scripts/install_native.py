#!/usr/bin/env python3
"""Register this user's own service. No sudo, no system Python modifications."""
from pathlib import Path
import argparse
import os
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--bind-host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=3189)
parser.add_argument("--model-base-url", default="http://127.0.0.1:3184")
parser.add_argument("--model", default="qwen3.8:27b")
parser.add_argument("--model-token-file", type=Path,
                    help="Existing proxy credential file. Required only when creating .env.")
args = parser.parse_args()
if not 1024 <= args.port <= 65535:
    parser.error("--port must be between 1024 and 65535")
if sys.platform != "linux":
    raise SystemExit("This user-service installer is for Linux; run python -m app.serve locally.")
if not (root / ".venv/bin/python").exists():
    raise SystemExit("Create the project .venv and install requirements.txt first.")
env = root / ".env"
if not env.exists() and (args.model_token_file is None or not args.model_token_file.is_file()):
    parser.error("Provide --model-token-file pointing to an existing proxy credential file, or prepare .env first.")
# EnvironmentFile is line based. Reject control characters rather than allowing
# a path/host argument to introduce another environment assignment.
for value in (str(root), args.bind_host, args.model_base_url, args.model,
              str(args.model_token_file or "")):
    if any(c in value for c in ("\n", "\r", "\x00")):
        parser.error("Configuration values must not contain control characters")
data = root / "data"
data.mkdir(mode=0o700, exist_ok=True)
data.chmod(0o700)
if not env.exists():
    contents = {
        "APP_BIND_HOST": args.bind_host, "APP_PORT": str(args.port),
        "AGENT_DATA_DIR": str(data), "MODEL_BASE_URL": args.model_base_url,
        "MODEL_NAME": args.model,
        "MODEL_TOKEN_FILE": str(args.model_token_file.resolve()),
        "APP_ACCESS_TOKEN_FILE": str(data / "access-token"),
        "AGENT_UID": str(os.getuid()), "AGENT_GID": str(os.getgid()),
        "AGENT_MAX_STEPS": "8", "AGENT_MAX_OUTPUT_TOKENS": "2048",
        "AGENT_MAX_TOTAL_TOKENS": "18000", "AGENT_RUN_TIMEOUT": "240",
    }
    with open(env, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        stream.write("".join(k + "=" + v + "\n" for k, v in contents.items()))
unit_dir = Path.home() / ".config/systemd/user"
unit_dir.mkdir(parents=True, exist_ok=True)
unit = unit_dir / "zhouyi-general-agent.service"
unit.write_text(f"""[Unit]
Description=Zhouyi standalone Qwen general agent
After=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
EnvironmentFile={env}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
ExecStart={root}/.venv/bin/python -m app.serve
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
""")
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
subprocess.run(["systemctl", "--user", "enable", "--now", unit.name], check=True)
print("User service enabled:", unit.name)
print("Project:", root)
print("Application login-code file:", data / "access-token", "(not displayed)")
