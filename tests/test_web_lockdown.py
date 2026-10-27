# Sorts after test_security.py, which reloads wyzebridge.auth (see test_web_recordings.py).
from unittest import mock

import pytest
import yaml
from test_web_guests import basic, setup  # noqa: F401


@pytest.fixture
def strict(monkeypatch, tmp_path):
    from wyzebridge import lockdown

    monkeypatch.setattr(lockdown, "FLAG", tmp_path / "tailscale-only")
    return lockdown


def test_who_gets_in_when_tailscale_only(strict):
    for addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "100.101.102.103", "fd7a:115c:a1e0::1"):
        assert strict.allowed(addr), addr
    for addr in ("192.168.0.5", "10.0.0.21", "8.8.8.8", "100.128.0.1", "", None, "nope"):
        assert not strict.allowed(addr), addr


def test_web_page_turns_away_home_network_when_tailscale_only(setup, strict):  # noqa: F811
    client, _, _ = setup
    owner = basic("owner", "ownerpass")
    lan, tailnet = {"REMOTE_ADDR": "192.168.0.5"}, {"REMOTE_ADDR": "100.64.1.2"}
    assert client.get("/", headers=owner, environ_base=lan).status_code == 200
    strict.set_tailscale_only(True)
    assert client.get("/login", environ_base=lan).status_code == 403
    assert client.get("/", headers=owner, environ_base=tailnet).status_code == 200
    assert client.get("/", headers=owner).status_code == 200  # this PC, and Wyze Tunnel's own Tailscale
    strict.set_tailscale_only(False)
    assert client.get("/", headers=owner, environ_base=lan).status_code == 200


def test_stream_server_listens_where_it_should(strict, tmp_path, monkeypatch):
    from wyzebridge import mtx_server

    cfg = tmp_path / "mediamtx.yml"
    cfg.write_text("paths: {}\nsrt: yes\n")
    monkeypatch.setattr(mtx_server, "MTX_CONFIG", str(cfg))
    mtx_server.MtxServer()
    home = yaml.safe_load(cfg.read_text())
    assert home["srt"] is False and home["rtspAddress"] == ":8554" and home["webrtcLocalUDPAddress"] == ":8189"
    strict.set_tailscale_only(True)
    mtx_server.MtxServer().set_reach()
    locked = yaml.safe_load(cfg.read_text())
    assert all(locked[k].startswith("127.0.0.1:") for k in mtx_server.LISTENERS)


def test_apple_home_stays_closed_when_tailscale_only(strict):
    from wyzebridge import go2rtc

    accessories = {"cam": {"name": "Cam"}}
    with mock.patch.object(go2rtc.homekit, "go2rtc_config", return_value=({}, accessories)):
        assert go2rtc.Go2rtcServer().config()["api"]["listen"].startswith(":")
        strict.set_tailscale_only(True)
        cfg = go2rtc.Go2rtcServer().config()
    assert cfg["api"]["listen"].startswith("127.0.0.1:") and "homekit" not in cfg


def test_open_stream_logins_are_caught():
    from wyzebridge import lockdown

    this_pc = {"ips": ["127.0.0.1"], "permissions": [{"action": "read"}, {"action": "publish"}]}
    assert not lockdown.open_readers([this_pc, {"user": "wb", "pass": "key", "permissions": [{"action": "read"}]}])
    assert lockdown.open_readers([this_pc, {"permissions": [{"action": "read"}]}])
    assert lockdown.open_readers([{"user": "any", "pass": "", "ips": ["192.168.0.0/16"], "permissions": [{"action": "read"}]}])


