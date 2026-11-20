import ctypes
import sys

import pytest

from latentroom.engine import awake

ON, OFF = awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED, awake.ES_CONTINUOUS


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_keep_awake_asks_windows_and_always_lets_go(monkeypatch):
    asked = []

    def fake(flags):  # stands in for kernel32.SetThreadExecutionState
        asked.append(flags.value)
        return 0x80000000  # the previous state: any non-zero is success

    monkeypatch.setattr(ctypes.windll.kernel32, "SetThreadExecutionState", fake)
    with awake.keep_awake():
        assert asked == [ON]  # display may still turn off: no ES_DISPLAY_REQUIRED
    with pytest.raises(RuntimeError), awake.keep_awake():
        raise RuntimeError("the job broke")
    assert asked == [ON, OFF, ON, OFF]  # let go after the error too
