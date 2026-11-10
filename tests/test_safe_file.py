import json

import pytest
from wyzebridge import safe_file


def test_failed_save_leaves_the_old_file_whole(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text('{"topic": "kept"}')
    with pytest.raises(TypeError), safe_file.writing(path) as f:
        f.write('{"topic": ')
        json.dump(object(), f)  # the save dies halfway
    assert json.loads(path.read_text()) == {"topic": "kept"}
    assert not list(tmp_path.glob("*.tmp"))


def test_save_keeps_the_last_good_copy_and_startup_restores_it(tmp_path):
    path = tmp_path / "alerts.json"
    for topic in ("one", "two"):
        with safe_file.writing(path) as f:
            json.dump({"topic": topic}, f)
    assert json.loads(path.read_text()) == {"topic": "two"}

    path.write_text("")  # what a power cut leaves behind
    (tmp_path / "sealed.json").write_bytes(b"WTV1\x00not json")  # the vault's files are left alone
    (tmp_path / "lost.json").write_text("{")
    assert safe_file.restore_broken(tmp_path) == ["alerts.json"]
    assert json.loads(path.read_text()) == {"topic": "one"}
    assert (tmp_path / "sealed.json").read_bytes().startswith(b"WTV1")
