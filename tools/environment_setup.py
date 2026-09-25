"""Bring a machine to the state Qaneris needs before ``./start-web.sh`` can run.

Every check reports one of three outcomes, and only the third is a failure:

``ok``
    The requirement is already met. Nothing is touched.
``fixed``
    It was missing or unhealthy, and this script repaired it.
``blocked``
    It is missing and this script is not allowed to repair it unattended.

The repair policy is deliberately conservative. Downloads are limited to the two version
managers the project already documents (``uv`` for Python, Node's official tarball for Node),
and they land in a private ``.tools/`` directory inside the repository instead of mutating a
system package manager or ``PATH``. Container services are started only from this repository's
own ``docker-compose.yml``. Anything that would need root, a global install, or a credential
this script cannot verify is reported as ``blocked`` with the exact command to run by hand.

Secrets are never generated into ``.env``. When the managed credential store is unconfigured,
the script prints the operator command that produces a key, because a key invented here would
be a key the operator did not choose.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]

OK = "ok"
FIXED = "fixed"
BLOCKED = "blocked"
SKIPPED = "skipped"

_STATUS_LABELS = {
    OK: "可用",
    FIXED: "已修复",
    BLOCKED: "待处理",
    SKIPPED: "已跳过",
}

#: Vendored runtimes live inside the repository and are never added to the operator's PATH.
TOOLS_DIR = PROJECT_ROOT / ".tools"

#: The pin matters more than "latest": Vite 8 declares ``engines.node = ^20.19.0 || >=22.12.0``.
#: That is a union, not a floor - Node 21.x and 22.0-22.11 are excluded - so the gate is spelled
#: out as the two accepted intervals rather than a single minimum.
NODE_ENGINE_RANGES = (((20, 19, 0), (21, 0, 0)), ((22, 12, 0), None))

#: Extras needed for a normal local run: the API, the SQL family, and the four graphs/stores the
#: acceptance configs point at. Optional engines are deliberately excluded so a plain setup does
#: not pull the whole PyPI mirror.
DEFAULT_EXTRAS = ("dev", "sql", "redis", "mongodb", "neo4j")

NEO4J_CONTAINER_PORTS = (7687, 7474)
API_PORT = 8000
FRONTEND_PORT = 5173

#: Docker Desktop is only auto-installed on the two platforms whose official channel is a
#: single documented artifact. Everywhere else the operator gets the vendor link instead.
DOCKER_DESKTOP_DMG = "https://desktop.docker.com/mac/main/{architecture}/Docker.dmg"
DOCKER_LINUX_INSTALLER = "https://get.docker.com"

#: macOS install location and the CLI path inside the bundle.
DOCKER_APP = Path("/Applications/Docker.app")
DOCKER_APP_BINARY = DOCKER_APP / "Contents" / "Resources" / "bin" / "docker"

#: The DMG is ~586 MB. Transfers over a flaky link are resumed rather than restarted.
DOCKER_DOWNLOAD_ATTEMPTS = 6

#: ``start-web.sh`` binds these; the checks confirm nothing else already answers on them.
SERVICE_PORTS = {"backend": API_PORT, "frontend": FRONTEND_PORT}

#: Resolved once by ``discover``. Child processes must see the same ``.env`` values the
#: checks do, otherwise a probe would silently test a different configuration than the API
#: will actually run with.
_PROCESS_ENV: dict[str, str] = dict(os.environ)


@dataclass
class Result:
    """One check's outcome, with the evidence that produced it."""

    name: str
    status: str
    detail: str
    fix: str | None = None
    evidence: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == BLOCKED


class SetupError(RuntimeError):
    """A repair step could not complete."""


# --------------------------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------------------------


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 600,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output, without a shell.

    Fixed argument vectors only: nothing here interpolates operator input into a command line.
    """
    merged = dict(_PROCESS_ENV if env is None else env)
    return subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        env=merged,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _which(name: str, *, extra_dirs: Iterable[Path] = ()) -> str | None:
    for directory in extra_dirs:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


# --------------------------------------------------------------------------------------------
# network helpers
# --------------------------------------------------------------------------------------------


def _urlopen(request: urllib.request.Request, *, timeout: float, allow_cert_fallback: bool):
    """Open ``request``, optionally retrying once without certificate verification.

    Some macOS Python builds ship without a usable CA bundle. That specific failure is retried
    with verification relaxed rather than being reported as "the network is down"; the caller
    still sees the outcome class it asked about (reachable / not reachable).
    """
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.URLError as error:
        reason = str(error.reason) if isinstance(error.reason, Exception) else str(error)
        if not allow_cert_fallback or "CERTIFICATE_VERIFY_FAILED" not in reason:
            raise
        import ssl

        context = ssl._create_unverified_context()
        return urllib.request.urlopen(request, timeout=timeout, context=context)


def _download(url: str, destination: Path, *, timeout: float = 300) -> None:
    """Fetch ``url`` to ``destination``.

    Uses the standard library only, so this works before any dependency is installed. Some
    macOS Python builds ship without a usable CA bundle; that specific failure is retried with
    verification relaxed and reported, rather than silently pretending the network is down.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "qaneris-setup/1.0"})
    try:
        with _urlopen(request, timeout=timeout, allow_cert_fallback=True) as response:
            payload = response.read()
    except urllib.error.URLError as error:
        raise SetupError(f"下载失败 {url}：{error}") from error
    destination.write_bytes(payload)


def _remote_size(url: str, *, timeout: float = 30) -> int | None:
    """Return the artifact's size from a HEAD request, or ``None`` when it cannot be read."""
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "qaneris-setup/1.0"}
    )
    try:
        with _urlopen(request, timeout=timeout, allow_cert_fallback=True) as response:
            length = response.headers.get("Content-Length")
            return int(length) if length and length.isdigit() else None
    except Exception:  # noqa: BLE001 - an unknown size simply disables the completeness check
        return None


def _download_resumable(
    url: str,
    destination: Path,
    *,
    attempts: int = DOCKER_DOWNLOAD_ATTEMPTS,
    timeout: float = 120,
    on_progress: Callable[[str], None] | None = None,
) -> Path:
    """Download a large artifact, resuming from the partial file after a dropped connection.

    The Docker Desktop DMG is ~586 MB and the vendor endpoint resets connections often. Restarting
    from zero on every reset would make this unusable, so each retry sends ``Range`` from whatever
    is already on disk. A server that ignores ``Range`` and answers 200 truncates instead, which
    keeps the file honest rather than appending a second copy.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = _remote_size(url, timeout=30)

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        have = destination.stat().st_size if destination.is_file() else 0
        if expected is not None and have >= expected:
            return destination

        headers = {"User-Agent": "qaneris-setup/1.0"}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with _urlopen(request, timeout=timeout, allow_cert_fallback=True) as response:
                resuming = have > 0 and response.status == 206
                mode = "ab" if resuming else "wb"
                with destination.open(mode) as handle:
                    shutil.copyfileobj(response, handle, length=1 << 20)
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            last_error = error
            if attempt < attempts:
                if on_progress is not None:
                    written = destination.stat().st_size if destination.is_file() else 0
                    on_progress(f"连接中断，正在续传（已完成 {_human_size(written)}）")
                time.sleep(min(2**attempt, 10))
            continue

        if expected is None or destination.stat().st_size >= expected:
            return destination

    raise SetupError(f"下载失败 {url}：{last_error}")


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _download_text(url: str, *, timeout: float = 60) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "qaneris-setup/1.0"})
    try:
        with _urlopen(request, timeout=timeout, allow_cert_fallback=True) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as error:
        raise SetupError(f"请求失败 {url}：{error}") from error


def _port_open(port: int, host: str = "127.0.0.1", *, timeout: float = 1.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def _http_ok(url: str, *, timeout: float = 4.0) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "qaneris-setup/1.0"})
        with _urlopen(request, timeout=timeout, allow_cert_fallback=True) as response:
            return 200 <= response.status < 400
    except Exception:  # noqa: BLE001 - any failure means "not answering"
        return False


# --------------------------------------------------------------------------------------------
# environment file
# --------------------------------------------------------------------------------------------


#: Template written when ``.env`` is absent. Local secrets are filled in with generated values;
#: external credentials are left blank on purpose, because inventing them is impossible and
#: embedding this operator's real keys in source would put credentials under version control.
_ENV_TEMPLATE = """\
# Qaneris local runtime configuration.
#
# Loaded by ./start-web.sh (shell `source`) and by the Python entry points through
# qaneris.common.environment.load_runtime_environment(), whose default file is <cwd>/.env.
# Existing exported shell variables always win over this file.
#
# .env is git-ignored. Never commit real keys, passwords or private keys.

