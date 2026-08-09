"""Parent-owned ephemeral filesystem lifecycle for Browser Use workers.

This confines framework-created files to one unique system-temporary directory
per run.  It is an application/framework boundary, not mandatory OS isolation
against arbitrary Python or native code executed by a dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_MAX_ENTRIES = 4_096
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_WORKER_RESULT_BYTES = _MAX_FILE_BYTES
_PROCESS_GROUP_GRACE_S = 2.0


class SandboxError(RuntimeError):
    """The framework sandbox could not be created, inventoried, or removed."""


@dataclass(frozen=True)
class SandboxPaths:
    """Private absolute paths used only by the parent and child process."""

    root: Path
    temp_dir: Path
    config_dir: Path
    cache_dir: Path
    browser_profile_dir: Path
    downloads_dir: Path
    agent_files_dir: Path

    @classmethod
    def create(cls, *, root_created: Callable[[Path], None]) -> "SandboxPaths":
        """Create a 0700 root in the OS temporary directory, never caller-selected.

        ``root_created`` transfers ownership to the parent lifecycle immediately
        after ``mkdtemp`` succeeds and again after canonicalization, so both
        equivalent path spellings are redacted. Setup deliberately does not
        attempt its own best-effort cleanup: a setup exception must leave the
        lifecycle able to inventory, remove, and receipt the real root.
        """

        root = Path(tempfile.mkdtemp(prefix="btb-browser-use-", dir=tempfile.gettempdir()))
        root_created(root)
        root.chmod(0o700)
        root = root.resolve(strict=True)
        root_created(root)
        _require_outside_repo_and_home(root)
        paths = cls(
            root=root,
            temp_dir=root / "tmp",
            config_dir=root / "config",
            cache_dir=root / "cache",
            browser_profile_dir=root / "browser-profile",
            downloads_dir=root / "downloads",
            agent_files_dir=root / "agent-files",
        )
        for path in paths._children():
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        return paths

    def _children(self) -> tuple[Path, ...]:
        return (
            self.temp_dir,
            self.config_dir,
            self.cache_dir,
            self.browser_profile_dir,
            self.downloads_dir,
            self.agent_files_dir,
        )

    def environment(self, *, provider: str) -> dict[str, str]:
        """Return the minimal child environment, rooted inside this sandbox."""

        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "TMPDIR": str(self.temp_dir),
            "TEMP": str(self.temp_dir),
            "TMP": str(self.temp_dir),
            "BROWSER_USE_CONFIG_DIR": str(self.config_dir),
            "XDG_CONFIG_HOME": str(self.config_dir),
            "XDG_CACHE_HOME": str(self.cache_dir),
            "ANONYMIZED_TELEMETRY": "false",
            "BROWSER_USE_CLOUD_SYNC": "false",
            "BROWSER_USE_SETUP_LOGGING": "false",
            "BROWSER_USE_LOGGING_LEVEL": "WARNING",
            "BROWSER_USE_VERSION_CHECK": "false",
            "BROWSER_USE_DISABLE_EXTENSIONS": "1",
            "BTB_BROWSER_USE_SANDBOX_ROOT": str(self.root),
        }
        # The worker may need one selected provider credential.  Do not copy any
        # other parent configuration or credentials into the child process.
        credential_name = {
            "anthropic": "ANTHROPIC_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(provider)
        if credential_name and (value := os.environ.get(credential_name)):
            environment[credential_name] = value
        return environment


def _require_outside_repo_and_home(root: Path) -> None:
    protected = {
        Path.cwd().resolve(),
        Path(__file__).resolve().parents[2],
        Path.home().resolve(),
    }
    if any(_is_relative_to(root, parent) for parent in protected):
        raise SandboxError("Browser Use sandbox must be outside the repository and home")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_relative(path: Path) -> str:
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SandboxError("sandbox inventory contains an unsafe relative path")
    return path.as_posix()


def _open_flags(*, directory: bool) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise SandboxError("safe sandbox inventory requires O_NOFOLLOW support")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        if not hasattr(os, "O_DIRECTORY"):
            raise SandboxError("safe sandbox inventory requires O_DIRECTORY support")
        flags |= os.O_DIRECTORY
    return flags


def _same_entry(actual: os.stat_result, expected: os.stat_result) -> bool:
    return actual.st_dev == expected.st_dev and actual.st_ino == expected.st_ino


def _open_directory(
    name: str | Path,
    *,
    expected: os.stat_result,
    parent_fd: int | None = None,
) -> int:
    try:
        if parent_fd is None:
            descriptor = os.open(name, _open_flags(directory=True))
        else:
            descriptor = os.open(name, _open_flags(directory=True), dir_fd=parent_fd)
    except OSError as exc:
        raise SandboxError("cannot safely open sandbox directory") from exc
    actual = os.fstat(descriptor)
    if not stat.S_ISDIR(actual.st_mode) or not _same_entry(actual, expected):
        os.close(descriptor)
        raise SandboxError("sandbox directory changed while being inventoried")
    return descriptor


def _sha256_regular_file(
    name: str,
    expected: os.stat_result,
    *,
    parent_fd: int,
) -> str:
    try:
        descriptor = os.open(name, _open_flags(directory=False), dir_fd=parent_fd)
    except OSError as exc:
        raise SandboxError("cannot safely open sandbox file") from exc
    try:
        actual = os.fstat(descriptor)
        if (
            not stat.S_ISREG(actual.st_mode)
            or not _same_entry(actual, expected)
            or actual.st_size != expected.st_size
        ):
            raise SandboxError("sandbox file changed while being inventoried")
        digest = hashlib.sha256()
        remaining = expected.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk or len(chunk) > remaining:
                raise SandboxError("sandbox file changed while being inventoried")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise SandboxError("sandbox file changed while being inventoried")
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or not _same_entry(after, expected)
            or after.st_size != expected.st_size
        ):
            raise SandboxError("sandbox file changed while being inventoried")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _bounded_scandir_names(directory_fd: int, *, remaining_slots: int) -> list[str]:
    """Read at most the remaining names needed to prove the global limit.

    We need sorted output for reproducible inventories, but must not first
    materialize an arbitrary directory.  The first name beyond the available
    entry allowance is consumed only as a limit sentinel and never retained.
    """

    if remaining_slots < 0:
        raise SandboxError("sandbox inventory exceeds entry limit")
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as scanned:
            for entry in scanned:
                if len(names) >= remaining_slots:
                    raise SandboxError("sandbox inventory exceeds entry limit")
                names.append(entry.name)
    except OSError as exc:
        raise SandboxError("cannot enumerate sandbox") from exc
    return sorted(names)


def inventory_sandbox(root: Path) -> dict[str, Any]:
    """Return a bounded, lstat-only inventory without following links."""

    try:
        root_status = root.lstat()
    except OSError as exc:
        raise SandboxError("cannot lstat sandbox root") from exc
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        raise SandboxError("sandbox root is not a real directory")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    retained_names = 0

    def visit(directory_fd: int, relative: Path) -> None:
        nonlocal retained_names, total_bytes
        names = _bounded_scandir_names(
            directory_fd,
            remaining_slots=_MAX_ENTRIES - len(entries) - retained_names,
        )
        retained_names += len(names)
        names.reverse()
        try:
            while names:
                name = names.pop()
                retained_names -= 1
                child_relative = relative / name
                rendered = _safe_relative(child_relative)
                try:
                    child_status = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    raise SandboxError("cannot lstat sandbox entry") from exc
                if stat.S_ISLNK(child_status.st_mode):
                    raise SandboxError(f"sandbox inventory rejects symlink: {rendered}")
                if stat.S_ISDIR(child_status.st_mode):
                    entries.append({"path": rendered, "type": "directory"})
                    if len(entries) > _MAX_ENTRIES:
                        raise SandboxError("sandbox inventory exceeds entry limit")
                    child_fd = _open_directory(
                        name, expected=child_status, parent_fd=directory_fd
                    )
                    try:
                        visit(child_fd, child_relative)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(child_status.st_mode):
                    raise SandboxError(
                        f"sandbox inventory rejects non-regular entry: {rendered}"
                    )
                if child_status.st_size > _MAX_FILE_BYTES:
                    raise SandboxError("sandbox inventory exceeds per-file byte limit")
                total_bytes += child_status.st_size
                if total_bytes > _MAX_TOTAL_BYTES:
                    raise SandboxError("sandbox inventory exceeds total byte limit")
                entries.append(
                    {
                        "path": rendered,
                        "type": "regular_file",
                        "size_bytes": child_status.st_size,
                        "sha256": _sha256_regular_file(
                            name, child_status, parent_fd=directory_fd
                        ),
                    }
                )
                if len(entries) > _MAX_ENTRIES:
                    raise SandboxError("sandbox inventory exceeds entry limit")
        finally:
            retained_names -= len(names)

    root_fd = _open_directory(root, expected=root_status)
    try:
        visit(root_fd, Path())
    finally:
        os.close(root_fd)
    entries.sort(key=lambda entry: entry["path"])
    files = [entry for entry in entries if entry["type"] == "regular_file"]
    digest_payload = {"entries": entries}
    return {
        "version": 1,
        "entries": entries,
        "entry_count": len(entries),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "inventory_sha256": canonical_json_sha256(digest_payload),
    }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass
class SandboxLifecycle:
    """Track one child sandbox and truthfully report its terminal lifecycle."""

    on_root_created: Callable[[Path], None] | None = field(default=None, repr=False)
    root: Path | None = None
    paths: SandboxPaths | None = None
    inventory: dict[str, Any] | None = None
    cleanup_error: Exception | None = None

    def create(self) -> SandboxPaths:
        if self.root is not None:
            raise RuntimeError("sandbox already created")

        def register_root(root: Path) -> None:
            self.root = root
            if self.on_root_created is not None:
                self.on_root_created(root)

        self.paths = SandboxPaths.create(root_created=register_root)
        return self.paths

    def has_real_root(self) -> bool:
        """Return whether the parent-owned root still exists as a directory."""

        if self.root is None:
            return False
        try:
            root_status = self.root.lstat()
        except OSError:
            return False
        return stat.S_ISDIR(root_status.st_mode) and not stat.S_ISLNK(root_status.st_mode)

    def capture_inventory(self) -> dict[str, Any]:
        if not self.has_real_root() or self.root is None:
            raise RuntimeError("sandbox root is not available for inventory")
        self.inventory = inventory_sandbox(self.root)
        return self.inventory

    def cleanup(self) -> bool:
        if self.root is None:
            return True
        try:
            if os.path.lexists(self.root):
                shutil.rmtree(self.root)
            if os.path.lexists(self.root):
                raise SandboxError("sandbox still exists after cleanup")
            return True
        except Exception as exc:  # retained in the receipt as cleanup_failed
            self.cleanup_error = exc
            return False

    def receipt_state(self) -> str:
        if self.root is None:
            return "not_created"
        if self.cleanup_error is not None:
            return "cleanup_failed"
        return "cleaned"


@dataclass(frozen=True)
class WorkerResult:
    payload: dict[str, Any] | None
    timed_out: bool
    return_code: int | None
    teardown_error: str | None = None


def run_worker(
    paths: SandboxPaths,
    request: dict[str, Any],
    *,
    timeout_s: float,
    provider: str,
) -> WorkerResult:
    """Run one Browser Use worker in a new process group and enforce its timeout."""

    if timeout_s <= 0:
        raise ValueError("worker timeout must be positive")
    process = subprocess.Popen(
        [sys.executable, "-m", "btb.harness.browser_use_worker"],
        cwd=paths.root,
        env=paths.environment(provider=provider),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    teardown_error: str | None = None
    try:
        process.communicate(json.dumps(request), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            _quiesce_process_group(process)
        except SandboxError as exc:
            teardown_error = str(exc)
    payload = (
        None
        if teardown_error is not None
        else _read_worker_result(paths.root, "worker-result.json")
    )
    return WorkerResult(
        payload=payload,
        timed_out=timed_out,
        return_code=process.returncode,
        teardown_error=teardown_error,
    )


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise SandboxError("cannot inspect Browser Use child process group") from exc
    return True


def _wait_for_process_group_exit(process_group: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _signal_process_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise SandboxError("cannot signal Browser Use child process group") from exc


def _wait_for_leader_exit(
    process: subprocess.Popen[str], *, timeout_s: float
) -> bool:
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:
        raise SandboxError("cannot reap Browser Use child process") from exc
    return True


def _quiesce_process_group(process: subprocess.Popen[str]) -> None:
    """Make the child-owned session empty before parent-side inventory."""

    process_group = process.pid
    try:
        leader_running = process.poll() is None
    except OSError as exc:
        raise SandboxError("cannot inspect Browser Use child process") from exc

    # A signalled but unreaped leader remains a zombie in its process group.
    # Reap it before asking whether descendants still keep the group alive.
    if leader_running:
        _signal_process_group(process_group, 15)
        if not _wait_for_leader_exit(process, timeout_s=_PROCESS_GROUP_GRACE_S):
            _signal_process_group(process_group, 9)
            if not _wait_for_leader_exit(process, timeout_s=_PROCESS_GROUP_GRACE_S):
                raise SandboxError("Browser Use child process did not exit")

    # The reaped leader may have left a browser/driver descendant behind.  Give
    # the group a bounded graceful exit, then escalate and positively confirm
    # that no member remains before parent-side inventory starts.
    if _process_group_exists(process_group):
        _signal_process_group(process_group, 15)
        if not _wait_for_process_group_exit(process_group, timeout_s=_PROCESS_GROUP_GRACE_S):
            _signal_process_group(process_group, 9)
            if not _wait_for_process_group_exit(
                process_group, timeout_s=_PROCESS_GROUP_GRACE_S
            ):
                raise SandboxError("Browser Use child process group did not quiesce")


def _read_worker_result(root: Path, name: str) -> dict[str, Any] | None:
    """Read a bounded regular result file without following sandbox links."""

    if name != "worker-result.json":
        return None
    try:
        root_status = root.lstat()
        root_fd = _open_directory(root, expected=root_status)
    except (OSError, SandboxError):
        return None
    try:
        expected = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(expected.st_mode) or stat.S_ISLNK(expected.st_mode):
            return None
        if expected.st_size > _MAX_WORKER_RESULT_BYTES:
            return None
        descriptor = os.open(name, _open_flags(directory=False), dir_fd=root_fd)
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or not _same_entry(actual, expected)
                or actual.st_size != expected.st_size
            ):
                return None
            content = os.read(descriptor, _MAX_WORKER_RESULT_BYTES + 1)
            after = os.fstat(descriptor)
            if (
                len(content) != expected.st_size
                or len(content) > _MAX_WORKER_RESULT_BYTES
                or not _same_entry(after, expected)
                or after.st_size != expected.st_size
            ):
                return None
        finally:
            os.close(descriptor)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
