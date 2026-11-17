#!/usr/bin/env python3
"""Poll main and deploy only a successful CI revision through the existing updater."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .update_bot import Updater, UpdateError, deployment_lock, load_config, private_directory, private_json
except ImportError:
    from update_bot import Updater, UpdateError, deployment_lock, load_config, private_directory, private_json


REPOSITORY = "delta-dashed/DeShuffle"
WORKFLOW = "ci.yml"
REPORT = "automatic-report.json"


def workflow_status(commit: str) -> dict:
    """Public-repository API: no GitHub or SSH credentials on the polling service."""
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise UpdateError("Invalid candidate commit")
    query = urlencode({"branch": "main", "head_sha": commit, "per_page": 30})
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?{query}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "DeShuffle-auto-updater",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("oversized response")
        data = json.loads(payload)
        runs = data["workflow_runs"]
        if not isinstance(runs, list):
            raise ValueError("invalid runs")
    except HTTPError as exc:
        if exc.code == 404:
            return {"state": "waiting_ci", "reason": "Workflow is not available yet"}
        raise UpdateError("GitHub CI check unavailable (HTTP " + str(exc.code) + ")") from exc
    except (OSError, URLError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError("Cannot verify GitHub CI; running bot was not changed") from exc
    return evaluate_runs(runs, commit)


def evaluate_runs(runs: list, commit: str) -> dict:
    matching = [run for run in runs if isinstance(run, dict)
                and run.get("head_sha") == commit and run.get("head_branch") == "main"
                and run.get("event") in {"push", "workflow_dispatch"}
                and (run.get("head_repository") or {}).get("full_name", "").lower() == REPOSITORY.lower()
                and run.get("path", "").split("@", 1)[0] == ".github/workflows/" + WORKFLOW]
    if not matching:
        return {"state": "waiting_ci", "reason": "No main workflow run for this exact commit"}
    # A newer failed/in-progress retry must not be bypassed using an older success.
    run = max(matching, key=lambda item: (item.get("id", 0), item.get("run_attempt", 1)))
    result = {"run_id": run.get("id"), "run_url": run.get("html_url"), "conclusion": run.get("conclusion")}
    if run.get("status") != "completed":
        return {**result, "state": "waiting_ci"}
    return {**result, "state": "approved" if run.get("conclusion") == "success" else "blocked_ci"}


class AutomaticUpdater:
    def __init__(self, config, *, updater_factory=Updater, check_ci=workflow_status, now=time.time):
        self.config, self.updater_factory, self.check_ci, self.now = config, updater_factory, check_ci, now
        self.path = Path(config["state_dir"]) / REPORT

    def previous(self):
        if not self.path.exists():
            return {}
        try:
            result = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ValueError("invalid report")
            return result
        except (OSError, ValueError) as exc:
            raise UpdateError("Automatic deployment journal is unreadable; inspect it before retrying") from exc

    def save(self, state, **fields):
        report = {"state": state, "checked_at": self.now(), **fields}
        private_json(self.path, report)
        return report

    def run_once(self, *, retry_failed=False):
        if not self.config["enabled"] or not self.config.get("automatic_enabled", False):
            return self.save("disabled")
        previous = self.previous()
        updater = self.updater_factory(self.config)
        updater.preflight()  # Clean main, approved remote, same account, mounts and no active import.
        updater.git("fetch", "--prune", self.config["remote"], "main")
        target = updater.git("rev-parse", self.config["remote"] + "/main")
        if not re.fullmatch(r"[a-f0-9]{40}", target):
            raise UpdateError("Remote main did not resolve to a commit")
        updater.git("merge-base", "--is-ancestor", updater.old_sha, target)
        deployed = (updater.current.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
        if deployed == target and updater.old_sha == target:
            return self.save("unchanged", target_commit=target)
        same_failed = previous.get("blocked_commit") == target or (
            previous.get("state") == "deploying" and previous.get("target_commit") == target)
        if same_failed and not retry_failed:
            return self.save("blocked_release", target_commit=target, blocked_commit=target,
                             reason="Previous deployment failed or was interrupted; explicit retry required")
        ci = self.check_ci(target)
        if ci["state"] != "approved":
            return self.save(ci["state"], target_commit=target, ci=ci)
        updater.expected_commit = target
        self.save("deploying", target_commit=target, ci=ci)
        report = updater.run()  # Fetch again, require the exact approved SHA, then build/backup/deploy.
        if report["state"] == "failed":
            return self.save("failed", target_commit=target, blocked_commit=target, ci=ci,
                             deployment_report=str(Path(self.config["state_dir"]) / "last-report.json"),
                             reason=report.get("error", "Deployment failed"))
        return self.save(report["state"], target_commit=target, ci=ci,
                         backup=report.get("backup"), deployment_report=str(Path(self.config["state_dir"]) / "last-report.json"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--retry-failed", action="store_true", help="explicitly retry the blocked revision after inspection")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if not isinstance(config.get("automatic_enabled", False), bool):
            raise UpdateError("automatic_enabled must be boolean")
        if sys.platform != "linux":
            raise UpdateError("Automatic deployment requires the Linux Docker host")
        os.umask(0o077)
        with deployment_lock(Path(config["state_dir"])):
            private_directory(Path(config["backup_dir"]))
            report = AutomaticUpdater(config).run_once(retry_failed=args.retry_failed)
        print("Automatic deployment: " + report["state"] + ". Report: " + str(Path(config["state_dir"]) / REPORT))
        return 1 if report["state"] in {"failed", "blocked_release", "blocked_ci"} else 0
    except UpdateError as exc:
        # Keep the last deploy journal intact, especially after an interrupted release.
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