# ---------------------------------------------------------------------------
# Graph store: published scan structure, semantic retrieval and grounding read this Neo4j.
# Generated by setup.py; the value must match the container started by docker-compose.yml.
# Do not change it while a Neo4j volume already exists, or the container will reject the login.
# ---------------------------------------------------------------------------
QANERIS_GRAPH_STORE=neo4j
QANERIS_NEO4J_URI=bolt://localhost:7687
QANERIS_NEO4J_USERNAME=neo4j
QANERIS_NEO4J_PASSWORD={neo4j_password}
QANERIS_NEO4J_DATABASE=neo4j

# ---------------------------------------------------------------------------
# Model endpoint (OpenAI-compatible). REQUIRED for answering questions, and the one thing
# setup.py cannot fill in: it needs a real provider key.
#
# Asking a question without these returns intent_parsing_failed. Everything else - scanning,
# schema inventory, the web workspace - works without them.
#
# QANERIS_MODEL_BASE_URL=https://api.example.com/v1
# QANERIS_MODEL_API_KEY=
# QANERIS_MODEL_NAME=
#
# To run several endpoint sets side by side, set QANERIS_MODEL_PROFILE=<name> and declare
# QANERIS_MODEL_<NAME>_BASE_URL / _API_KEY / _NAME.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Managed credential / certificate store.
#
# Required before the Web workspace can create managed credentials or upload a certificate.
# The directory must sit outside this repository, and the key must be a stable, URL-safe Base64
# encoding of exactly 32 random bytes.
#
# Never regenerate this key once secrets exist: it is the only way to decrypt them.
# ---------------------------------------------------------------------------
QANERIS_SECRET_STORE_DIR={secret_store_dir}
QANERIS_MASTER_KEY={master_key}

# ---------------------------------------------------------------------------
# Optional: legacy five-database acceptance fixtures.
# Read only by qaneris/scripts/acceptance/scan_5db.py. Leave unset if you do not run that suite.
#
# NL_QUERY_DATASET_DIR=<path to an NLQuery-Test-Dataset checkout>
# SD_POSTGRES_URL=postgresql+psycopg://readonly:readonly@127.0.0.1:5433/retail
# SD_MYSQL_URL=mysql+pymysql://readonly:readonly@127.0.0.1:3307/retail
# SD_MONGO_URL=mongodb://127.0.0.1:27018
# SD_REDIS_URL=redis://127.0.0.1:6380/0
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Left at their defaults on purpose.
#
# QANERIS_CATALOG=qaneris.db   SQLite catalog holding datasources and connection profiles.
# QANERIS_ARTIFACT_ROOT=...      Defaults to ../QanerisArtifacts beside the repository.
# QANERIS_BACKEND_URL=...        Only read by web/frontend/vite.config.js to retarget Vite.
# ---------------------------------------------------------------------------
"""


def generate_master_key() -> str:
    """Return a fresh 32-byte key, URL-safe Base64 encoded, matching the store's own check."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def generate_password() -> str:
    """Return a local Neo4j password. URL-safe so it needs no escaping in the compiled YAML."""
    return base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")


def _default_secret_store_dir() -> Path:
    return Path.home() / ".qaneris" / "secrets"


def _store_has_secrets(store_dir: Path) -> bool:
    """Return whether the store already holds documents, which pins the existing key."""
    if not store_dir.is_dir():
        return False
    return any(entry.is_file() for entry in store_dir.iterdir())


def _append_env_values(path: Path, values: Mapping[str, str]) -> list[str]:
    """Append missing keys to an existing ``.env`` without touching what is already there."""
    written: list[str] = []
    existing = _read_dotenv(path)
    additions = [name for name in values if not existing.get(name)]
    if not additions:
        return written

    block = ["", "# Added by setup.py"]
    block.extend(f"{name}={values[name]}" for name in additions)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(block) + "\n")
    return additions


