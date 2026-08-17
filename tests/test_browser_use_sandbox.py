"""Offline lifecycle tests for Browser Use's parent-owned filesystem root."""

from __future__ import annotations

import asyncio
import builtins
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from btb.harness import browser_use_sandbox as sandbox
from btb.harness import browser_use_worker as worker


@pytest.mark.parametrize("path", [Path("../escape"), Path("/absolute")])
def test_safe_relative_rejects_traversal_and_absolute_paths(path: Path) -> None:
    with pytest.raises(sandbox.SandboxError, match="unsafe relative"):
        sandbox._safe_relative(path)


def test_sandbox_root_rejects_both_cwd_and_package_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(sandbox.SandboxError, match="outside the repository"):
        sandbox._require_outside_repo_and_home(Path.cwd() / "nested-sandbox")
    monkeypatch.chdir(tmp_path)
    package_root = Path(sandbox.__file__).resolve().parents[2]
    with pytest.raises(sandbox.SandboxError, match="outside the repository"):
        sandbox._require_outside_repo_and_home(package_root / "nested-sandbox")


def test_unique_private_roots_are_outside_repo_and_home_and_cleaned() -> None:
    first = sandbox.SandboxLifecycle()
    second = sandbox.SandboxLifecycle()
    first_paths = first.create()
    second_paths = second.create()
    try:
        assert first_paths.root != second_paths.root
        assert stat.S_IMODE(first_paths.root.stat().st_mode) == 0o700
        assert not first_paths.root.is_relative_to(Path.cwd())
        assert not first_paths.root.is_relative_to(Path.home())
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o700
            for path in first_paths._children()
        )
    finally:
        assert first.cleanup() is True
        assert second.cleanup() is True
    assert not first_paths.root.exists()
    assert not second_paths.root.exists()


def test_partial_setup_root_is_parent_owned_and_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    registered: list[Path] = []
    lifecycle.on_root_created = registered.append

    def fail_child_setup(_paths: sandbox.SandboxPaths) -> tuple[Path, ...]:
        raise RuntimeError("forced partial setup failure")

    monkeypatch.setattr(sandbox.SandboxPaths, "_children", fail_child_setup)
    with pytest.raises(RuntimeError, match="forced partial setup failure"):
        lifecycle.create()

    assert lifecycle.paths is None
    assert lifecycle.root is not None
    assert registered[-1] == lifecycle.root
    assert lifecycle.has_real_root()
    assert lifecycle.capture_inventory()["entries"] == []
    root = lifecycle.root
    assert lifecycle.cleanup() is True
    assert lifecycle.receipt_state() == "cleaned"
    assert not root.exists()


def test_inventory_rejects_a_large_flat_directory_without_unbounded_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    yielded = 0

    class Entry:
        def __init__(self, index: int) -> None:
            self.name = f"entry-{index:05d}"

    class Scanned:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self) -> Entry:
            nonlocal yielded
            yielded += 1
            if yielded > sandbox._MAX_ENTRIES + 1:
                pytest.fail("inventory consumed beyond its bounded limit sentinel")
            return Entry(yielded)

    original_scandir = sandbox.os.scandir
    monkeypatch.setattr(sandbox.os, "scandir", lambda _fd: Scanned())
    try:
        with pytest.raises(sandbox.SandboxError, match="exceeds entry limit"):
            sandbox.inventory_sandbox(paths.root)
        assert yielded == sandbox._MAX_ENTRIES + 1
    finally:
        monkeypatch.setattr(sandbox.os, "scandir", original_scandir)
        assert lifecycle.cleanup() is True
        assert not os.path.lexists(paths.root)


