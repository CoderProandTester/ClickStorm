"""GitHub release updater for ClickStorm.

The updater only talks to the project's own repository over HTTPS with the
standard library, so the app keeps its no-dependency promise.  Nothing here
touches Tk: the dialog runs ``fetch_latest_release`` and ``download_installer``
on a worker thread and posts results back to the UI thread.
"""

from __future__ import annotations

import json
import os
import platform
import re
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import APP_VERSION

REPOSITORY = "CoderProandTester/ClickStorm"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"{REPOSITORY_URL}/releases/latest"
USER_AGENT = f"ClickStorm/{APP_VERSION} (+{REPOSITORY_URL})"
TIMEOUT_SECONDS = 12.0
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
ARCH_NAMES = {"x64": "64-bit", "x86": "32-bit", "arm64": "ARM64"}


class UpdateError(Exception):
    """A user-facing update failure."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int

    @property
    def size_text(self) -> str:
        if self.size <= 0:
            return "size unknown"
        return f"{self.size / (1024 * 1024):.1f} MB"


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: tuple[int, ...]
    name: str
    page_url: str
    notes: str
    published_at: str
    assets: tuple[ReleaseAsset, ...]


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)")


def parse_version(text: str) -> tuple[int, ...]:
    """Turn ``v3.0.0`` / ``3.0`` / ``3.0.0-beta`` into a comparable tuple."""
    numbers = _VERSION_RE.findall(text or "")
    if not numbers:
        return (0,)
    parts = [int(part) for part in numbers[:4]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------

def current_architecture() -> str:
    """Return ``x64``, ``x86`` or ``arm64`` for this process."""
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("amd64", "x86_64", "x64"):
        return "x64"
    if machine in ("x86", "i386", "i686", "i586"):
        return "x86"
    # Fall back to the pointer width: a 32-bit build reports a 4-byte pointer
    # even on a 64-bit machine, which matches the installer it can run.
    return "x64" if struct.calcsize("P") == 8 else "x86"


def asset_architecture(name: str) -> str:
    lowered = (name or "").lower()
    if "arm64" in lowered:
        return "arm64"
    if "x64" in lowered or "amd64" in lowered:
        return "x64"
    if "x86" in lowered or "win32" in lowered:
        return "x86"
    return ""


def preferred_asset_index(assets, arch: str | None = None) -> int:
    """Index of the installer that matches this PC, or 0."""
    arch = arch or current_architecture()
    for index, asset in enumerate(assets):
        if asset_architecture(asset.name) == arch:
            return index
    return 0


# ---------------------------------------------------------------------------
# Release lookup
# ---------------------------------------------------------------------------

def _request(url: str) -> urllib.request.Request:
    if not url.startswith("https://"):
        raise UpdateError("Refusing a non-HTTPS update URL.")
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _is_installer(name: str) -> bool:
    return name.lower().endswith(".exe") and "setup" in name.lower()


def fetch_latest_release(timeout: float = TIMEOUT_SECONDS) -> ReleaseInfo:
    """Read the latest published (non-draft, non-prerelease) GitHub release."""
    try:
        with urllib.request.urlopen(_request(LATEST_RELEASE_URL), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                "No published release was found for this repository yet."
            ) from exc
        if exc.code == 403:
            raise UpdateError(
                "GitHub is rate-limiting update checks from this network. "
                "Try again later or download the installer from the releases page."
            ) from exc
        raise UpdateError(f"GitHub returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(
            f"Could not reach GitHub: {exc.reason}"
        ) from exc
    except Exception as exc:
        raise UpdateError(f"Could not read the update information: {exc}") from exc

    if not isinstance(payload, dict):
        raise UpdateError("GitHub sent an unexpected update response.")

    tag = str(payload.get("tag_name") or payload.get("name") or "")
    assets = []
    for item in payload.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if not name or not url or not _is_installer(name):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_DOWNLOAD_BYTES:
            continue
        assets.append(ReleaseAsset(name=name, download_url=url, size=size))

    return ReleaseInfo(
        tag=tag or "latest",
        version=parse_version(tag),
        name=str(payload.get("name") or tag or "latest"),
        page_url=str(payload.get("html_url") or RELEASES_PAGE),
        notes=str(payload.get("body") or ""),
        published_at=str(payload.get("published_at") or ""),
        assets=tuple(assets),
    )


def preferred_asset(assets) -> ReleaseAsset | None:
    if not assets:
        return None
    return assets[preferred_asset_index(assets)]


# ---------------------------------------------------------------------------
# Download / install
# ---------------------------------------------------------------------------

def _safe_name(url: str) -> str:
    name = os.path.basename(url.split("?")[0])
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name.lower().endswith(".exe") or "setup" not in name.lower():
        raise UpdateError("The selected release asset is not a ClickStorm installer.")
    return name


def download_installer(asset: ReleaseAsset, on_progress=None) -> Path:
    """Download a release asset into a temporary folder and return its path."""
    name = _safe_name(asset.download_url)
    target_dir = Path(tempfile.mkdtemp(prefix="clickstorm-update-"))
    target = target_dir / name
    downloaded = 0
    try:
        with urllib.request.urlopen(
            _request(asset.download_url), timeout=TIMEOUT_SECONDS
        ) as response, target.open("wb") as handle:
            while True:
                block = response.read(CHUNK_BYTES)
                if not block:
                    break
                downloaded += len(block)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    raise UpdateError("The update is larger than expected; download cancelled.")
                handle.write(block)
                if on_progress is not None:
                    on_progress(downloaded, asset.size)
    except UpdateError:
        raise
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"Download failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Download failed: {exc.reason}") from exc
    except Exception as exc:
        raise UpdateError(f"Download failed: {exc}") from exc

    if downloaded <= 0:
        raise UpdateError("The downloaded installer was empty.")
    if asset.size > 0 and downloaded != asset.size:
        raise UpdateError(
            "The installer download was incomplete. Please try again."
        )
    return target


def launch_installer(path: Path) -> None:
    """Start a downloaded installer as its own process."""
    installer = str(path)
    if not installer.lower().endswith(".exe") or not os.path.isfile(installer):
        raise UpdateError("The installer file is missing.")
    try:
        # /CLOSEAPPLICATIONS lets Inno close ClickStorm itself if it is still
        # shutting down, instead of stopping on a "file in use" prompt.
        subprocess.Popen(
            [installer, "/NORESTART", "/CLOSEAPPLICATIONS", "/SP-"],
            close_fds=True,
        )
    except Exception as exc:
        raise UpdateError(f"Could not start the installer: {exc}") from exc