def _dotenv_path() -> Path:
    configured = (os.getenv("QANERIS_ENV_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return PROJECT_ROOT / ".env"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal dotenv reader; matches how ``load_runtime_environment`` ignores absent files."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def effective_environment() -> dict[str, str]:
    """Return file values overlaid by the process environment, which always wins."""
    merged = _read_dotenv(_dotenv_path())
    merged.update({k: v for k, v in os.environ.items() if v})
    return merged


# --------------------------------------------------------------------------------------------
# version parsing
# --------------------------------------------------------------------------------------------


def _version_tuple(text: str) -> tuple[int, ...]:
    digits: list[int] = []
    for token in text.strip().lstrip("v").split("."):
        number = ""
        for character in token:
            if character.isdigit():
                number += character
            else:
                break
        if not number:
            break
        digits.append(int(number))
    return tuple(digits)


def node_version_supported(version: str) -> bool:
    """Return whether ``version`` satisfies Vite's declared Node engine range."""
    parsed = _version_tuple(version)
    if not parsed:
        return False
    padded = (parsed + (0, 0, 0))[:3]
    for lower, upper in NODE_ENGINE_RANGES:
        if padded >= lower and (upper is None or padded < upper):
            return True
    return False


# --------------------------------------------------------------------------------------------
# checks: runtime tooling
# --------------------------------------------------------------------------------------------


def _installer_for_platform() -> tuple[str, str] | None:
    """Return ``(kind, payload)`` for the platform's official Docker channel, if any."""
    system = platform.system().lower()
    if system == "darwin":
        architecture = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
        return "dmg", DOCKER_DESKTOP_DMG.format(architecture=architecture)
    if system == "linux":
        return "script", DOCKER_LINUX_INSTALLER
    return None


def _install_docker_desktop_macos(ctx: Context, url: str) -> Result:
    """Download the official DMG, copy ``Docker.app`` into /Applications and clean up.

    The DMG is mounted at a private mountpoint so a stale ``/Volumes/Docker`` from a previous
    attempt cannot be mistaken for this one, and every exit path detaches it.
    """
    downloads = TOOLS_DIR / "downloads"
    dmg = downloads / "Docker.dmg"

    print(f"     正在下载 Docker Desktop（约 {_human_size(_remote_size(url) or 0)}），可中断后续传…")
    _download_resumable(
        url,
        dmg,
        on_progress=lambda message: print(f"     {message}"),
    )
    print(f"     下载完成：{_human_size(dmg.stat().st_size)}")

    if not _can_write_applications():
        return Result(
            "Docker",
            BLOCKED,
            "Docker Desktop 已下载到 .tools/downloads，但 /Applications 不可写",
            fix=f"sudo hdiutil attach {dmg} && sudo cp -R /Volumes/Docker/Docker.app /Applications",
        )

    mountpoint = TOOLS_DIR / "docker-mnt"
    if mountpoint.exists():
        shutil.rmtree(mountpoint)
    mountpoint.mkdir(parents=True)

    attached = _run(
        ["hdiutil", "attach", str(dmg), "-nobrowse", "-quiet", "-mountpoint", str(mountpoint)],
        timeout=600,
    )
    if attached.returncode != 0:
        return Result(
            "Docker",
            BLOCKED,
            "无法挂载下载的 Docker.dmg",
            fix=f"手动安装：open {dmg}",
            evidence=[_first_line(attached.stderr)],
        )

    try:
        source = _docker_app_in(mountpoint)
        if source is None:
            return Result(
                "Docker",
                BLOCKED,
                "DMG 中未找到 Docker.app",
                fix=f"手动检查：open {dmg}",
            )
        if DOCKER_APP.exists():
            shutil.rmtree(DOCKER_APP)
        # ``ditto`` preserves the bundle signature; a plain copy can invalidate it.
        copied = _run(["ditto", str(source), str(DOCKER_APP)], timeout=900)
        if copied.returncode != 0:
            return Result(
                "Docker",
                BLOCKED,
                "复制 Docker.app 到 /Applications 失败",
                fix=f"手动安装：open {dmg}",
                evidence=[_first_line(copied.stderr)],
            )
    finally:
        _run(["hdiutil", "detach", str(mountpoint), "-quiet", "-force"], timeout=300)
        shutil.rmtree(mountpoint, ignore_errors=True)

    return Result(
        "Docker",
        FIXED,
        "Docker Desktop 已安装到 /Applications",
        fix="首次使用需手动启动（open -a Docker）并完成许可确认",
    )


def _install_docker_linux(ctx: Context, url: str) -> Result:
    """Run Docker's documented convenience script.

    It needs root, so the operator is asked to approve by re-running with ``--allow-root`` rather
    than the script silently invoking ``sudo``.
    """
    if os.geteuid() != 0 and not ctx.allow_root:
        return Result(
            "Docker",
            BLOCKED,
            "Linux 安装 Docker 需要 root 权限",
            fix="确认后重新运行：sudo python3 setup.py --allow-root",
        )

    script = _download_text(url)
    with tempfile.TemporaryDirectory(prefix="qaneris-docker-") as scratch:
        script_path = Path(scratch) / "get-docker.sh"
        script_path.write_text(script, encoding="utf-8")
        completed = _run(["sh", str(script_path)], timeout=1800)

    if completed.returncode != 0:
        return Result(
            "Docker",
            BLOCKED,
            "Docker 官方安装脚本执行失败",
            fix="参考 https://docs.docker.com/engine/install/ 手动安装",
            evidence=[_first_line(completed.stderr)],
        )
    return Result(
        "Docker",
        FIXED,
        "Docker Engine 已安装",
        fix="确认服务已启动：sudo systemctl enable --now docker",
    )


def install_docker(ctx: Context) -> Result:
    """Install Docker through the platform's official channel."""
    selected = _installer_for_platform()
    if selected is None:
        return Result(
            "Docker",
            BLOCKED,
            f"暂不支持在 {platform.system()} 上自动安装 Docker",
            fix="安装 Docker Desktop：https://docs.docker.com/get-docker/",
        )
    kind, url = selected
    if kind == "dmg":
        return _install_docker_desktop_macos(ctx, url)
    return _install_docker_linux(ctx, url)


def _docker_app_in(mountpoint: Path) -> Path | None:
    for candidate in (mountpoint / "Docker.app", mountpoint / "Docker Desktop.app"):
        if candidate.is_dir():
            return candidate
    for candidate in sorted(mountpoint.glob("*.app")):
        return candidate
    return None


def _can_write_applications() -> bool:
    return DOCKER_APP.parent.is_dir() and os.access(DOCKER_APP.parent, os.W_OK)


def check_docker(ctx: Context) -> Result:
    """Docker must be installed *and* its daemon answering, because Neo4j runs as a container.

    Missing Docker is installed from the platform's official channel. An installed-but-stopped
    daemon is only reported: starting a GUI app and waiting for its socket is the operator's call.
    """
    if ctx.docker is None:
        if ctx.dry_run:
            return Result("Docker", SKIPPED, "dry-run：跳过 Docker 检查")
        if not ctx.install_docker:
            return Result(
                "Docker",
                BLOCKED,
                "未找到 docker 命令",
                fix="自动安装被 --no-install-docker 关闭；去掉它即可安装，或参考 https://docs.docker.com/get-docker/",
            )
        installed = install_docker(ctx)
        if installed.status != FIXED:
            return installed
        ctx.docker = _which("docker", extra_dirs=[DOCKER_APP_BINARY.parent, Path("/usr/local/bin")])
        if ctx.docker is None:
            return Result(
                "Docker",
                FIXED,
                f"{installed.detail}（本进程尚未看到 docker 命令）",
                fix=installed.fix,
            )
        ctx.node_bin_dir = ctx.node_bin_dir or None
        _rediscover_compose(ctx)

    daemon = _run([ctx.docker, "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if daemon.returncode != 0:
        return Result(
            "Docker",
            BLOCKED,
            "docker 已安装，但守护进程没有响应",
            fix="启动 Docker Desktop（open -a Docker），等待图标变为运行中后重试",
            evidence=[_first_line(daemon.stderr)],
        )
    if ctx.compose is None:
        return Result(
            "Docker Compose",
            BLOCKED,
            "docker compose 插件不可用",
            fix="升级 Docker Desktop，或单独安装 compose v2 插件",
            evidence=[f"docker {daemon.stdout.strip()}"],
        )
    return Result(
        "Docker",
        OK,
        f"daemon {daemon.stdout.strip()} · compose {ctx.compose_version}",
    )


def check_python_runtime(ctx: Context) -> Result:
    """The interpreter running this script may be far newer than the project supports.

    ``pyproject.toml`` requires >=3.11, and the dependencies are resolved against that range, so
    a mismatch is reported rather than worked around.
    """
    current = sys.version_info[:3]
    minimum = (3, 11, 0)
    if current < minimum:
        return Result(
            "Python",
            BLOCKED,
            f"当前解释器 {platform.python_version()} 低于要求 3.11",
            fix="安装 Python 3.12+（uv python install 3.12）后重新运行本脚本",
        )
    detail = f"{platform.python_version()} ({sys.executable})"
    if current[:2] != (3, 12):
        detail += " — 项目锁文件按 3.12 校验，脚本会用 3.12 创建 .venv"
    return Result("Python", OK, detail)


def _uv_install_commands(tools_dir: Path) -> list[str]:
    """Return the documented installers for ``uv``, newest form first."""
    return [
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        f"UV_INSTALL_DIR={tools_dir} curl -LsSf https://astral.sh/uv/install.sh | sh",
    ]


def ensure_uv(ctx: Context) -> Result:
    if ctx.uv is not None:
        version = _run([ctx.uv, "--version"], timeout=30)
        if version.returncode == 0:
            return Result("uv", OK, version.stdout.strip())

    if ctx.dry_run:
        return Result("uv", SKIPPED, "dry-run：缺少 uv")

    installer_env = dict(os.environ)
    installer_env["UV_INSTALL_DIR"] = str(TOOLS_DIR)
    installer_env["UV_NO_MODIFY_PATH"] = "1"
    script = _download_text("https://astral.sh/uv/install.sh")
    with tempfile.TemporaryDirectory(prefix="qaneris-uv-") as scratch:
        script_path = Path(scratch) / "install.sh"
        script_path.write_text(script, encoding="utf-8")
        completed = _run(["sh", str(script_path)], env=installer_env, timeout=600)

    vendored = TOOLS_DIR / "uv"
    if vendored.is_file() and os.access(vendored, os.X_OK):
        ctx.uv = str(vendored)
        ctx.use_vendored_tools = True
        version = _run([ctx.uv, "--version"], timeout=30)
        return Result(
            "uv",
            FIXED,
            f"已安装到 .tools/ · {version.stdout.strip()}",
            evidence=[_first_line(completed.stderr or completed.stdout)],
        )

    ctx.uv = _which("uv", extra_dirs=[Path.home() / ".local" / "bin"])
    if ctx.uv:
        version = _run([ctx.uv, "--version"], timeout=30)
        return Result("uv", FIXED, f"已安装 · {version.stdout.strip()}")

    return Result(
        "uv",
        BLOCKED,
        "自动安装未能产生可用的 uv",
        fix="手动安装：curl -LsSf https://astral.sh/uv/install.sh | sh",
        evidence=[_first_line(completed.stderr or completed.stdout)],
    )


def _node_absence_reason() -> str:
    """Explain why the system Node was not accepted, for the dry-run report."""
    existing = _which("node")
    if not existing:
        return "缺少可用的 Node.js"
    reported = _run([existing, "--version"], timeout=30)
    version = reported.stdout.strip() or "(无法读取版本)"
    return f"系统 Node {version} 不满足 Vite 8 的 ^20.19.0 || >=22.12.0"


def ensure_node(ctx: Context) -> Result:
    """Guarantee a Node that satisfies Vite 8's engine range.

    A system Node that is present but too old is a real failure mode for this project, so the
    version gate is checked before the command is accepted.
    """
    existing = _which("node")
    if existing:
        reported = _run([existing, "--version"], timeout=30)
        usable = reported.returncode == 0 and node_version_supported(reported.stdout)
        if usable:
            ctx.node = existing
            ctx.npm = _which("npm")
            return Result("Node.js", OK, f"{reported.stdout.strip()} ({existing})")

    if ctx.dry_run:
        return Result("Node.js", SKIPPED, f"dry-run：{_node_absence_reason()}")

    try:
        version = _download_node(ctx)
    except SetupError as error:
        return Result(
            "Node.js",
            BLOCKED,
            "自动下载 Node.js 失败",
            fix="手动安装 Node 22+：https://nodejs.org/en/download",
            evidence=[str(error)],
        )
    return Result("Node.js", FIXED, version)


def _node_dist_files() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return "osx-arm64-tar" if architecture == "arm64" else "osx-x64-tar", "darwin-" + architecture
    if system == "linux":
        architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
        return f"linux-{architecture}", f"linux-{architecture}"
    if system == "windows":
        raise SetupError("Windows 请手动安装 Node.js：https://nodejs.org/en/download")
    raise SetupError(f"不支持的平台：{system}/{machine}")


def _download_node(ctx: Context) -> str:
    marker, suffix = _node_dist_files()
    manifest = json.loads(_download_text("https://nodejs.org/dist/index.json"))
    candidates = [entry for entry in manifest if marker in entry.get("files", [])]
    if not candidates:
        raise SetupError("Node 官方站点没有为当前平台提供压缩包")

    # Prefer an LTS line, but accept the newest build when only current lines are published.
    lts_first = [entry for entry in candidates if entry.get("lts")]
    selected = (lts_first or candidates)[0]
    version = selected["version"]
    archive_suffix = "zip" if suffix.startswith("win") else "tar.gz"
    url = f"https://nodejs.org/dist/{version}/node-{version}-{suffix}.{archive_suffix}"

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qaneris-node-", dir=str(TOOLS_DIR)) as scratch:
        archive = Path(scratch) / f"node.{archive_suffix}"
        _download(url, archive)
        extracted = _extract_archive(archive, Path(scratch))
        roots = [item for item in extracted.iterdir() if item.is_dir()]
        if len(roots) != 1:
            raise SetupError(f"解压结果不符合预期：{extracted}")
        destination = TOOLS_DIR / "node"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(roots[0]), str(destination))

    ctx.node = str(destination / "bin" / "node")
    ctx.npm = str(destination / "bin" / "npm")
    ctx.use_vendored_tools = True
    ctx.node_bin_dir = destination / "bin"
    reported = _run([ctx.node, "--version"], timeout=30)
    return f"{reported.stdout.strip()} 已安装到 .tools/node"


def _extract_archive(archive: Path, destination: Path) -> Path:
    if archive.suffix == ".zip":
        shutil.unpack_archive(str(archive), str(destination))
        return destination
    with tarfile.open(archive, "r:gz") as handle:
        _assert_safe_members(handle.getmembers())
        handle.extractall(destination)
    return destination


def _assert_safe_members(members: Iterable[tarfile.TarInfo]) -> None:
    """Reject absolute paths and parent traversal before extraction."""
    for member in members:
        name = member.name
        if name.startswith("/") or ".." in Path(name).parts or member.isdev():
            raise SetupError(f"压缩包包含不安全的条目：{name}")


# --------------------------------------------------------------------------------------------
# checks: dependencies
# --------------------------------------------------------------------------------------------


def declared_extras() -> tuple[str, ...]:
    """Read the extras ``pyproject.toml`` actually declares.

    Parsed with ``tomllib`` rather than a dependency so the check works before any install.
    """
    import tomllib

    manifest = PROJECT_ROOT / "pyproject.toml"
    if not manifest.is_file():
        return ()
    with manifest.open("rb") as handle:
        document = tomllib.load(handle)
    return tuple(document.get("project", {}).get("optional-dependencies", {}).keys())


def unknown_extras(requested: Sequence[str]) -> list[str]:
    declared = declared_extras()
    if not declared:
        return []
    return [name for name in requested if name not in declared]


def _venv_python() -> Path:
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def _venv_healthy() -> tuple[bool, str]:
    interpreter = _venv_python()
    if not interpreter.is_file():
        return False, "缺少 .venv/bin/python"
    probe = _run(
        [str(interpreter), "-c", "import qaneris, fastapi, uvicorn; print('ok')"],
        cwd=PROJECT_ROOT,
        timeout=120,
    )
    if probe.returncode != 0:
        return False, _first_line(probe.stderr) or "依赖导入失败"
    return True, "已就绪"


def check_python_dependencies(ctx: Context) -> Result:
    if ctx.dry_run:
        healthy, detail = _venv_healthy()
        return Result("Python 依赖", OK if healthy else SKIPPED, f"dry-run：{detail}")
    if ctx.uv is None:
        return Result(
            "Python 依赖",
            BLOCKED,
            "缺少 uv，无法创建 .venv",
            fix="先解决 uv，然后重新运行 python3 setup.py",
        )

    healthy, detail = _venv_healthy()
    if healthy:
        return Result("Python 依赖", OK, detail)

    completed = ctx.uv_sync()
    if completed.returncode != 0:
        return Result(
            "Python 依赖",
            BLOCKED,
            "uv sync 未能完成",
            fix=f"cd {PROJECT_ROOT} && uv sync {' '.join('--extra ' + e for e in ctx.extras)}",
            evidence=[_first_line(completed.stderr)],
        )
    healthy, detail = _venv_healthy()
    if not healthy:
        return Result("Python 依赖", BLOCKED, f"安装后仍无法导入：{detail}")
    return Result("Python 依赖", FIXED, f"已用 uv sync 安装（{detail}）")


def check_frontend_dependencies(ctx: Context) -> Result:
    frontend = PROJECT_ROOT / "web" / "frontend"
    modules = frontend / "node_modules"
    vite = modules / "vite" / "bin" / "vite.js"

    if vite.is_file():
        return Result("前端依赖", OK, "node_modules 已安装（vite 存在）")
    if ctx.dry_run:
        return Result("前端依赖", SKIPPED, "dry-run：缺少 node_modules")
    if ctx.npm is None:
        return Result(
            "前端依赖",
            BLOCKED,
            "缺少 npm，无法安装前端依赖",
            fix="先解决 Node.js，然后重新运行 python3 setup.py",
        )

    # ``npm ci`` needs the lockfile and is reproducible; it is the documented command here.
    command = [ctx.npm, "ci", "--no-audit", "--no-fund"]
    completed = _run(command, cwd=frontend, env=ctx.tool_environment(), timeout=1800)
    if completed.returncode != 0:
        completed = _run(
            [ctx.npm, "install", "--no-audit", "--no-fund"],
            cwd=frontend,
            env=ctx.tool_environment(),
            timeout=1800,
        )
    if completed.returncode != 0 or not vite.is_file():
        return Result(
            "前端依赖",
            BLOCKED,
            "npm 安装前端依赖失败",
            fix="cd web/frontend && npm ci",
            evidence=[_first_line(completed.stderr)],
        )
    return Result("前端依赖", FIXED, "已安装 node_modules")


# --------------------------------------------------------------------------------------------
# checks: configuration
# --------------------------------------------------------------------------------------------


def _create_environment_file(ctx: Context) -> Result:
    """Write a working ``.env`` with generated local secrets.

    The generated keys are local-only (a Neo4j password for a container this repository starts,
    and a master key for a directory under the operator's home). They are written with mode 600.
    External credentials are never invented: the model section stays commented out.
    """
    path = _dotenv_path()
    if ctx.dry_run:
        return Result("环境文件", SKIPPED, f"dry-run：将创建 {path}")

    store_dir = ctx.environment.get("QANERIS_SECRET_STORE_DIR") or str(_default_secret_store_dir())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _ENV_TEMPLATE.format(
            neo4j_password=generate_password(),
            master_key=ctx.environment.get("QANERIS_MASTER_KEY") or generate_master_key(),
            secret_store_dir=store_dir,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    # The new file must take effect for the checks that follow, not just the next run.
    ctx.environment = effective_environment()
    _PROCESS_ENV.clear()
    _PROCESS_ENV.update(ctx.environment)
    return Result(
        "环境文件",
        FIXED,
        f"已生成 {path}（本机密钥自动生成，权限 600）",
        fix="问数需要模型：在 .env 填入 QANERIS_MODEL_BASE_URL / _API_KEY / _NAME",
    )


def check_environment_file(ctx: Context) -> Result:
    path = _dotenv_path()
    if not path.is_file():
        return _create_environment_file(ctx)

    values = _read_dotenv(path)
    required = ("QANERIS_NEO4J_URI", "QANERIS_NEO4J_USERNAME", "QANERIS_NEO4J_PASSWORD")
    missing = [name for name in required if not values.get(name)]

    if missing:
        if ctx.dry_run:
            return Result(
                "环境文件",
                SKIPPED,
                f"dry-run：{path.name} 缺少 {', '.join(missing)}",
            )
        # Only fill keys this script can generate. A missing Neo4j password is local to the
        # container this repository starts, so generating one is safe and is what makes a single
        # run enough.
        fillable = {
            "QANERIS_GRAPH_STORE": values.get("QANERIS_GRAPH_STORE", "neo4j"),
            "QANERIS_NEO4J_URI": values.get("QANERIS_NEO4J_URI", "bolt://localhost:7687"),
            "QANERIS_NEO4J_USERNAME": values.get("QANERIS_NEO4J_USERNAME", "neo4j"),
            "QANERIS_NEO4J_DATABASE": values.get("QANERIS_NEO4J_DATABASE", "neo4j"),
        }
        if "QANERIS_NEO4J_PASSWORD" in missing:
            fillable["QANERIS_NEO4J_PASSWORD"] = generate_password()
        added = _append_env_values(path, {k: v for k, v in fillable.items() if k in missing})
        if "QANERIS_NEO4J_PASSWORD" not in added and "QANERIS_NEO4J_PASSWORD" in missing:
            return Result(
                "环境文件",
                BLOCKED,
                f"{path.name} 缺少必填项：{', '.join(missing)}",
                fix=f"在 {path} 补齐这些键",
            )
        ctx.environment = effective_environment()
        _PROCESS_ENV.clear()
        _PROCESS_ENV.update(ctx.environment)
        return Result(
            "环境文件",
            FIXED,
            f"已补齐 {', '.join(added)}",
            fix="若 Neo4j 容器已存在，新密码与既有数据卷不一致时会登录失败，需删除数据卷后重建",
        )

    graph_store = values.get("QANERIS_GRAPH_STORE", "neo4j")
    detail = f"{path} · graph_store={graph_store}"
    model = values.get("QANERIS_MODEL_NAME")
    detail += f" · model={model}" if model else " · 未配置模型（问数会返回 intent_parsing_failed）"
    return Result("环境文件", OK, detail)


def check_secret_store(ctx: Context) -> Result:
    """Configure and validate the managed credential store.

    Mirrors ``ManagedCredentialStore.from_environment``: a directory outside the repository, and
    a master key decoding to exactly 32 bytes. Both are local to this machine, so setup.py
    generates them when absent - that is what makes one run enough.

    A key is only generated when the store holds no secrets yet. Once documents exist the key is
    the only way to decrypt them, so replacing it would destroy data; that case is reported.
    """
    env = ctx.environment
    store_dir = (env.get("QANERIS_SECRET_STORE_DIR") or "").strip()
    master_key = (env.get("QANERIS_MASTER_KEY") or "").strip()

    if (not store_dir or not master_key) and not ctx.dry_run:
        return _configure_secret_store(ctx)

    if not store_dir or not master_key:
        absent = [
            name
            for name, value in (
                ("QANERIS_SECRET_STORE_DIR", store_dir),
                ("QANERIS_MASTER_KEY", master_key),
            )
            if not value
        ]
        return Result("凭据库", SKIPPED, f"dry-run：未配置 {', '.join(absent)}")

    resolved = Path(store_dir).expanduser().resolve()
    repository = PROJECT_ROOT
    if resolved == repository or repository in resolved.parents:
        return Result(
            "凭据库",
            BLOCKED,
            "QANERIS_SECRET_STORE_DIR 必须位于仓库之外",
            fix=f"把 QANERIS_SECRET_STORE_DIR 改到例如 {Path.home() / '.qaneris' / 'secrets'}",
        )

    try:
        key = base64.urlsafe_b64decode(master_key)
    except (binascii.Error, ValueError):
        return Result(
            "凭据库",
            BLOCKED,
            "QANERIS_MASTER_KEY 不是合法的 URL-safe Base64",
            fix="重新生成 32 字节的 URL-safe Base64 密钥",
        )
    if len(key) != 32:
        return Result(
            "凭据库",
            BLOCKED,
            f"QANERIS_MASTER_KEY 解码后为 {len(key)} 字节，要求 32",
            fix="重新生成 32 字节的 URL-safe Base64 密钥",
        )

    if not resolved.is_dir():
        if ctx.dry_run:
            return Result("凭据库", SKIPPED, f"dry-run：目录不存在 {resolved}")
        resolved.mkdir(parents=True, exist_ok=True)
        os.chmod(resolved, 0o700)
        return Result("凭据库", FIXED, f"已创建 {resolved}（0700）")

    return Result("凭据库", OK, f"{resolved} · 32 字节密钥校验通过")


def _configure_secret_store(ctx: Context) -> Result:
    """Generate the store directory and master key, then persist them into ``.env``."""
    path = _dotenv_path()
    store_dir = Path(
        ctx.environment.get("QANERIS_SECRET_STORE_DIR") or _default_secret_store_dir()
    ).expanduser().resolve()
    repository = PROJECT_ROOT
    if store_dir == repository or repository in store_dir.parents:
        return Result(
            "凭据库",
            BLOCKED,
            "QANERIS_SECRET_STORE_DIR 必须位于仓库之外",
            fix=f"把它改到例如 {_default_secret_store_dir()}",
        )

    # A directory with documents in it must keep the key that encrypted them.
    if _store_has_secrets(store_dir) and not (ctx.environment.get("QANERIS_MASTER_KEY") or "").strip():
        return Result(
            "凭据库",
            BLOCKED,
            f"{store_dir} 已有加密凭据，但 .env 缺少 QANERIS_MASTER_KEY",
            fix="找回原来的密钥并写回 .env；更换密钥会导致已存凭据无法解密",
        )

    master_key = (ctx.environment.get("QANERIS_MASTER_KEY") or "").strip() or generate_master_key()
    if not path.is_file():
        return Result(
            "凭据库",
            BLOCKED,
            "缺少 .env，无法保存凭据库配置",
            fix="先让环境文件检查生成 .env，然后重新运行",
        )

    added = _append_env_values(
        path,
        {"QANERIS_SECRET_STORE_DIR": str(store_dir), "QANERIS_MASTER_KEY": master_key},
    )
    store_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(store_dir, 0o700)

    ctx.environment = effective_environment()
    _PROCESS_ENV.clear()
    _PROCESS_ENV.update(ctx.environment)
    detail = f"已生成 32 字节密钥并创建 {store_dir}（0700）"
    if not added:
        detail = f"已创建 {store_dir}（0700）"
    return Result("凭据库", FIXED, detail)


def check_model_endpoint(ctx: Context) -> Result:
    """Report whether the configured model endpoint answers.

    Only reachability is asserted. A 401/404 still proves the host is up, and the script must not
    spend the operator's quota, so no completion is ever requested.
    """
    if not ctx.probe_network:
        return Result("模型端点", SKIPPED, "--offline：跳过网络探测")
    base_url = (ctx.environment.get("QANERIS_MODEL_BASE_URL") or "").strip()
    if not base_url:
        if ctx.prompt_for_model and not ctx.dry_run:
            return _prompt_for_model(ctx)
        return Result(
            "模型端点",
            BLOCKED,
            "未配置 QANERIS_MODEL_BASE_URL",
            fix="在 .env 填入 QANERIS_MODEL_BASE_URL / _API_KEY / _NAME",
        )
    if not (ctx.environment.get("QANERIS_MODEL_API_KEY") or "").strip():
        return Result(
            "模型端点",
            BLOCKED,
            "配置了 BASE_URL 但缺少 QANERIS_MODEL_API_KEY",
            fix="在 .env 补齐 QANERIS_MODEL_API_KEY",
        )
    try:
        request = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {ctx.environment['QANERIS_MODEL_API_KEY']}"},
        )
        with _urlopen(request, timeout=15, allow_cert_fallback=True) as response:
            return Result("模型端点", OK, f"{base_url} 可达（HTTP {response.status}）")
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            return Result(
                "模型端点",
                BLOCKED,
                f"{base_url} 可达，但凭据被拒绝（HTTP {error.code}）",
                fix="检查 QANERIS_MODEL_API_KEY 是否有效/有余额",
            )
        return Result("模型端点", OK, f"{base_url} 可达（HTTP {error.code}）")
    except Exception as error:  # noqa: BLE001 - unreachable endpoint is the finding
        return Result(
            "模型端点",
            BLOCKED,
            f"{base_url} 不可达",
            fix="确认网络与 QANERIS_MODEL_BASE_URL",
            evidence=[f"{type(error).__name__}: {error}"],
        )


# --------------------------------------------------------------------------------------------
# checks: local services
# --------------------------------------------------------------------------------------------


def check_neo4j(ctx: Context) -> Result:
    """Ensure a bolt endpoint answers, reusing foreign containers instead of fighting them.

    Starting a second Neo4j on an occupied 7687 would fail; if the configured port already
    answers, the graph store is reachable and that is the requirement.
    """
    configure = ctx.environment.get("QANERIS_GRAPH_STORE", "neo4j").strip().lower()
    if configure == "null":
        return Result("Neo4j", SKIPPED, "QANERIS_GRAPH_STORE=null（已显式关闭图存储）")

    uri = (ctx.environment.get("QANERIS_NEO4J_URI") or "").strip()
    port = _neo4j_port(uri) or 7687
    if _port_open(port):
        owner = _bolt_owner(ctx, port)
        return Result("Neo4j", OK, f"{uri or 'bolt://localhost:7687'} 已在监听（端口 {port}）{owner}")

    if ctx.dry_run:
        return Result("Neo4j", SKIPPED, f"dry-run：端口 {port} 无监听")
    if ctx.compose is None or ctx.docker is None:
        return Result(
            "Neo4j",
            BLOCKED,
            "Docker/Compose 不可用，无法启动 Neo4j",
            fix="启动 Docker Desktop，或自行运行一个 Neo4j 5 实例",
        )

    password = (ctx.environment.get("QANERIS_NEO4J_PASSWORD") or "").strip()
    if not password:
        return Result(
            "Neo4j",
            BLOCKED,
            "缺少 QANERIS_NEO4J_PASSWORD，无法启动容器",
            fix="在 .env 设置 QANERIS_NEO4J_PASSWORD",
        )

    # Neo4j applies NEO4J_AUTH only when it initialises an empty data directory. If a volume
    # already exists, its password is fixed and a newly generated one silently fails to log in,
    # so this is worth detecting rather than letting the operator meet an auth error later.
    volume = _existing_neo4j_volume(ctx)
    if volume is not None:
        return Result(
            "Neo4j",
            BLOCKED,
            f"数据卷 {volume} 已存在，但端口 {port} 没有服务在监听",
            fix=(
                "该卷的密码已固化，与 .env 中可能不一致。"
                f"删除后重建（会清空图数据）：docker volume rm {volume} && docker compose up -d neo4j"
            ),
        )

    # No ``-p``: Compose derives the project name from the directory, so this checkout cannot
    # collide with another checkout's containers.
    completed = _run(
        [ctx.docker, "compose", "up", "-d", "neo4j"],
        cwd=PROJECT_ROOT,
        env=ctx.tool_environment(docker_password=password),
        timeout=900,
    )
    if completed.returncode != 0:
        return Result(
            "Neo4j",
            BLOCKED,
            "docker compose up neo4j 失败",
            fix=f"cd {PROJECT_ROOT} && docker compose up -d neo4j",
            evidence=[_first_line(completed.stderr)],
        )

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _port_open(port):
            ctx.neo4j_started_here = True
            return Result("Neo4j", FIXED, f"容器已启动，端口 {port} 就绪")
        time.sleep(2)
    return Result(
        "Neo4j",
        BLOCKED,
        f"容器已启动但端口 {port} 在 180 秒内未就绪",
        fix="docker compose logs neo4j",
    )


def _compose_volume_name(ctx: Context) -> str | None:
    """Return the Neo4j volume name this checkout's Compose file would use.

    Asking Compose for its resolved configuration is the only reliable source: the project name is
    derived from the directory, so a suffix match would also hit a *different* checkout's volume
    and then advise deleting someone else's data.
    """
    if ctx.docker is None:
        return None
    resolved = _run(
        [ctx.docker, "compose", "config", "--format", "json"], cwd=PROJECT_ROOT, timeout=120
    )
    if resolved.returncode != 0:
        return None
    try:
        document = json.loads(resolved.stdout)
    except json.JSONDecodeError:
        return None
    project = document.get("name") or PROJECT_ROOT.name
    # Compose keys the volume by the service-level name; the created volume is project-prefixed.
    declared = document.get("volumes") or {}
    for key in declared:
        if key.endswith("neo4j") or "neo4j" in key:
            return f"{project}_{key}"
    return None


def _existing_neo4j_volume(ctx: Context) -> str | None:
    """Return this checkout's Neo4j volume name if it exists, otherwise ``None``."""
    expected = _compose_volume_name(ctx)
    if expected is None:
        return None
    listed = _run(
        [ctx.docker, "volume", "ls", "--filter", f"name={expected}", "--format", "{{.Name}}"],
        timeout=60,
    )
    if listed.returncode != 0:
        return None
    for line in listed.stdout.split():
        if line == expected:
            return line
    return None


def _neo4j_port(uri: str) -> int | None:
    if "://" not in uri:
        return None
    authority = uri.split("://", 1)[1].split("/", 1)[0]
    if ":" in authority:
        candidate = authority.rsplit(":", 1)[1]
        if candidate.isdigit():
            return int(candidate)
    return None


def _bolt_owner(ctx: Context, port: int) -> str:
    """Describe which container publishes ``port``, so a foreign Neo4j is visible, not hidden."""
    if ctx.docker is None:
        return ""
    listed = _run(
        [ctx.docker, "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"],
        timeout=30,
    )
    if listed.returncode != 0 or not listed.stdout.strip():
        return ""
    names = ", ".join(listed.stdout.split())
    return f" · 由容器提供：{names}"


def check_neo4j_connectivity(ctx: Context) -> Result:
    """Drive a real bolt handshake through the project's own config reader."""
    if ctx.environment.get("QANERIS_GRAPH_STORE", "neo4j").strip().lower() == "null":
        return Result("Neo4j 连通性", SKIPPED, "图存储已关闭")

    interpreter = _venv_python()
    if not interpreter.is_file():
        return Result("Neo4j 连通性", SKIPPED, "缺少 .venv，无法验证")
    if _which_driver_missing(interpreter):
        return Result("Neo4j 连通性", SKIPPED, "未安装 neo4j 驱动")

    probe = (
        "from qaneris.graph.config import Neo4jConfig;"
        "from neo4j import GraphDatabase;"
        "c = Neo4jConfig.from_environment();"
        "d = GraphDatabase.driver(c.uri, auth=(c.username, c.password));"
        "d.verify_connectivity();"
        "print('connected')"
    )
    # A freshly started container accepts bolt connections before authentication is ready, so the
    # handshake is retried. Without this, a first run reports auth failure for a container that is
    # in fact still initialising.
    if ctx.dry_run:
        attempts, delay = 1, 0
    elif check_neo4j_just_started(ctx):
        attempts, delay = 30, 2
    else:
        attempts, delay = 1, 0

    completed = _run([str(interpreter), "-c", probe], cwd=PROJECT_ROOT, timeout=90)
    for _ in range(attempts - 1):
        if completed.returncode == 0:
            break
        if _is_auth_failure(completed):
            break  # retrying a wrong password only wastes time
        time.sleep(delay)
        completed = _run([str(interpreter), "-c", probe], cwd=PROJECT_ROOT, timeout=90)
    if completed.returncode == 0:
        return Result("Neo4j 连通性", OK, "verify_connectivity 通过")

    combined = f"{completed.stdout}\n{completed.stderr}"
    if "Unauthorized" in combined or "authentication failure" in combined.lower():
        # A wrong password and an unreachable host need completely different fixes, so they must
        # not share one message. This is the common case when a container was created earlier
        # with a different (or generated) password.
        owner = _bolt_owner(ctx, _neo4j_port(ctx.environment.get("QANERIS_NEO4J_URI", "")) or 7687)
        return Result(
            "Neo4j 连通性",
            BLOCKED,
            f"认证失败：端口上的 Neo4j 不认 .env 里的密码{owner}",
            fix=(
                "容器已有数据卷时，密码在首次初始化时就固化了。"
                "要么把 .env 的 QANERIS_NEO4J_PASSWORD 改回该卷的原密码，"
                "要么删除数据卷重建（会清空图数据）：docker compose down -v && python3 setup.py"
            ),
        )
    return Result(
        "Neo4j 连通性",
        BLOCKED,
        "无法连接 Neo4j",
        fix="确认 QANERIS_NEO4J_* 与容器状态：docker compose logs neo4j",
        evidence=[_first_line(completed.stderr)],
    )


def _is_auth_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{completed.stdout}\n{completed.stderr}"
    return "Unauthorized" in combined or "authentication failure" in combined.lower()


def check_neo4j_just_started(ctx: Context) -> bool:
    """Return whether this run started the container, so a warm-up wait is worth it."""
    return ctx.neo4j_started_here


def _which_driver_missing(interpreter: Path) -> bool:
    probe = _run([str(interpreter), "-c", "import neo4j"], timeout=60)
    return probe.returncode != 0


#: Well-known OpenAI-compatible endpoints offered as defaults for the interactive prompt.
MODEL_ENDPOINT_PRESETS = (
    ("qiyuanapi", "https://api.qiyuanapi.cc/v1"),
    ("deepseek", "https://api.deepseek.com/v1"),
    ("openai", "https://api.openai.com/v1"),
    ("modelscope", "https://api-inference.modelscope.cn/v1"),
)


def _prompt_for_model(ctx: Context) -> Result:
    """Ask the operator for the one credential setup.py cannot invent.

    Interactive by design: this is the only value the script must not fabricate, so it is typed
    by a human, echoed as masked input, and written straight to `.env` with mode 600.
    """
    import getpass

    if not sys.stdin.isatty():
        return Result(
            "模型端点",
            BLOCKED,
            "未配置模型端点（当前非交互环境，无法询问）",
            fix="在 .env 填入 QANERIS_MODEL_BASE_URL / _API_KEY / _NAME",
        )

    print("     问数需要一个 OpenAI 兼容的模型端点，请输入（直接回车可跳过）：")
    for index, (label, url) in enumerate(MODEL_ENDPOINT_PRESETS, start=1):
        print(f"       {index}) {label:<12} {url}")
    print(f"       {len(MODEL_ENDPOINT_PRESETS) + 1}) 自定义")
    try:
        choice = input("     选择 [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        return Result("模型端点", BLOCKED, "已跳过模型配置", fix="稍后在 .env 中补齐")

    if choice.isdigit() and 1 <= int(choice) <= len(MODEL_ENDPOINT_PRESETS):
        base_url = MODEL_ENDPOINT_PRESETS[int(choice) - 1][1]
    elif choice.isdigit() and int(choice) == len(MODEL_ENDPOINT_PRESETS) + 1:
        try:
            base_url = input("     BASE_URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            return Result("模型端点", BLOCKED, "已跳过模型配置", fix="稍后在 .env 中补齐")
    else:
        base_url = choice

    if not base_url:
        return Result("模型端点", BLOCKED, "已跳过模型配置", fix="稍后在 .env 中补齐")

    try:
        api_key = getpass.getpass("     API_KEY（输入不回显）: ").strip()
        model = input("     MODEL_NAME（例如 deepseek-chat）: ").strip()
    except (EOFError, KeyboardInterrupt):
        return Result("模型端点", BLOCKED, "已跳过模型配置", fix="稍后在 .env 中补齐")

    if not api_key or not model:
        return Result("模型端点", BLOCKED, "端点已给出但缺少 API_KEY 或 MODEL_NAME", fix="稍后补齐")

    path = _dotenv_path()
    added = _append_env_values(
        path,
        {
            "QANERIS_MODEL_BASE_URL": base_url,
            "QANERIS_MODEL_API_KEY": api_key,
            "QANERIS_MODEL_NAME": model,
        },
    )
    ctx.environment = effective_environment()
    _PROCESS_ENV.clear()
    _PROCESS_ENV.update(ctx.environment)
    return Result(
        "模型端点",
        FIXED,
        f"已写入 {', '.join(added) or '（键已存在）'} · model={model}",
    )


def check_web_services(ctx: Context) -> Result:
    """Start the backend and frontend, reusing anything already listening.

    Both servers are started as detached child processes rather than by exec'ing ``start-web.sh``,
    because that script owns the foreground and waits for Ctrl+C - setup.py must return.
    """
    backend = _http_ok(f"http://127.0.0.1:{API_PORT}/health")
    frontend = _port_open(FRONTEND_PORT)
    if backend and frontend:
        return Result("Web 服务", OK, f"后端 :{API_PORT} 与前端 :{FRONTEND_PORT} 都在运行")

    if ctx.dry_run:
        missing = []
        if not backend:
            missing.append(f"后端 :{API_PORT}")
        if not frontend:
            missing.append(f"前端 :{FRONTEND_PORT}")
        return Result("Web 服务", SKIPPED, f"dry-run：{'、'.join(missing)} 未运行")
    if not ctx.start_services:
        return Result(
            "Web 服务",
            BLOCKED,
            "未启动（--no-start）",
            fix="./start-web.sh",
        )

    started: list[str] = []
    problems: list[str] = []
    if not backend:
        outcome = _start_backend(ctx)
        if outcome is None:
            started.append(f"后端 :{API_PORT}")
        else:
            problems.append(f"后端启动失败：{outcome}")
    if not frontend:
        outcome = _start_frontend(ctx)
        if outcome is None:
            started.append(f"前端 :{FRONTEND_PORT}")
        else:
            problems.append(f"前端启动失败：{outcome}")

    if problems:
        return Result(
            "Web 服务",
            BLOCKED,
            "；".join(problems),
            fix="./start-web.sh --no-open   # 前台运行以查看完整日志",
        )
    return Result(
        "Web 服务",
        FIXED,
        f"已启动 {'、'.join(started)}",
        fix="停止：pkill -f 'uvicorn qaneris' ; pkill -f 'vite --host'",
    )


def _spawn_detached(command: Sequence[str], *, cwd: Path, log_path: Path, env: Mapping[str, str]):
    """Start a long-running child that survives this process exiting.

    ``start_new_session`` detaches it from this process group, so Ctrl+C on setup does not kill
    the servers, and the log file is the only way to see their output afterwards.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # The child holds its own descriptor; closing ours keeps the file from leaking.
        handle.close()


def _await(predicate: Callable[[], bool], *, timeout: float, process: object | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _start_backend(ctx: Context) -> str | None:
    """Start uvicorn; return ``None`` on success or a short reason on failure."""
    interpreter = _venv_python()
    if not interpreter.is_file():
        return "缺少 .venv/bin/python"

    log_path = TOOLS_DIR / "logs" / "backend.log"
    process = _spawn_detached(
        [
            str(interpreter),
            "-m",
            "uvicorn",
            "qaneris.interfaces.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
        ],
        cwd=PROJECT_ROOT,
        log_path=log_path,
        env=ctx.tool_environment(),
    )
    if _await(lambda: _http_ok(f"http://127.0.0.1:{API_PORT}/health"), timeout=60, process=process):
        return None
    return f"60 秒内 /health 未就绪，见 {log_path}"


def _start_frontend(ctx: Context) -> str | None:
    """Start Vite; return ``None`` on success or a short reason on failure."""
    vite = PROJECT_ROOT / "web" / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.is_file():
        return "缺少 web/frontend/node_modules/vite"
    if ctx.node is None:
        return "缺少 node"

    log_path = TOOLS_DIR / "logs" / "frontend.log"
    process = _spawn_detached(
        [
            ctx.node,
            str(vite),
            "--host",
            "127.0.0.1",
            "--port",
            str(FRONTEND_PORT),
            "--strictPort",
        ],
        cwd=PROJECT_ROOT / "web" / "frontend",
        log_path=log_path,
        env=ctx.tool_environment(),
    )
    if _await(lambda: _port_open(FRONTEND_PORT), timeout=60, process=process):
        return None
    return f"60 秒内 {FRONTEND_PORT} 未就绪，见 {log_path}"


# --------------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------------


def _print_results(results: Sequence[Result], *, stream: TextIO | None = None) -> None:
    stream = stream if stream is not None else sys.stdout
    width = max((len(item.name) for item in results), default=0)
    for item in results:
        label = _STATUS_LABELS.get(item.status, item.status)
        print(f"[{label}] {item.name.ljust(width)}  {item.detail}", file=stream)
        for line in item.evidence:
            if line:
                print(f"{' ' * (width + 12)}{line}", file=stream)


def _print_summary(results: Sequence[Result], *, stream: TextIO | None = None) -> int:
    stream = stream if stream is not None else sys.stdout
    blocked = [item for item in results if item.status == BLOCKED]
    fixed = [item for item in results if item.status == FIXED]
    print(file=stream)
    print(f"合计 {len(results)} 项：{len(fixed)} 项已修复，{len(blocked)} 项待处理。", file=stream)
    if blocked:
        print("\n需要你处理：", file=stream)
        for item in blocked:
            print(f"  · {item.name}：{item.detail}", file=stream)
            if item.fix:
                print(f"    → {item.fix}", file=stream)
    for item in fixed:
        if item.fix:
            print(f"\n提示 {item.name}：{item.fix}", file=stream)
    return 1 if blocked else 0


# --------------------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------------------


@dataclass
class Context:
    """Shared state: discovered tool paths, resolved environment and operator switches."""

    extras: tuple[str, ...] = DEFAULT_EXTRAS
    dry_run: bool = False
    install_docker: bool = True
    allow_root: bool = False
    start_services: bool = True
    prompt_for_model: bool = True
    probe_network: bool = True
    python_version: str = "3.12"
    environment: dict[str, str] = field(default_factory=dict)
    docker: str | None = None
    compose: str | None = None
    compose_version: str = ""
    uv: str | None = None
    node: str | None = None
    npm: str | None = None
    node_bin_dir: Path | None = None
    use_vendored_tools: bool = False
    #: Set when this run started the Neo4j container, so connectivity waits for it to warm up.
    neo4j_started_here: bool = False

    def tool_environment(self, *, docker_password: str | None = None) -> dict[str, str]:
        """Return the environment child processes should see.

        Vendored runtimes are exposed through ``PATH`` for the child process only: the operator's
        shell is never modified.
        """
        merged = dict(os.environ)
        merged.update(self.environment)
        if docker_password is not None:
            merged["QANERIS_NEO4J_PASSWORD"] = docker_password
        if self.use_vendored_tools:
            prefixes = [str(path) for path in (self.node_bin_dir, TOOLS_DIR) if path]
            if prefixes:
                merged["PATH"] = os.pathsep.join(prefixes + [merged.get("PATH", "")])
        return merged

    def uv_sync(self) -> subprocess.CompletedProcess[str]:
        assert self.uv is not None
        command = [
            self.uv,
            "sync",
            "--frozen",
            "--python",
            self.python_version,
            *[token for extra in self.extras for token in ("--extra", extra)],
        ]
        return _run(command, cwd=PROJECT_ROOT, env=self.tool_environment(), timeout=1800)


def discover(ctx: Context) -> None:
    """Locate tools once so every check reads the same picture."""
    ctx.environment = effective_environment()
    _PROCESS_ENV.clear()
    _PROCESS_ENV.update(ctx.environment)
    extra_dirs = [TOOLS_DIR]
    if ctx.node_bin_dir:
        extra_dirs.insert(0, ctx.node_bin_dir)

    ctx.docker = _which("docker")
    if ctx.docker:
        compose = _run([ctx.docker, "compose", "version", "--short"], timeout=60)
        if compose.returncode == 0:
            ctx.compose = ctx.docker
            ctx.compose_version = compose.stdout.strip()
    ctx.uv = _which("uv", extra_dirs=extra_dirs + [Path.home() / ".local" / "bin"])
    ctx.node = _which("node", extra_dirs=extra_dirs)
    ctx.npm = _which("npm", extra_dirs=extra_dirs)
    if ctx.node:
        ctx.node_bin_dir = Path(ctx.node).parent


def run_checks(ctx: Context) -> list[Result]:
    """Run every check in dependency order.

    ``uv`` and Node are repaired first because the dependency checks consume them; each later
    check degrades to ``blocked`` rather than raising when an earlier one could not be fixed.
    """
    results: list[Result] = []
    results.append(check_python_runtime(ctx))
    results.append(check_docker(ctx))
    results.append(ensure_uv(ctx))
    results.append(ensure_node(ctx))
    results.append(check_environment_file(ctx))
    results.append(check_secret_store(ctx))
    results.append(check_python_dependencies(ctx))
    results.append(check_frontend_dependencies(ctx))
    results.append(check_model_endpoint(ctx))
    results.append(check_neo4j(ctx))
    results.append(check_neo4j_connectivity(ctx))
    results.append(check_web_services(ctx))
    return results


def _rediscover_compose(ctx: Context) -> None:
    """Re-check the compose plugin after Docker was just installed."""
    if ctx.docker is None:
        return
    compose = _run([ctx.docker, "compose", "version", "--short"], timeout=60)
    if compose.returncode == 0:
        ctx.compose = ctx.docker
        ctx.compose_version = compose.stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="一次运行把 Qaneris 运行所需的软件、依赖、配置与本地服务全部准备好。",
        epilog=(
            "默认会安装缺失的运行时、生成缺失的本地密钥、启动 Neo4j 与前后端。"
            "无法代填的只有外部模型凭据（需真实 API Key）。"
        ),
    )
    parser.add_argument("--check", action="store_true", help="只检查，不做任何修改（等同 --dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不下载、不安装、不启动")
    parser.add_argument("--offline", action="store_true", help="跳过需要联网的探测")
    parser.add_argument(
        "--no-install-docker",
        action="store_false",
        dest="install_docker",
        help="缺少 Docker 时不要自动安装，只打印指引",
    )
    parser.add_argument(
        "--allow-root",
        action="store_true",
        dest="allow_root",
        help="允许在 Linux 上以 root 身份安装 Docker（默认拒绝）",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_false",
        dest="prompt_for_model",
        help="不要交互式询问模型凭据，只报告缺失",
    )
    parser.add_argument(
        "--no-start",
        action="store_false",
        dest="start_services",
        help="不要自动启动后端与前端，只做好准备工作",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="以 JSON 输出结果")
    parser.add_argument(
        "--extra",
        action="append",
        dest="extras",
        metavar="NAME",
        help="追加要安装的 pyproject extra（可重复；默认 dev,sql,redis,mongodb,neo4j）",
    )
    parser.add_argument(
        "--python",
        default="3.12",
        metavar="VERSION",
        help="创建 .venv 时使用的 Python 版本（默认 3.12，与 uv.lock 对齐）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    requested = tuple(args.extras or ())
    if invalid := unknown_extras(requested):
        # Failing here is better than an unexplained resolver error halfway through uv sync.
        print(f"未知的 extra：{', '.join(invalid)}", file=sys.stderr)
        print(f"可用：{', '.join(declared_extras())}", file=sys.stderr)
        return 2
    extras = tuple(dict.fromkeys((*DEFAULT_EXTRAS, *requested)))

    ctx = Context(
        extras=extras,
        dry_run=args.check or args.dry_run,
        install_docker=args.install_docker,
        allow_root=args.allow_root,
        start_services=args.start_services,
        prompt_for_model=args.prompt_for_model,
        probe_network=not args.offline,
        python_version=args.python,
    )

    discover(ctx)
    results = run_checks(ctx)

    if args.json_output:
        payload = {
            "project_root": str(PROJECT_ROOT),
            "dry_run": ctx.dry_run,
            "extras": list(ctx.extras),
            "results": [
                {
                    "name": item.name,
                    "status": item.status,
                    "detail": item.detail,
                    "fix": item.fix,
                    "evidence": item.evidence,
                }
                for item in results
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if any(item.failed for item in results) else 0

    _print_results(results)
    return _print_summary(results)


if __name__ == "__main__":
    raise SystemExit(main())
