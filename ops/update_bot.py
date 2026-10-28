#!/usr/bin/env python3
"""One explicitly requested deployment of main. No scheduler, model or shell."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


class UpdateError(RuntimeError):
    """Only fixed, non-secret diagnostics may be passed to this exception."""


def private_json(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid()):
        raise UpdateError("Backup/state directories must be private and owned by the updater user")


def load_config(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError("Cannot read updater configuration") from exc
    if not isinstance(config, dict) or not isinstance(config.get("enabled"), bool):
        raise UpdateError("Configuration requires boolean enabled")
    for key in ("repo", "backup_dir", "state_dir"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise UpdateError("repo, backup_dir and state_dir must be absolute paths")
        config[key] = str(Path(config[key]).resolve())
    repo = Path(config["repo"])
    if not repo.is_dir():
        raise UpdateError("Repository directory is missing")
    for key in ("backup_dir", "state_dir"):
        location = Path(config[key])
        if location == repo or repo in location.parents or location in repo.parents or location == Path(location.anchor):
            raise UpdateError("Backup and state directories must be outside the checkout and filesystem root")
    backup, state = Path(config["backup_dir"]), Path(config["state_dir"])
    if backup == state or backup in state.parents or state in backup.parents:
        raise UpdateError("Backup and state directories must not overlap")
    config.setdefault("remote", "origin")
    config.setdefault("branch", "main")
    config.setdefault("service", "reshuffle")
    config.setdefault("project", "reshuffle")
    config.setdefault("compose_files", [str(repo / "docker-compose.yml")])
    config.setdefault("env_file", str(repo / ".env"))
    config.setdefault("build_args", {})
    config.setdefault("external_config_files", [])
    config.setdefault("health_timeout", 300)
    config.setdefault("command_timeout", 900)
    config.setdefault("database_path", "/data/voice_activity.sqlite3")
    if config["branch"] != "main":
        raise UpdateError("This updater only deploys main")
    for key in ("remote", "service", "project"):
        if not isinstance(config[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config[key]):
            raise UpdateError("Invalid remote, service or project name")
    for key in ("health_timeout", "command_timeout"):
        if not isinstance(config[key], int) or not 15 <= config[key] <= 7200:
            raise UpdateError("Timeouts must be integer seconds between 15 and 7200")
    if not isinstance(config["build_args"], dict) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)) or not isinstance(value, str)
        for key, value in config["build_args"].items()
    ):
        raise UpdateError("build_args must map build argument names to strings")
    for key in ("compose_files", "external_config_files"):
        if not isinstance(config[key], list) or (key == "compose_files" and not config[key]):
            raise UpdateError("Invalid file list")
        for filename in config[key]:
            if not isinstance(filename, str) or not Path(filename).is_absolute() or not Path(filename).is_file():
                raise UpdateError("Configured files must exist and have absolute paths")
    if config["env_file"] is not None and (
        not isinstance(config["env_file"], str) or not Path(config["env_file"]).is_absolute()
        or not Path(config["env_file"]).is_file()
    ):
        raise UpdateError("env_file must be an existing absolute file or null")
    database = config["database_path"]
    if not isinstance(database, str) or not database.startswith("/data/") or ".." in database.split("/"):
        raise UpdateError("database_path must stay inside /data")
    return config


class Runner:
    def __init__(self, timeout: int):
        self.timeout = timeout

    def run(self, args: list[str], *, cwd: str | Path | None = None, timeout: float | None = None) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        try:
            result = subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                                    text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout or self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Command unavailable or timed out: " + args[0]) from exc
        if result.returncode:
            # Docker config/logs can contain tokens. Never echo stdout/stderr or argv.
            raise UpdateError("Command failed: " + args[0])
        return result.stdout.strip()


@contextmanager
def deployment_lock(directory: Path):
    import fcntl  # Linux host only; importing this module for tests works on Windows.
    private_directory(directory)
    with (directory / "update.lock").open("a") as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Another update is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def parse_json(value: str):
    try:
        return json.loads(value)
    except ValueError as exc:
        raise UpdateError("Command returned invalid JSON") from exc


def parse_compose_json(value: str):
    # Compose config escapes every dollar in the serialized document for reuse
    # as a Compose file (including mapping keys). Inspection data is unescaped.
    # https://github.com/docker/compose/blob/v2.34.0/cmd/compose/config.go#L393
    return parse_json(value.replace("$$", "$"))


def compose_literals(value):
    """A rendered Compose snapshot must not interpolate literal dollars a second time."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, dict):
        return {key: compose_literals(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compose_literals(item) for item in value]
    return value


def token_from(container: dict) -> str | None:
    return next((item.split("=", 1)[1] for item in container.get("Config", {}).get("Env", [])
                 if item.startswith("DISCORD_TOKEN=")), None)


class Updater:
    def __init__(self, config: dict, runner=None, *, sleep=time.sleep, now=time.time):
        self.config = config
        self.repo = Path(config["repo"])
        self.runner = runner or Runner(config["command_timeout"])
        self.sleep, self.now = sleep, now
        self.service = config["service"]
        self.report = {"format": 1, "started_at": now(), "state": "preflight"}
        self.current: dict = {}
        self.before: dict = {}
        self.candidate: Path | None = None
        self.override: Path | None = None
        self.stopped = False
        self.deployed = False
        self.target = ""
        self.old_sha = ""
        self.old_codex = False

    def checkpoint(self, state: str) -> None:
        self.report["state"] = state
        private_json(Path(self.config["state_dir"]) / "last-report.json", self.report)
        print("Deployment phase: " + state, flush=True)

    def remember_files(self) -> dict:
        filenames = list(self.config["compose_files"]) + list(self.config["external_config_files"])
        if self.config["env_file"]:
            filenames.append(self.config["env_file"])
        return {filename: hashlib.sha256(Path(filename).read_bytes()).hexdigest() for filename in filenames}

    def config_unchanged(self) -> None:
        if self.remember_files() != self.file_hashes:
            raise UpdateError("Deployment configuration changed during update")

    def git(self, *args: str) -> str:
        return self.runner.run(["git", "-C", str(self.repo), *args])

    def compose(self, *args: str, candidate: bool = False, override: Path | None = None, frozen: bool = False) -> str:
        command = ["docker", "compose", "--project-directory", str(self.repo), "--project-name", self.config["project"]]
        if self.config["env_file"] and not frozen:
            command += ["--env-file", self.config["env_file"]]
        filenames = [str(self.rollback_base)] if frozen else self.config["compose_files"]
        for filename in filenames:
            path = Path(filename)
            if candidate and self.candidate and self.repo in path.parents:
                path = self.candidate / path.relative_to(self.repo)
            command += ["--file", str(path)]
        if override:
            command += ["--file", str(override)]
        return self.runner.run(command + list(args))

    def inspect(self, container: str) -> dict:
        result = parse_json(self.runner.run(["docker", "inspect", container], timeout=30))
        if not isinstance(result, list) or len(result) != 1:
            raise UpdateError("Expected exactly one container")
        return result[0]

    def container(self, *, frozen: bool = False) -> dict:
        identifiers = self.compose("ps", "--all", "--quiet", self.service, frozen=frozen).splitlines()
        if len(identifiers) != 1:
            raise UpdateError("Expected one existing service container; refusing first install or replicas")
        return self.inspect(identifiers[0])

    def no_duplicate_bot(self, current: dict) -> None:
        token = token_from(current)
        if not token:
            raise UpdateError("Existing bot container has no Discord token")
        for identifier in self.runner.run(["docker", "ps", "--quiet"]).splitlines():
            other = self.inspect(identifier)
            if other["Id"] != current["Id"] and token_from(other) == token:
                raise UpdateError("Another running container uses this bot token")

    def preflight(self) -> None:
        if Path(self.git("rev-parse", "--show-toplevel")).resolve() != self.repo:
            raise UpdateError("Configured checkout is not the Git root")
        if self.git("status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError("Checkout has local changes; refusing to overwrite them")
        if self.git("branch", "--show-current") != "main":
            raise UpdateError("Server checkout must be on main")
        remote = self.git("remote", "get-url", self.config["remote"]).rstrip("/").removesuffix(".git")
        if remote.lower() not in {"https://github.com/delta-dashed/deshuffle", "git@github.com:delta-dashed/deshuffle",
                                  "ssh://git@github.com/delta-dashed/deshuffle"}:
            raise UpdateError("Remote is not delta-dashed/DeShuffle")
        self.old_sha = self.git("rev-parse", "HEAD")
        rendered = parse_compose_json(self.compose("config", "--format", "json"))
        if self.service not in rendered.get("services", {}):
            raise UpdateError("Service not found in configured Compose files")
        self.original_service = rendered["services"][self.service]
        self.original_rendered = rendered
        self.original_volumes = rendered.get("volumes", {})
        self.file_hashes = self.remember_files()
        build = self.original_service.get("build", {})
        if not isinstance(build, dict) or Path(build.get("context", "")).resolve() != self.repo:
            raise UpdateError("Updater requires this repository as the service build context")
        if Path(build.get("dockerfile", "Dockerfile")).is_absolute() or ".." in Path(build.get("dockerfile", "Dockerfile")).parts:
            raise UpdateError("Dockerfile must be relative to the repository build context")
        self.current = self.container()
        labels = self.current.get("Config", {}).get("Labels", {}) or {}
        if labels.get("com.docker.compose.project") != self.config["project"] or labels.get("com.docker.compose.service") != self.service:
            raise UpdateError("Compose project/service does not match existing container")
        if not self.current.get("State", {}).get("Running"):
            raise UpdateError("Existing bot must be running before an update")
        data_mounts = [mount for mount in self.current.get("Mounts", []) if mount["Destination"] == "/data"]
        if len(data_mounts) != 1 or data_mounts[0].get("Type") not in {"volume", "bind"} or not data_mounts[0].get("RW"):
            raise UpdateError("Expected one persistent writable /data mount")
        if any(mount.get("RW") and mount["Destination"] != "/data" for mount in self.current.get("Mounts", [])):
            raise UpdateError("Additional writable mounts need an explicit backup strategy")
        for mount in self.current.get("Mounts", []):
            source = Path(mount["Source"]).resolve()
            for key in ("backup_dir", "state_dir"):
                location = Path(self.config[key])
                if source == location or source in location.parents or location in source.parents:
                    raise UpdateError("Backup/state directories must not overlap persistent mounts")
        self.validate_mounts(rendered, self.current)
        self.no_duplicate_bot(self.current)
        environment = dict(item.split("=", 1) for item in self.current["Config"].get("Env", []) if "=" in item)
        image_info = parse_json(self.runner.run(["docker", "image", "inspect", self.current["Image"]]))[0]
        image_config = image_info.get("Config", {})
        image_env = dict(item.split("=", 1) for item in image_config.get("Env", []) if "=" in item)
        runtime_env = {key: value for key, value in environment.items() if image_env.get(key) != value}
        rendered_env = self.original_service.get("environment", {})
        if any(environment.get(key) != ("" if value is None else str(value)) for key, value in rendered_env.items()) or any(
            key != "RESHUFFLE_DEPLOY_SHA" and key not in rendered_env for key in runtime_env
        ):
            raise UpdateError("Compose environment differs from running bot; reconcile configuration first")
        self.frozen_env = {key: environment[key] for key in set(runtime_env) | set(rendered_env)}
        for key, actual_key, default_key in (("command", "Cmd", "Cmd"), ("entrypoint", "Entrypoint", "Entrypoint"),
                                             ("user", "User", "User"), ("working_dir", "WorkingDir", "WorkingDir")):
            expected = self.original_service.get(key, image_config.get(default_key))
            if expected != self.current["Config"].get(actual_key):
                raise UpdateError("Compose command/user differs from running bot; manual rollout required")
        self.actual_env = environment
        self.require_club = environment.get("BOOKCLUB_ENABLED", "false").lower() in {"1", "true", "yes"}
        self.health_file = environment.get("RESHUFFLE_HEALTH_FILE", "/data/runtime-health.json")
        if not self.health_file.startswith("/data/") or ".." in self.health_file.split("/"):
            raise UpdateError("Runtime health file must be inside /data")
        self.old_codex = self.has_codex(self.current["Id"], running=True)
        self.before = self.snapshot(self.current)
        if self.require_club and not self.before.get("database_exists"):
            raise UpdateError("Enabled book club database is missing")
        if self.before.get("active_imports", 0):
            raise UpdateError("An archive import is running; wait before updating")
        self.report.update(old_commit=self.old_sha, old_image=self.current["Image"], schema_before=self.before.get("schema"))

    def validate_mounts(self, rendered: dict, container: dict) -> None:
        expected = rendered["services"][self.service].get("volumes", [])
        actual = {mount["Destination"]: mount for mount in container.get("Mounts", [])}
        if {mount["target"] for mount in expected} != set(actual):
            raise UpdateError("Compose mounts differ from running bot")
        for mount in expected:
            existing = actual[mount["target"]]
            if mount["type"] != existing["Type"] or bool(mount.get("read_only")) == bool(existing.get("RW")):
                raise UpdateError("Compose mount type/access differs from running bot")
            if mount["type"] == "volume":
                name = rendered.get("volumes", {}).get(mount.get("source"), {}).get("name")
                if not name or name != existing.get("Name"):
                    raise UpdateError("Compose named volume differs from running bot")
            elif mount["type"] == "bind":
                if Path(mount["source"]).resolve() != Path(existing["Source"]).resolve():
                    raise UpdateError("Compose bind mount differs from running bot")
            else:
                raise UpdateError("Unsupported persistent mount type")

    def has_codex(self, identifier: str, *, running: bool) -> bool:
        script = "import shutil; print('yes' if shutil.which('codex') else 'no')"
        command = (["docker", "exec", identifier, "python", "-c", script] if running else
                   ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", identifier, "-c", script])
        return self.runner.run(command) == "yes"

    def helper(self, container: dict, *args: str, backup: Path | None = None) -> str:
        command = ["docker", "run", "--rm", "--network", "none", "--user", "0",
                   "--volumes-from", container["Id"] + ":ro"]
        snapshot_script = self.repo / "ops" / "state_snapshot.py"
        command += ["--mount", "type=bind,src=" + str(snapshot_script) + ",dst=/state_snapshot.py,readonly"]
        if backup:
            command += ["--mount", "type=bind,src=" + str(backup) + ",dst=/backup"]
        return self.runner.run(command + ["--entrypoint", "python", container["Image"], *args])

    def snapshot(self, container: dict) -> dict:
        return parse_json(self.helper(container, "/state_snapshot.py", "capture", self.config["database_path"]))

    def build(self) -> None:
        self.git("fetch", "--prune", self.config["remote"], "main")
        self.target = self.git("rev-parse", self.config["remote"] + "/main")
        if not re.fullmatch(r"[a-f0-9]{40}", self.target):
            raise UpdateError("Remote main did not resolve to a commit")
        self.git("merge-base", "--is-ancestor", self.old_sha, self.target)
        self.report["target_commit"] = self.target
        # A successful release is identified by the actual container, never just checkout HEAD.
        labels = self.current.get("Config", {}).get("Labels", {}) or {}
        deployed = labels.get("org.opencontainers.image.revision")
        if deployed == self.target:
            self.wait_healthy(self.current, self.target, started_after=0)
            self.git("merge", "--ff-only", self.target)
            self.report["state"] = "unchanged"
            return
        stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.config["state_dir"]))
        stage.rmdir()  # git worktree requires an absent/empty target.
        self.candidate = stage
        self.git("worktree", "add", "--detach", str(stage), self.target)
        image = self.config["project"] + "-deployment:" + self.target
        override_value = {"services": {self.service: {
            "image": image,
            "build": {"context": str(stage), "args": self.config["build_args"],
                      "labels": {"org.opencontainers.image.revision": self.target}},
            "environment": dict(self.frozen_env, RESHUFFLE_DEPLOY_SHA=self.target),
            "labels": {"org.opencontainers.image.revision": self.target},
        }}}
        self.override = Path(self.config["state_dir"]) / "candidate-compose.json"
        private_json(self.override, compose_literals(override_value))
        proposed = parse_compose_json(self.compose("config", "--format", "json", candidate=True, override=self.override))
        service = proposed["services"][self.service]
        self.validate_mounts(proposed, self.current)
        if proposed.get("volumes", {}) != self.original_volumes:
            raise UpdateError("Candidate changes named volume definitions; manual rollout required")
        # A code deployment must not silently switch volume identities or Discord accounts.
        for key in ("volumes", "user", "working_dir", "entrypoint", "command"):
            if service.get(key) != self.original_service.get(key):
                raise UpdateError("Candidate changes runtime mounts or command; manual rollout required")
        expected_env = self.original_service.get("environment", {})
        actual_env = service.get("environment", {})
        if {k: v for k, v in actual_env.items() if k != "RESHUFFLE_DEPLOY_SHA"} != {
            k: v for k, v in expected_env.items() if k != "RESHUFFLE_DEPLOY_SHA"
        }:
            raise UpdateError("Candidate changes runtime configuration; manual rollout required")
        self.checkpoint("building")
        self.compose("build", self.service, candidate=True, override=self.override)
        self.image = image
        built = parse_json(self.runner.run(["docker", "image", "inspect", image]))[0].get("Config", {})
        for key, actual_key in (("command", "Cmd"), ("entrypoint", "Entrypoint"), ("user", "User"), ("working_dir", "WorkingDir")):
            if service.get(key, built.get(actual_key)) != self.current["Config"].get(actual_key):
                raise UpdateError("Candidate image changes runtime command/user; manual rollout required")
        effective_env = dict(item.split("=", 1) for item in built.get("Env", []) if "=" in item)
        effective_env.update(service.get("environment", {}))
        prefixes = ("BOOKCLUB_", "RESHUFFLE_", "DISCORD_", "CODEX_", "OPENAI_")
        for key in set(effective_env) | set(self.actual_env):
            if key != "RESHUFFLE_DEPLOY_SHA" and key.startswith(prefixes) and effective_env.get(key) != self.actual_env.get(key):
                raise UpdateError("Candidate image changes application configuration; manual rollout required")
        if self.old_codex and not self.has_codex(image, running=False):
            raise UpdateError("Candidate omits installed Codex CLI; preserve INSTALL_CODEX build argument")

    def backup(self) -> None:
        self.checkpoint("backup")
        self.config_unchanged()
        self.rollback_base = Path(self.config["state_dir"]) / "previous-compose.json"
        private_json(self.rollback_base, compose_literals(self.original_rendered))
        # Recheck the active instance immediately before entering the maintenance window.
        if self.container()["Id"] != self.current["Id"]:
            raise UpdateError("Service container changed during build")
        self.no_duplicate_bot(self.current)
        if self.snapshot(self.current).get("active_imports", 0):
            raise UpdateError("An archive import started during build; wait before updating")
        self.stopped = True
        self.compose("stop", "--timeout", "60", self.service)
        if self.inspect(self.current["Id"]).get("State", {}).get("Running"):
            raise UpdateError("Bot did not stop; cannot take a consistent backup")
        self.before = self.snapshot(self.current)
        stamp = datetime.fromtimestamp(self.now(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = Path(self.config["backup_dir"]) / (stamp + "-" + self.target[:12])
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.report["backup"] = str(directory)
        self.checkpoint("backup")
        script = """import hashlib,json,os,tarfile
p='/backup/data.tar.gz'
with tarfile.open(p,'w:gz') as archive:
    archive.add('/data',arcname='data',recursive=True)
os.chmod(p,0o600)
with tarfile.open(p,'r:gz') as archive:
    for member in archive:
        if member.isfile():
            with archive.extractfile(member) as stream:
                while stream.read(1024*1024):
                    pass
digest=hashlib.sha256()
with open(p,'rb') as stream:
    for block in iter(lambda:stream.read(1024*1024),b''):
        digest.update(block)
print(json.dumps({'sha256':digest.hexdigest(),'size':os.path.getsize(p)}))
"""
        # Run Python only, in the existing image with read-only data and no network.
        archive = parse_json(self.helper(self.current, "-c", script, backup=directory))
        if archive.get("size", 0) <= 0 or not re.fullmatch(r"[a-f0-9]{64}", archive.get("sha256", "")):
            raise UpdateError("Backup verification failed")
        files = list(self.config["compose_files"]) + list(self.config["external_config_files"])
        if self.config["env_file"]:
            files.append(self.config["env_file"])
        # Preserve read-only bind files such as external club/import configurations too.
        for mount in self.current.get("Mounts", []):
            if mount.get("Type") == "bind" and not mount.get("RW"):
                source = Path(mount["Source"])
                if not source.is_file():
                    raise UpdateError("Read-only bind directory needs an explicit backup strategy")
                files.append(str(source))
        host_files = directory / "host-files"
        host_files.mkdir(mode=0o700)
        mapping = []
        for index, filename in enumerate(dict.fromkeys(files)):
            dest = host_files / str(index)
            shutil.copyfile(filename, dest)
            os.chmod(dest, 0o600)
            mapping.append({"source": filename, "backup": str(dest.relative_to(directory)),
                            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
        private_json(directory / "before.json", self.before)
        private_json(directory / "manifest.json", {
            "format": 1, "old_commit": self.old_sha, "target_commit": self.target,
            "old_image": self.current["Image"], "data_archive": archive, "host_files": mapping,
            "mounts": self.current.get("Mounts", []), "schema": self.before.get("schema"),
        })

    def wait_healthy(self, container: dict, sha: str, *, started_after: float) -> None:
        deadline = self.now() + self.config["health_timeout"]
        while self.now() < deadline:
            state = self.inspect(container["Id"]).get("State", {})
            if state.get("Running") and not state.get("Restarting"):
                command = ["docker", "exec", container["Id"], "python", "-m", "bookclub.runtime_health",
                           "--check", self.health_file, "--expected-sha", sha, "--max-age", "90",
                           "--max-club-age", "300"]
                if started_after > 0:
                    command += ["--started-after", str(started_after)]
                if self.require_club:
                    command.append("--require-club")
                try:
                    self.runner.run(command, timeout=min(15, max(1, deadline - self.now())))
                    return
                except UpdateError:
                    pass
            self.sleep(5)
        raise UpdateError("New runtime did not pass Discord and club health checks in time")

    def deploy(self) -> None:
        self.config_unchanged()
        self.checkpoint("deploying")
        started = self.now()
        # Mark before up: a timeout can leave the new container running.
        self.deployed = True
        self.compose("up", "--detach", "--no-build", "--no-deps", self.service,
                     candidate=True, override=self.override)
        container = self.container()
        self.no_duplicate_bot(container)
        self.wait_healthy(container, self.target, started_after=started)
        after = self.snapshot(container)
        self.report["schema_after"] = after.get("schema")
        try:
            from .state_snapshot import compare
        except ImportError:  # Direct execution: python /srv/reshuffle/ops/update_bot.py
            from state_snapshot import compare
        differences = compare(self.before, after)
        if differences:
            self.report["invariant_errors"] = differences
            raise UpdateError("Book club data preservation check failed")
        self.git("merge", "--ff-only", self.target)
        self.report["state"] = "updated"
        self.stopped = False

    def rollback(self) -> None:
        if not self.stopped:
            return
        if not self.deployed:
            # The original container still owns its exact mounts/config. A stop reply may
            # have been lost; starting the same ID is idempotent and cannot create a clone.
            self.no_duplicate_bot(self.current)
            self.runner.run(["docker", "start", self.current["Id"]])
            if not self.inspect(self.current["Id"]).get("State", {}).get("Running"):
                raise UpdateError("Original container did not restart")
            self.report["recovery"] = "original-container-restarted"
            self.stopped = False
            return
        if self.deployed:
            try:
                self.compose("stop", "--timeout", "60", self.service, frozen=True)
            except UpdateError:
                # The stop can succeed while its response is lost. Inspect before deciding.
                pass
            active = self.container(frozen=True)
            if self.inspect(active["Id"]).get("State", {}).get("Running"):
                raise UpdateError("Candidate did not stop; refusing second bot instance")
            after = self.snapshot(active)
            self.report["schema_after"] = after.get("schema")
            if after.get("schema") != self.before.get("schema"):
                self.report["recovery"] = "manual-schema-change"
                return
        # Never restore a stale DB snapshot after Discord side effects. Keep live volume.
        rollback_file = Path(self.config["state_dir"]) / "rollback-compose.json"
        old_env = dict(item.split("=", 1) for item in self.current["Config"].get("Env", []) if "=" in item)
        old_revision = old_env.get("RESHUFFLE_DEPLOY_SHA", "")
        private_json(rollback_file, compose_literals({"services": {self.service: {
            "image": self.current["Image"], "environment": dict(self.frozen_env, RESHUFFLE_DEPLOY_SHA=old_revision),
            "labels": {"org.opencontainers.image.revision": old_revision},
        }}}))
        started = self.now()
        self.no_duplicate_bot(self.current)
        self.compose("up", "--detach", "--no-build", "--no-deps", self.service, override=rollback_file, frozen=True)
        previous = self.container(frozen=True)
        self.no_duplicate_bot(previous)
        if old_revision:
            self.wait_healthy(previous, old_revision, started_after=started)
            self.report["recovery"] = "previous-image-healthy-live-data"
        elif self.inspect(previous["Id"]).get("State", {}).get("Running"):
            self.report["recovery"] = "previous-image-running-manual-health-check"
        else:
            raise UpdateError("Previous image did not start")
        self.stopped = False

    def run(self) -> dict:
        try:
            self.preflight()
            self.build()
            if self.report["state"] != "unchanged":
                self.backup()
                self.deploy()
        except Exception as exc:
            self.report["failed_phase"] = self.report.get("state")
            self.report["state"] = "failed"
            self.report["error"] = str(exc) if isinstance(exc, UpdateError) else type(exc).__name__
            try:
                self.rollback()
            except Exception as rollback_error:
                self.report["recovery"] = "manual-recovery-required"
                self.report["recovery_error"] = (str(rollback_error) if isinstance(rollback_error, UpdateError)
                                                 else type(rollback_error).__name__)
        finally:
            if self.candidate:
                try:
                    self.git("worktree", "remove", str(self.candidate))
                except UpdateError:
                    self.report["worktree_cleanup"] = "manual-cleanup-required"
            self.report["finished_at"] = self.now()
            private_json(Path(self.config["state_dir"]) / "last-report.json", self.report)
        return self.report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="read-only preflight, no fetch/build/stop")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        updater = Updater(config)
        if args.check:
            updater.preflight()
            print("Preflight passed. No deployment performed.")
            return 0
        if not config["enabled"]:
            raise UpdateError("Manual updater is disabled in configuration")
        if sys.platform != "linux":
            raise UpdateError("The deployment command requires a Linux Docker host")
        os.umask(0o077)
        with deployment_lock(Path(config["state_dir"])):
            private_directory(Path(config["backup_dir"]))
            report = updater.run()
        print("Deployment: " + report["state"] + ". Report: " + str(Path(config["state_dir"]) / "last-report.json"))
        if report.get("error"):
            print(report["error"], file=sys.stderr)
        return 1 if report["state"] == "failed" else 0
    except UpdateError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
