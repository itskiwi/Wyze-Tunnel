from queue import Empty
from unittest import mock

import pytest
from wyzebridge import wyze_control


def mux_with(*results):
    """A fake ioctl mux whose futures return (or raise) each result in turn."""
    futures = []
    for r in results:
        future = mock.Mock()
        future.result.side_effect = r if isinstance(r, type) else None
        future.result.return_value = None if isinstance(r, type) else r
        futures.append(future)
    mux = mock.Mock()
    mux.send_ioctl.side_effect = futures
    return mux


def test_retries_then_succeeds():
    mux = mux_with(Empty, Empty, [{"vertical": 1, "horizontal": 2}])
    assert wyze_control._ioctl_retry(mux, object) == [{"vertical": 1, "horizontal": 2}]
    assert mux.send_ioctl.call_count == 3


def test_gives_up_after_three_tries():
    with pytest.raises(TimeoutError):
        wyze_control._ioctl_retry(mux_with(Empty, Empty, Empty), object)


def test_pan_to_saved_position():
    mux = mux_with(Empty, [{"vertical": 10, "horizontal": 20}, {"vertical": 30, "horizontal": 40}], b"\x01")
    sess = mock.MagicMock()
    sess.iotctrl_mux.return_value.__enter__.return_value = mux
    with mock.patch.object(wyze_control.tutk_protocol, "K11018SetPTZPosition") as set_pos:
        resp = wyze_control.pan_to_cruise_point(sess, ("cruise_point", "2"))
    assert resp["status"] == "success"
    set_pos.assert_called_with(30, 40)
