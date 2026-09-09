"""Unit tests for the `service` CLI PID-file lifecycle (CODE_REVIEW §1.7)."""

import asyncio
import os
import sys
from pathlib import Path


from bambu_monitor.cli import commands


# --- _is_pid_alive -----------------------------------------------------------


def test_is_pid_alive_for_current_process():
    assert commands._is_pid_alive(os.getpid()) is True


def test_is_pid_alive_for_dead_process(monkeypatch):
    def raise_lookup(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(os, "kill", raise_lookup)
    assert commands._is_pid_alive(12345) is False


def test_is_pid_alive_treats_eperm_as_alive(monkeypatch):
    """EPERM means the process exists but is owned by another user — not dead."""
    def raise_eperm(pid, sig):
        raise PermissionError()

    monkeypatch.setattr(os, "kill", raise_eperm)
    assert commands._is_pid_alive(12345) is True


# --- _pid_is_our_daemon -------------------------------------------------------


def test_pid_is_our_daemon_matches_spawned_command(monkeypatch):
    monkeypatch.setattr(
        commands,
        "_process_cmdline",
        lambda pid: f"{sys.executable} -m bambu_monitor.main run --port 8000",
    )
    assert commands._pid_is_our_daemon(999) is True


def test_pid_is_our_daemon_rejects_foreign_process(monkeypatch):
    monkeypatch.setattr(commands, "_process_cmdline", lambda pid: "/usr/sbin/ntpd -n")
    assert commands._pid_is_our_daemon(999) is False


def test_pid_is_our_daemon_unknown_when_cmdline_unavailable(monkeypatch):
    monkeypatch.setattr(commands, "_process_cmdline", lambda pid: None)
    assert commands._pid_is_our_daemon(999) is None


# --- Path anchoring -----------------------------------------------------------


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(commands.STATE_DIR_ENV, str(tmp_path))
    assert commands._pid_file() == tmp_path / "bambu-monitor.pid"
    assert commands._log_file() == tmp_path / "bambu-monitor.log"


def test_state_dir_default_is_absolute_not_cwd_relative(monkeypatch):
    monkeypatch.delenv(commands.STATE_DIR_ENV, raising=False)
    state_dir = commands._state_dir()
    assert state_dir.is_absolute()
    assert commands._pid_file().is_absolute()
    assert commands._log_file().is_absolute()


# --- _get_running_pid ---------------------------------------------------------


def test_get_running_pid_returns_live_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv(commands.STATE_DIR_ENV, str(tmp_path))
    (tmp_path / "bambu-monitor.pid").write_text("4242")
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(commands, "_pid_is_our_daemon", lambda pid: True)
    assert commands._get_running_pid() == 4242


def test_get_running_pid_stale_pid_reuse_reads_as_not_running(monkeypatch, tmp_path):
    """A live but foreign process recorded in the PID file is not our daemon."""
    monkeypatch.setenv(commands.STATE_DIR_ENV, str(tmp_path))
    (tmp_path / "bambu-monitor.pid").write_text("4242")
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(commands, "_pid_is_our_daemon", lambda pid: False)
    assert commands._get_running_pid() is None


def test_get_running_pid_dead_process(monkeypatch, tmp_path):
    monkeypatch.setenv(commands.STATE_DIR_ENV, str(tmp_path))
    (tmp_path / "bambu-monitor.pid").write_text("4242")
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: False)
    assert commands._get_running_pid() is None


# --- cmd_service_stop ---------------------------------------------------------


def _setup_pid_file(monkeypatch, tmp_path, pid: int) -> Path:
    monkeypatch.setenv(commands.STATE_DIR_ENV, str(tmp_path))
    pid_file = tmp_path / "bambu-monitor.pid"
    pid_file.write_text(str(pid))
    return pid_file


def test_service_stop_refuses_to_kill_reused_pid(monkeypatch, tmp_path):
    """PID reuse: the recorded PID now belongs to another process — never signal it."""
    pid_file = _setup_pid_file(monkeypatch, tmp_path, 4242)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(commands, "_pid_is_our_daemon", lambda pid: False)

    asyncio.run(commands.cmd_service_stop())

    assert kills == []  # nothing was signalled
    assert not pid_file.exists()  # stale PID file cleaned up


def test_service_stop_stops_verified_daemon(monkeypatch, tmp_path):
    pid_file = _setup_pid_file(monkeypatch, tmp_path, 4242)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(commands, "_pid_is_our_daemon", lambda pid: True)
    # SIGTERM succeeds; process exits before the SIGKILL grace expires.
    monkeypatch.setattr(asyncio, "sleep", _make_immediate_exit_sleep(monkeypatch))

    asyncio.run(commands.cmd_service_stop())

    import signal

    assert kills == [(4242, signal.SIGTERM)]
    assert not pid_file.exists()


def test_service_stop_skips_sigkill_when_identity_unknown(monkeypatch, tmp_path):
    """If the process can't be verified AND refuses to die, no SIGKILL — we might be
    about to kill an unrelated process that happened to reuse the PID."""
    pid_file = _setup_pid_file(monkeypatch, tmp_path, 4242)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(commands, "_pid_is_our_daemon", lambda pid: None)
    monkeypatch.setattr(asyncio, "sleep", _make_no_exit_sleep())

    asyncio.run(commands.cmd_service_stop())

    import signal

    assert kills == [(4242, signal.SIGTERM)]  # gentle TERM only, no KILL
    assert not pid_file.exists()


def test_service_stop_sigkills_verified_daemon_after_timeout(monkeypatch, tmp_path):
    pid_file = _setup_pid_file(monkeypatch, tmp_path, 4242)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(commands, "_pid_is_our_daemon", lambda pid: True)
    monkeypatch.setattr(asyncio, "sleep", _make_no_exit_sleep())

    asyncio.run(commands.cmd_service_stop())

    import signal

    assert (4242, signal.SIGTERM) in kills
    assert (4242, signal.SIGKILL) in kills
    assert not pid_file.exists()


def test_service_stop_not_running_removes_stale_pid_file(monkeypatch, tmp_path):
    pid_file = _setup_pid_file(monkeypatch, tmp_path, 4242)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: False)

    asyncio.run(commands.cmd_service_stop())

    assert kills == []
    assert not pid_file.exists()


def test_service_stop_cleans_corrupt_pid_file(monkeypatch, tmp_path):
    pid_file = _setup_pid_file(monkeypatch, tmp_path, 42)  # "42" but non-numeric below
    pid_file.write_text("not-a-pid")
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    asyncio.run(commands.cmd_service_stop())

    assert kills == []
    assert not pid_file.exists()


# --- helpers ------------------------------------------------------------------


def _make_immediate_exit_sleep(monkeypatch):
    """asyncio.sleep stub whose first call 'kills' the process, i.e. makes
    subsequent _is_pid_alive checks False. The real asyncio.sleep runs so the
    loop machinery stays intact."""
    async def fake_sleep(delay, *args, **kwargs):
        monkeypatch.setattr(commands, "_is_pid_alive", lambda pid: False)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return fake_sleep


def _make_no_exit_sleep():
    """asyncio.sleep stub where the process never dies — exercises the timeout path."""

    async def fake_sleep(delay, *args, **kwargs):
        pass

    return fake_sleep