def test_inventory_rejects_nested_scans_that_exceed_the_shared_name_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    child = paths.root / "a-child"
    later = paths.root / "z-later"
    child.mkdir()
    later.mkdir()
    root_inode = paths.root.stat().st_ino
    child_inode = child.stat().st_ino
    child_yielded = 0

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class Scanned:
        def __init__(self, names: list[str] | None = None) -> None:
            self._names = iter(names) if names is not None else None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def __iter__(self):
            return self

        def __next__(self) -> Entry:
            nonlocal child_yielded
            if self._names is not None:
                return Entry(next(self._names))
            child_yielded += 1
            if child_yielded > sandbox._MAX_ENTRIES - 1:
                pytest.fail("nested inventory consumed beyond its shared sentinel")
            return Entry(f"child-{child_yielded:05d}")

    original_scandir = sandbox.os.scandir

    def fake_scandir(directory_fd: int) -> Scanned:
        inode = os.fstat(directory_fd).st_ino
        if inode == root_inode:
            return Scanned(["a-child", "z-later"])
        if inode == child_inode:
            return Scanned()
        pytest.fail(f"unexpected inventory directory inode: {inode}")

    monkeypatch.setattr(sandbox.os, "scandir", fake_scandir)
    try:
        with pytest.raises(sandbox.SandboxError, match="exceeds entry limit"):
            sandbox.inventory_sandbox(paths.root)
        assert child_yielded == sandbox._MAX_ENTRIES - 1
    finally:
        monkeypatch.setattr(sandbox.os, "scandir", original_scandir)
        assert lifecycle.cleanup() is True
        assert not os.path.lexists(paths.root)


def test_inventory_hash_rejects_growth_after_the_expected_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    payload = b"stable"
    name = "growing.txt"
    target = paths.agent_files_dir / name
    target.write_bytes(payload)
    root_fd = sandbox._open_directory(
        paths.agent_files_dir,
        expected=paths.agent_files_dir.lstat(),
    )
    expected = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    original_read = sandbox.os.read
    read_sizes: list[int] = []
    grew = False

    def grow_then_hide_extra(descriptor: int, size: int) -> bytes:
        nonlocal grew
        read_sizes.append(size)
        if not grew:
            chunk = original_read(descriptor, size)
            with target.open("ab") as handle:
                handle.write(b"+")
            grew = True
            return chunk
        if size == 1:
            return b""
        return original_read(descriptor, size)

    monkeypatch.setattr(sandbox.os, "read", grow_then_hide_extra)
    try:
        with pytest.raises(sandbox.SandboxError, match="file changed"):
            sandbox._sha256_regular_file(name, expected, parent_fd=root_fd)
        assert read_sizes == [len(payload), 1]
    finally:
        os.close(root_fd)
        assert lifecycle.cleanup() is True
        assert not os.path.lexists(paths.root)


def test_inventory_is_sorted_hashed_and_rejects_links_and_nonregular_entries() -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    try:
        (paths.agent_files_dir / "z.txt").write_text("z", encoding="utf-8")
        (paths.agent_files_dir / "a.txt").write_text("a", encoding="utf-8")
        inventory = lifecycle.capture_inventory()
        names = [entry["path"] for entry in inventory["entries"]]
        assert names == sorted(names)
        assert inventory["file_count"] == 2
        assert inventory["total_bytes"] == 2
        assert len(inventory["inventory_sha256"]) == 64

        os.symlink(Path.home(), paths.agent_files_dir / "outside")
        with pytest.raises(sandbox.SandboxError, match="symlink"):
            sandbox.inventory_sandbox(paths.root)
        (paths.agent_files_dir / "outside").unlink()

        listener = socket.socket(socket.AF_UNIX)
        socket_path = paths.agent_files_dir / "socket"
        listener.bind(str(socket_path))
        try:
            with pytest.raises(sandbox.SandboxError, match="non-regular"):
                sandbox.inventory_sandbox(paths.root)
        finally:
            listener.close()
        socket_path.unlink()

        fifo_path = paths.agent_files_dir / "fifo"
        os.mkfifo(fifo_path)
        with pytest.raises(sandbox.SandboxError, match="non-regular"):
            sandbox.inventory_sandbox(paths.root)
    finally:
        assert lifecycle.cleanup() is True


def test_cleanup_failure_is_truthfully_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    original = sandbox.shutil.rmtree

    def fail_cleanup(path: Path) -> None:
        assert path == paths.root
        raise PermissionError("cleanup denied")

    monkeypatch.setattr(sandbox.shutil, "rmtree", fail_cleanup)
    assert lifecycle.cleanup() is False
    assert lifecycle.receipt_state() == "cleanup_failed"
    assert isinstance(lifecycle.cleanup_error, PermissionError)
    monkeypatch.setattr(sandbox.shutil, "rmtree", original)
    assert lifecycle.cleanup() is True


