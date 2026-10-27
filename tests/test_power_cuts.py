import json
from time import time

from wyzebridge import web_ui


def test_power_cuts_from_the_last_week(tmp_path, monkeypatch):
    monkeypatch.setenv("WT_UPDATE_FILE", str(tmp_path / "update-now"))
    assert web_ui.power_cuts() == []
    now = int(time())
    cuts = [{"from": now - 600, "to": now - 60}, {"from": 1, "to": 2}, {"from": "x", "to": now}, "junk"]
    (tmp_path / "power-cuts.json").write_text(json.dumps(cuts))
    assert web_ui.power_cuts() == [{"from": now - 600, "to": now - 60}]
    (tmp_path / "power-cuts.json").write_text("not json")
    assert web_ui.power_cuts() == []


def test_no_tray_no_cuts(monkeypatch):
    monkeypatch.delenv("WT_UPDATE_FILE", raising=False)
    assert web_ui.power_cuts() == []
