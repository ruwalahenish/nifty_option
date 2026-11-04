"""Runtime paper-only guard and the kill switch.

PAPER_ONLY is a module constant. There is no setter, no environment override and no
config option that turns it off in Version 1.
"""
import os

from ..errors import KillSwitchActive, LiveTradingNotSupported

PAPER_ONLY = True
KILL_FILE_NAME = "KILL"


def assert_paper_only(cfg=None):
    """Raise unless we are in paper mode. Called at every execution boundary."""
    if PAPER_ONLY is not True:
        raise LiveTradingNotSupported("PAPER_ONLY was altered; refusing to continue")
    if cfg is not None and getattr(cfg, "paper", True) is not True:
        raise LiveTradingNotSupported("configuration is not in paper mode")


def kill_switch_path(trader_dir):
    return os.path.join(trader_dir, KILL_FILE_NAME)


def kill_switch_active(trader_dir):
    return os.path.exists(kill_switch_path(trader_dir))


def assert_no_kill(trader_dir):
    """Raise KillSwitchActive when the KILL file exists in trader_dir."""
    if kill_switch_active(trader_dir):
        raise KillSwitchActive("kill switch file present: %s" % kill_switch_path(trader_dir))


def trip_kill_switch(trader_dir, reason="manual"):
    """Create the KILL file. Only a human removes it; nothing in the package deletes it."""
    os.makedirs(trader_dir, exist_ok=True)
    with open(kill_switch_path(trader_dir), "w", encoding="utf-8") as fh:
        fh.write("%s\n" % reason)