def test_cleanup_rejects_a_dangling_root_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    replacement_target = paths.root.parent / f"{paths.root.name}.missing-target"
    original = sandbox.shutil.rmtree

    def replace_root(path: Path) -> None:
        original(path)
        path.symlink_to(replacement_target, target_is_directory=True)

    monkeypatch.setattr(sandbox.shutil, "rmtree", replace_root)
    try:
        assert lifecycle.cleanup() is False
        assert isinstance(lifecycle.cleanup_error, sandbox.SandboxError)
        assert os.path.lexists(paths.root)
    finally:
        if os.path.lexists(paths.root):
            paths.root.unlink()


def test_worker_child_environment_and_timeout_are_parent_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    captured: dict[str, object] = {}

    class TimedOutProcess:
        pid = 12345
        returncode = None

        def communicate(self, payload: str, timeout: float) -> None:
            captured["payload"] = payload
            captured["timeout"] = timeout
            raise subprocess.TimeoutExpired("worker", timeout)

        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        assert kwargs["start_new_session"] is True
        return TimedOutProcess()

    quiesced: list[object] = []
    monkeypatch.setattr(sandbox.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        sandbox, "_quiesce_process_group", lambda process: quiesced.append(process)
    )
    try:
        result = sandbox.run_worker(
            paths,
            {"kind": "audit"},
            timeout_s=1,
            provider="openai",
        )
        environment = captured["env"]
        assert isinstance(environment, dict)
        assert captured["cwd"] == paths.root
        for name in (
            "HOME",
            "TMPDIR",
            "TEMP",
            "TMP",
            "BROWSER_USE_CONFIG_DIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "BTB_BROWSER_USE_SANDBOX_ROOT",
        ):
            assert Path(environment[name]).is_relative_to(paths.root)
        assert "PYTHONPATH" not in environment
        assert environment["ANONYMIZED_TELEMETRY"] == "false"
        assert environment["BROWSER_USE_CLOUD_SYNC"] == "false"
        assert result.timed_out is True
        assert quiesced
    finally:
        assert lifecycle.cleanup() is True


def test_worker_result_reader_rejects_symlink_and_oversize_without_reading_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    try:
        outside = paths.root.parent / f"{paths.root.name}.outside-result.json"
        monkeypatch.setattr(
            sandbox.os,
            "read",
            lambda *_args, **_kwargs: pytest.fail("unsafe result must not be read"),
        )
        outside.write_text('{"outside":"secret"}', encoding="utf-8")
        assert sandbox._read_worker_result(paths.root, "../outside-result.json") is None
        result = paths.root / "worker-result.json"
        result.symlink_to(outside)
        assert sandbox._read_worker_result(paths.root, result.name) is None
        result.unlink()
        with result.open("wb") as handle:
            handle.truncate(sandbox._MAX_WORKER_RESULT_BYTES + 1)
        assert sandbox._read_worker_result(paths.root, result.name) is None
    finally:
        assert lifecycle.cleanup() is True
        if outside.exists():
            outside.unlink()


def test_worker_result_temp_is_exclusive_and_no_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    outside = paths.root.parent / f"{paths.root.name}.outside-result"
    try:
        outside.write_text("outside", encoding="utf-8")
        (paths.root / ".worker-result.tmp").symlink_to(outside)
        monkeypatch.setenv("BTB_BROWSER_USE_SANDBOX_ROOT", str(paths.root))
        with pytest.raises(FileExistsError):
            worker._write_result({"status": "ok"})
        assert outside.read_text(encoding="utf-8") == "outside"
    finally:
        assert lifecycle.cleanup() is True
        if outside.exists():
            outside.unlink()