def test_lockdown_card_and_switch(setup, strict):  # noqa: F811
    client, _, _ = setup
    owner = basic("owner", "ownerpass")
    with mock.patch.object(strict, "open_ports", return_value=[5000, 8554]), mock.patch.object(strict, "lan_ip", return_value="192.168.0.9"):
        page = client.get("/health", headers=owner).get_data(as_text=True)
    assert "Lockdown" in page and "Every video link needs a login" in page and "No public links or router ports" in page
    assert "Needs a look" not in page and "Tailscale only" in page

    with mock.patch("frontend.Timer") as timer:
        assert client.post("/health/tailscale-only", json={"on": True}, headers=owner).json == {"on": True}
    assert strict.tailscale_only() and timer.return_value.start.called
    # Still answering the home network until the restart: the card says so.
    with mock.patch.object(strict, "open_ports", return_value=[5000]), mock.patch.object(strict, "lan_ip", return_value="192.168.0.9"):
        page = client.get("/health", headers=owner).get_data(as_text=True)
    assert "Needs a look" in page and "Still answering your home network (web page)" in page

    with mock.patch("frontend.Timer"):
        client.post("/health/tailscale-only", json={"on": False}, headers=owner)
    assert not strict.tailscale_only()


def test_secrets_are_sealed_and_old_ones_still_read(tmp_path, monkeypatch):
    from wyzebridge import vault

    # Stand-in for DPAPI, which only exists on Windows (CI checks the real one there).
    monkeypatch.setattr(vault, "_protect", lambda b: b[::-1])
    monkeypatch.setattr(vault, "_unprotect", lambda b: b[::-1])
    path = tmp_path / "wb_api"
    path.write_text("old plain key")
    assert vault.read_text(path) == "old plain key" and not vault.sealed(path)
    vault.seal_existing([path, tmp_path / "missing"])
    assert vault.sealed(path) and b"old plain key" not in path.read_bytes() and vault.read_text(path) == "old plain key"

    def broken(_):
        raise OSError("no DPAPI")

    monkeypatch.setattr(vault, "_protect", broken)
    vault.write_text(path, "still saved")  # never lose a sign-in over it
    assert path.read_text() == "still saved"


def test_wyze_sign_in_cache_round_trips_sealed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from wyzebridge import vault, wyze_api

    monkeypatch.setattr(vault, "_protect", lambda b: b[::-1])
    monkeypatch.setattr(vault, "_unprotect", lambda b: b[::-1])
    monkeypatch.setattr(wyze_api, "TOKEN_PATH", f"{tmp_path}/")
    wyze_api.pickle_dump("cameras", ["secret-camera-key"])
    assert vault.sealed(tmp_path / "cameras.pickle") and b"secret-camera-key" not in (tmp_path / "cameras.pickle").read_bytes()

    class Api:
        auth, creds, cameras = True, SimpleNamespace(is_set=True), None

        @wyze_api.cached
        def get_cameras(self, fresh_data=False):
            raise AssertionError("should come from the cache")

    assert Api().get_cameras() == ["secret-camera-key"]


def test_firewall_open_to_any_address_is_caught(strict, monkeypatch):
    from types import SimpleNamespace

    for out, expected in (("open\r\n", True), ("", False)):
        monkeypatch.setattr(strict, "_fw", (0.0, False))
        with mock.patch.object(strict.subprocess, "run", return_value=SimpleNamespace(stdout=out)) as run:
            assert strict.firewall_too_open() is expected
            assert strict.firewall_too_open() is expected  # cached: one PowerShell call a minute
        assert run.call_count == 1 and "-Program" in run.call_args.args[0][-1]


def test_fix_it_asks_the_tray(setup, tmp_path, monkeypatch):  # noqa: F811
    client, _, _ = setup
    owner = basic("owner", "ownerpass")
    monkeypatch.delenv("WT_UPDATE_FILE", raising=False)
    assert client.post("/health/fix-firewall", headers=owner).json == {"asked": False}
    monkeypatch.setenv("WT_UPDATE_FILE", str(tmp_path / "update-now"))
    assert client.post("/health/fix-firewall", headers=owner).json == {"asked": True}
    assert (tmp_path / "fix-firewall").exists()
