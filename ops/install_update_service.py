#!/usr/bin/env python3
"""Install the manual, one-shot server updater; never starts an update."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SERVICE_NAME = "reshuffle-update.service"
MANAGED_MARKER = "# Managed by DeShuffle ops/install_update_service.py"


class InstallError(RuntimeError):
    """An actionable installation failure, safe to show to the operator."""


def systemd_value(value: str, *, executable: bool = False) -> str:
    """Quote one systemd argument, including its specifier/env expansion rules."""
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InstallError("Unit paths must be non-empty and contain no control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if executable:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def render_service(template: str, repo: Path, config: Path, python: Path) -> str:
    replacements = {
        "@MANAGED_MARKER@": MANAGED_MARKER,
        "@PYTHON@": systemd_value(str(python), executable=True),
        "@UPDATER@": systemd_value(str(repo / "ops" / "update_bot.py"), executable=True),
        "@CONFIG@": systemd_value(str(config), executable=True),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def config_repo(config_path: Path) -> Path:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError("Cannot read updater config JSON") from exc
    if not isinstance(config, dict) or not isinstance(config.get("repo"), str):
        raise InstallError("Updater config must contain an absolute repo path")
    repo = Path(config["repo"])
    if not repo.is_absolute() or not repo.is_dir():
        raise InstallError("Updater repo must be an existing absolute directory")
    updater = repo / "ops" / "update_bot.py"
    template = repo / "ops" / "systemd" / SERVICE_NAME
    if not updater.is_file() or not template.is_file():
        raise InstallError("The configured checkout does not contain the updater and unit template")
    return repo.resolve()


def write_unit(path: Path, content: str) -> None:
    """Refuse foreign/symlink units and replace our file atomically."""
    if path.is_symlink():
        raise InstallError("Refusing to overwrite a symlink unit: " + str(path))
    if path.exists() and MANAGED_MARKER not in path.read_text(encoding="utf-8"):
        raise InstallError("An unmanaged unit already exists: " + str(path))
    fd, temporary = tempfile.mkstemp(prefix=".reshuffle-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_service(config_path: Path, python: Path, *,
                    unit_dir: Path = Path("/etc/systemd/system")) -> None:
    config_path = config_path.resolve(strict=True)
    python = python.resolve(strict=True)
    repo = config_repo(config_path)
    unit_path = unit_dir / SERVICE_NAME
    # Verify ownership of an existing unit before invoking any external command.
    if unit_path.is_symlink() or (unit_path.exists() and
            MANAGED_MARKER not in unit_path.read_text(encoding="utf-8")):
        raise InstallError("Refusing to replace an unmanaged updater service")
    content = render_service((repo / "ops" / "systemd" / SERVICE_NAME).read_text(encoding="utf-8"),
                             repo, config_path, python)
    # The updater owns validation of Docker, Compose overrides, paths and Git state.
    # --check must remain read-only and also accept disabled updater configs.
    subprocess.run([str(python), str(repo / "ops" / "update_bot.py"),
                    "--config", str(config_path), "--check"], check=True)
    write_unit(unit_path, content)
    subprocess.run(["systemctl", "daemon-reload"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path,
                        help="existing server-local updater JSON config")
    parser.add_argument("--python", type=Path, default=Path(sys.executable),
                        help="absolute Python 3.10+ interpreter for the service")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise InstallError("Install on the Linux Docker host as root")
        if not args.config.is_absolute() or not args.python.is_absolute():
            raise InstallError("--config and --python must be absolute paths")
        if shutil.which("systemctl") is None:
            raise InstallError("systemctl is required on this host")
        install_service(args.config, args.python)
    except (InstallError, OSError, subprocess.CalledProcessError) as exc:
        print("Updater service installation failed: " + str(exc), file=sys.stderr)
        return 1
    print("Installed reshuffle-update.service. No update was started and no timer was installed.")
    print("After configuring enabled=true: sudo systemctl start reshuffle-update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
