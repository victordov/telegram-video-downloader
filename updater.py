"""
Keep yt-dlp current.

Sites change their pages all the time and yt-dlp ships fixes within days, so a
months-old yt-dlp is the most common reason downloads start failing. This
module can tell how old the installed copy is, ask PyPI for the newest one,
and upgrade it in place with pip (rolling back if the new copy will not even
import). The bot restarts itself afterwards to load the new code.

In a read-only environment such as the Docker image, the upgrade is skipped:
rebuild the image instead.
"""

import datetime as _dt
import importlib.util
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

PACKAGE = 'yt-dlp'
REQUIREMENT = 'yt-dlp[default,curl-cffi]'
PYPI_URL = 'https://pypi.org/pypi/yt-dlp/json'

_VERSION_DATE = re.compile(r'^(\d{4})\.(\d{1,2})\.(\d{1,2})')


@dataclass
class UpgradeResult:
    ok: bool
    old_version: str
    new_version: Optional[str]
    message: str

    @property
    def changed(self) -> bool:
        return self.ok and bool(self.new_version) and self.new_version != self.old_version


def installed_version() -> str:
    """Version of the yt-dlp loaded in this process."""
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:  # pragma: no cover - yt-dlp is a hard dependency
        return 'unknown'


def version_date(version: str) -> Optional[_dt.date]:
    """yt-dlp versions are dates (2026.10.26, nightlies add .HHMMSS)."""
    match = _VERSION_DATE.match(version or '')
    if not match:
        return None
    try:
        return _dt.date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def version_age_days(version: str, today: Optional[_dt.date] = None) -> Optional[int]:
    released = version_date(version)
    if released is None:
        return None
    return max(0, ((today or _dt.date.today()) - released).days)


def is_newer(candidate: Optional[str], current: str) -> bool:
    """Compare two date-style versions; unknown formats are never 'newer'."""
    if not candidate:
        return False

    def key(version: str):
        return tuple(int(part) for part in re.findall(r'\d+', version))

    try:
        return key(candidate) > key(current)
    except ValueError:
        return False


def latest_version(timeout: float = 15.0) -> Optional[str]:
    """Newest yt-dlp release on PyPI, or None when PyPI cannot be reached."""
    try:
        response = requests.get(PYPI_URL, timeout=timeout,
                                headers={'User-Agent': 'telegram-video-bot-updater'})
        response.raise_for_status()
        return response.json()['info']['version']
    except Exception as exc:
        logger.warning("Could not check PyPI for the latest yt-dlp: %s", exc)
        return None


def can_self_update() -> Tuple[bool, str]:
    """Whether pip can replace the installed yt-dlp from inside this process."""
    if getattr(sys, 'frozen', False):
        return False, "running from a frozen build"
    if importlib.util.find_spec('pip') is None:
        return False, "pip is not installed in this environment"
    spec = importlib.util.find_spec('yt_dlp')
    if spec is None or not spec.origin:
        return False, "yt-dlp location is unknown"
    site_dir = os.path.dirname(os.path.dirname(spec.origin))
    if not os.access(site_dir, os.W_OK):
        return False, f"{site_dir} is read-only (rebuild the image to update)"
    return True, "ok"


def _run_pip(*args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
           '--no-input', '--quiet', *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _version_on_disk(timeout: float = 60) -> Optional[str]:
    """Import yt-dlp in a fresh interpreter; proves the new copy actually loads."""
    try:
        proc = subprocess.run(
            [sys.executable, '-c', 'import yt_dlp, yt_dlp.version; print(yt_dlp.version.__version__)'],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        logger.error("Could not verify the yt-dlp install: %s", exc)
        return None
    if proc.returncode != 0:
        logger.error("yt-dlp does not import after the update: %s", proc.stderr.strip()[-500:])
        return None
    return proc.stdout.strip() or None


def upgrade(current: Optional[str] = None) -> UpgradeResult:
    """Upgrade yt-dlp with pip. Blocking; run it in a thread."""
    old = current or installed_version()
    allowed, reason = can_self_update()
    if not allowed:
        return UpgradeResult(False, old, None, f"Cannot update in place: {reason}.")

    logger.info("Updating yt-dlp (installed: %s)", old)
    try:
        proc = _run_pip('--upgrade', REQUIREMENT)
    except subprocess.TimeoutExpired:
        return UpgradeResult(False, old, None, "pip timed out while updating yt-dlp.")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or '').strip()[-400:]
        return UpgradeResult(False, old, None, f"pip failed: {tail or f'exit {proc.returncode}'}")

    new = _version_on_disk()
    if new is None:
        # A release that does not import would take the bot down on restart.
        logger.error("Rolling yt-dlp back to %s", old)
        _run_pip(f'{PACKAGE}=={old}')
        return UpgradeResult(False, old, None,
                             f"The new yt-dlp failed to import, so {old} was restored.")

    if new == old:
        return UpgradeResult(True, old, new, f"yt-dlp {old} is already the latest.")
    return UpgradeResult(True, old, new, f"Updated yt-dlp {old} → {new}.")
