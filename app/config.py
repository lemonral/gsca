from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model_base_url: str = "http://127.0.0.1:3184"
    model_name: str = "qwen3.8:27b"
    model_token_file: str = ""
    access_token_file: str = ""
    max_steps: int = 8
    max_output_tokens: int = 2048
    max_total_tokens: int = 18000
    run_timeout: int = 240
    context_chars: int = 20000
    context_tokens: int = 8192

    @classmethod
    def from_env(cls):
        def number(name, default, lo, hi):
            value = int(os.getenv(name, default))
            if not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}")
            return value
        return cls(
            data_dir=Path(os.getenv("AGENT_DATA_DIR", "./data")).resolve(),
            model_base_url=os.getenv("MODEL_BASE_URL", "http://127.0.0.1:3184").rstrip("/"),
            model_name=os.getenv("MODEL_NAME", "qwen3.8:27b"),
            model_token_file=os.getenv("MODEL_TOKEN_FILE", ""),
            access_token_file=os.getenv("APP_ACCESS_TOKEN_FILE", ""),
            max_steps=number("AGENT_MAX_STEPS", 8, 1, 20),
            max_output_tokens=number("AGENT_MAX_OUTPUT_TOKENS", 2048, 64, 8192),
            max_total_tokens=number("AGENT_MAX_TOTAL_TOKENS", 18000, 1000, 100000),
            run_timeout=number("AGENT_RUN_TIMEOUT", 240, 10, 900),
        )

    def read_model_token(self):
        return Path(self.model_token_file).read_text().strip() if self.model_token_file else ""


def access_secret(settings):
    """Generate an app-only login code. Never reuse the upstream model credential."""
    import secrets
    target = Path(settings.access_token_file) if settings.access_token_file else settings.data_dir / "access-token"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
                f.write(secrets.token_urlsafe(24) + "\n")
        except FileExistsError:
            pass
    if target.is_symlink() or not target.is_file():
        raise ValueError("Access-token path must be a regular file")
    value = target.read_text().strip()
    if len(value) < 24:
        raise ValueError("Access token must have at least 24 characters")
    return value
