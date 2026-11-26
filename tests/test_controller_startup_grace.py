"""Startup must not report a just-(re)loaded matter-server as crashing.

Field report 2026-11-26: enabling the controller wrote and bootstrapped its
LaunchAgent; the client's first two misses (Node still starting) produced
"matter-server is not responding ... appears to be crashing" with stale err-log
lines, ten seconds before it connected. ``ensure_installed()`` returning True
means launchd has just (re)loaded the job, so startup opens the same expected-
restart window a menu restart does.
"""
from __future__ import annotations

import importlib
import time
from unittest.mock import Mock

import pytest


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


def _plug(plugin_mod):
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p._restart_expected_until = 0.0
    p._restart_notice_shown = False
    p.server_process = None
    return p


def _source(plugin_mod) -> str:
    import inspect
    return inspect.getsource(plugin_mod.Plugin.startup)


def test_startup_opens_the_window_only_when_launchd_just_loaded_the_server(plugin_mod):
    """Pinned at the source level: startup() is too large to drive in a unit test,
    so pin that the window is opened on, and only on, ensure_installed() == True."""
    src = _source(plugin_mod)
    assert "if self.server_process.ensure_installed() is True:" in src
    after = src.split("if self.server_process.ensure_installed() is True:", 1)[1]
    assert "self._expect_restart()" in after.split("except", 1)[0]


def test_inside_the_window_an_outage_is_expected_not_a_crash(plugin_mod):
    p = _plug(plugin_mod)
    p._expect_restart()
    assert p._restart_expected()
    p.server_process = Mock()
    p.matter = Mock()
    p._on_server_unreachable(2)
    p.server_process.tail_error_log.assert_not_called()
    p.logger.error.assert_not_called()
    p.matter.rearm_failure_diagnostic.assert_called_once()


def test_after_the_window_a_real_failure_is_still_reported(plugin_mod, monkeypatch):
    p = _plug(plugin_mod)
    p._expect_restart()
    real = time.monotonic
    monkeypatch.setattr(plugin_mod.time, "monotonic", lambda: real() + 31)
    p.server_process = Mock()
    p.server_process.tail_error_log.return_value = "FATAL something"
    p.server_process.describe_fatal_cause.return_value = None
    p.matter = Mock()
    p._on_server_unreachable(2)
    assert "appears to be" in str(p.logger.error.call_args)