def test_worker_consumes_only_the_selected_credential_before_framework_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = worker.engine.BrowserUseConfig(
        provider="openai",
        model="gpt-4.1-mini",
        max_steps=1,
        wall_s=1,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "selected-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "other-secret")

    assert worker._take_provider_api_key(config, kind="run") == "selected-secret"
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["DEEPSEEK_API_KEY"] == "other-secret"

    monkeypatch.setenv("OPENAI_API_KEY", "audit-secret")
    assert (
        worker._take_provider_api_key(config, kind="audit")
        == "constructor-only-not-sent"
    )
    assert "OPENAI_API_KEY" not in os.environ


def test_worker_removes_selected_credential_before_browser_use_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = worker.engine.BrowserUseConfig(
        provider="openai",
        model="gpt-4.1-mini",
        max_steps=1,
        wall_s=1,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "import-seam-secret")
    monkeypatch.setattr(worker.engine, "_require_browser_use_version", lambda: None)
    original_import = builtins.__import__

    class BrowserUseImportObserved(Exception):
        pass

    def guarded_import(name, *args, **kwargs):
        if name == "browser_use":
            assert "OPENAI_API_KEY" not in os.environ
            raise BrowserUseImportObserved()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(BrowserUseImportObserved):
        asyncio.run(
            worker._construct_and_maybe_run(
                {
                    "kind": "audit",
                    "config": worker.engine._worker_config(config),
                }
            )
        )


def test_browser_use_filesystem_extract_and_done_style_files_stay_managed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = sandbox.SandboxLifecycle()
    paths = lifecycle.create()
    original_cwd = Path.cwd()
    outside = paths.root.parent / f"{paths.root.name}.host-secret.txt"
    outside_done = paths.root.parent / f"{paths.root.name}.done-secret.txt"
    try:
        environment = paths.environment(provider="openai")
        for name, value in environment.items():
            if name != "PATH" and not name.endswith("_API_KEY"):
                monkeypatch.setenv(name, value)
        monkeypatch.chdir(paths.root)
        from browser_use import Tools
        from browser_use.filesystem.file_system import FileSystem
        from browser_use.tools.views import DoneAction

        outside.write_text("host secret", encoding="utf-8")
        filesystem = FileSystem(paths.agent_files_dir)
        assert asyncio.run(filesystem.read_file(str(outside))) != "host secret"
        assert asyncio.run(filesystem.read_file("../host-secret.txt")) != "host secret"
        assert filesystem.display_file(str(outside)) is None
        assert asyncio.run(filesystem.write_file(str(outside), "managed"))
        assert outside.read_text(encoding="utf-8") == "host secret"
        assert filesystem.display_file(str(outside)) == "managed"

        extracted = asyncio.run(filesystem.save_extracted_content("x" * 1_000_000))
        assert filesystem.display_file(extracted) == "x" * 1_000_000
        outside_done.write_text("not an attachment", encoding="utf-8")
        tools = Tools(display_files_in_done_text=False)
        done = tools.registry.registry.actions["done"].function
        done_result = asyncio.run(
            done(
                params=DoneAction(
                    text="done",
                    files_to_display=[str(outside_done), extracted],
                ),
                file_system=filesystem,
            )
        )
        assert done_result.extracted_content == "done"
        assert done_result.attachments == [str(filesystem.get_dir() / extracted)]
        assert Path(done_result.attachments[0]).resolve().is_relative_to(paths.root)
        inventory = lifecycle.capture_inventory()
        files = {
            entry["path"]
            for entry in inventory["entries"]
            if entry["type"] == "regular_file"
        }
        assert any(path.endswith(extracted) for path in files)
    finally:
        monkeypatch.chdir(original_cwd)
        assert lifecycle.cleanup() is True
        if outside.exists():
            outside.unlink()
        if outside_done.exists():
            outside_done.unlink()


def test_process_group_is_quiesced_after_normal_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 12345
        returncode = 0

        def poll(self) -> int:
            return 0

    states = iter((True, False))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(sandbox, "_process_group_exists", lambda _group: next(states))
    monkeypatch.setattr(sandbox.os, "killpg", lambda group, signal: signals.append((group, signal)))
    sandbox._quiesce_process_group(ExitedProcess())
    assert signals == [(12345, 15)]


def test_process_group_quiescence_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OrphanedProcess:
        pid = 12345
        returncode = 0

        def poll(self) -> int:
            return 0

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(sandbox, "_process_group_exists", lambda _group: True)
    monkeypatch.setattr(
        sandbox, "_wait_for_process_group_exit", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(sandbox.os, "killpg", lambda group, signal: signals.append((group, signal)))
    with pytest.raises(sandbox.SandboxError, match="did not quiesce"):
        sandbox._quiesce_process_group(OrphanedProcess())
    assert signals == [(12345, 15), (12345, 9)]


def test_timeout_style_group_termination_reaps_a_real_sleeping_leader() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    started = time.monotonic()
    try:
        sandbox._quiesce_process_group(process)
        assert time.monotonic() - started < sandbox._PROCESS_GROUP_GRACE_S
        assert process.poll() is not None
        assert sandbox._process_group_exists(process.pid) is False
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            process.wait(timeout=sandbox._PROCESS_GROUP_GRACE_S)
