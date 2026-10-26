# Sorts after test_web_devices.py; reuses its fakes.
import pytest
import test_web_devices as twd
from test_web_devices import AUTH, client, wyze  # noqa: F401
from wyzebridge import devices

GROUP = {"group_id": 7, "group_name": "Front of House", "group_type_id": 2,
         "device_list": [{"mac": "PLUG-0001"}, {"device_mac": "DOOR"}]}  # device_mac exercises the fallback


@pytest.fixture
def with_group(wyze, monkeypatch):
    monkeypatch.setattr(twd, "OBJECTS", {**twd.OBJECTS, "device_group_list": [GROUP]})
    return wyze


def test_members_are_tagged_and_others_are_not(with_group):
    found = {d["mac"]: d for d in devices.list_devices(AUTH)}
    assert found["PLUG-0001"]["group"] == "Front of House"
    assert found["DOOR"]["group"] == "Front of House"
    assert "group" not in found["CO_EA1_1"]  # not in any group


def test_groups_accessor_returns_id_name_type_and_macs(with_group):
    assert devices.groups(AUTH) == [{"id": 7, "name": "Front of House", "type": 2, "macs": ["PLUG-0001", "DOOR"]}]


def test_no_group_list_is_no_tags_and_no_groups(wyze):
    found = {d["mac"]: d for d in devices.list_devices(AUTH)}
    assert all("group" not in d for d in found.values())
    assert devices.groups(AUTH) == []


def test_groups_endpoint(client, wyze, monkeypatch):
    monkeypatch.setattr(devices, "groups", lambda auth, fresh=False: [{"id": 7, "name": "Front of House", "type": 2, "macs": ["PLUG-0001"]}])
    assert client.get("/api/devices/groups").json == {"groups": [{"id": 7, "name": "Front of House", "type": 2, "macs": ["PLUG-0001"]}]}
