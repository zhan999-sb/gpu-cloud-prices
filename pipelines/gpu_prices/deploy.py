"""Explicit, opt-in Cloudflare Pages publication. No credentials at import time."""

import logging
import os
from pathlib import Path
import re
import shutil
import subprocess

from .store import ROOT

LOG = logging.getLogger(__name__)
# Stable release from 2025-10-23, deliberately pinned (never @latest).
# https://github.com/cloudflare/workers-sdk/releases/tag/wrangler%404.45.0
WRANGLER_VERSION = "4.45.0"
CREDENTIAL_KEYS = ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")


class DeployError(RuntimeError):
    """Only fixed, secret-free messages may cross the CLI boundary."""


def deploy_command(project: str, *, executable: str = "npx") -> list[str]:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", project):
        raise DeployError("Invalid Pages project name; use 1-63 lowercase letters, digits and internal hyphens.")
    return [executable, "--yes", f"wrangler@{WRANGLER_VERSION}", "pages", "deploy", "site",
            "--project-name", project, "--branch", "main"]


def deploy_site(project: str, *, root: Path = ROOT, environ=None, runner=None, dotenv_loader=None) -> dict:
    """Secrets travel only in the child's environment; child output stays private.

    An already configured process environment avoids reading .env at all. The
    real file is read only when the user explicitly invokes this deploy command.
    Tests inject a loader and runner; neither npx nor live credentials is needed.
    """
    command = deploy_command(project)
    root = Path(root)
    if any(not (root / "site" / name).is_file() for name in ("index.html", "sitemap.xml")):
        raise DeployError("Refusing deployment: site/index.html and site/sitemap.xml must exist; build first.")
    environment = dict(os.environ if environ is None else environ)
    credentials = {key: environment.get(key, "") for key in CREDENTIAL_KEYS}
    if not all(value.strip() for value in credentials.values()):
        try:
            if dotenv_loader is None:
                from orchestrator.dotenv import parse_file
                dotenv_loader = parse_file
            configured = dotenv_loader(root / ".env")
            for key in CREDENTIAL_KEYS:
                if not credentials[key].strip():
                    credentials[key] = configured.get(key, "")
        except (OSError, ValueError, ImportError):
            raise DeployError("Cannot load deployment credentials; set both CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the process environment or a readable repo .env.") from None
    if not all(value.strip() for value in credentials.values()):
        raise DeployError("Refusing deployment: CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are both required.")
    if any(any(c in value for c in "\r\n\x00") for value in credentials.values()):
        raise DeployError("Refusing deployment: credentials must be single-line values.")
    environment.update(credentials)
    environment["WRANGLER_SEND_METRICS"] = "false"
    if runner is None:
        executable = shutil.which("npx")
        if not executable:
            raise DeployError("npx is unavailable; install Node.js with npm before deploying.")
        command[0] = executable  # Resolves npx.cmd on Windows, without shell=True.
        runner = subprocess.run
    LOG.info("Deploying site/ with wrangler@%s on branch main; subprocess output is withheld.", WRANGLER_VERSION)
    try:
        result = runner(command, cwd=root, env=environment, shell=False, check=False,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        raise DeployError("Wrangler could not run; subprocess details are withheld to protect credentials.") from None
    if result.returncode != 0:
        raise DeployError("Wrangler deployment failed; subprocess output is withheld to protect credentials.")
    LOG.info("Cloudflare Pages deployment completed.")
    return {"status": "complete", "project": project, "branch": "main", "wrangler_version": WRANGLER_VERSION}
