"""Reset must never leave the simulator stuck in 'resetting' (it did once under load, when a rebuild raised an error nobody caught).
A failed rebuild is retried; if it keeps failing the controller ends 'idle' with an error and Reset can be pressed again."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "sim"), os.path.join(ROOT, "agent")]

import demo_server as ds  # noqa: E402


class _Agent:
    source = "SIMULATOR"


class _Backend:
    SOURCE = "SIMULATOR"


class _Cluster:
    backend = _Backend()
    agents = {n: _Agent() for n in "ABCD"}

    def stop(self):
        pass


def _controller(fail_times, monkeypatch):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] > 1 and calls["n"] <= 1 + fail_times:        # the first call is the start-up build
            raise OSError("address already in use")
        return _Cluster()
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    return ds.DemoController(factory, catalogue={}, check_env=False), calls


def test_a_rebuild_that_fails_twice_is_retried_and_reset_succeeds(monkeypatch):
    ctl, calls = _controller(2, monkeypatch)
    code, body = ctl.reset()
    assert (code, body) == (200, {"reset": True}) and ctl.status == "idle" and ctl.dirty is False and calls["n"] == 4


def test_a_rebuild_that_always_fails_leaves_the_simulator_usable_not_stuck(monkeypatch):
    ctl, _ = _controller(99, monkeypatch)
    code, body = ctl.reset()
    assert code == 500 and "could not restart" in body["error"]
    assert ctl.status == "idle" and ctl.dirty is True                   # not stuck in "resetting"; a scenario is refused until Reset works
    assert ctl.start("x")[0] in (400, 409)
    ctl.factory = lambda: _Cluster()                                    # the port is free again: pressing Reset once more recovers
    assert ctl.reset() == (200, {"reset": True}) and ctl.dirty is False
