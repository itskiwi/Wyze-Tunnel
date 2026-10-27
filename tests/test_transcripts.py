import json
from unittest import mock

import numpy as np
import pytest

from wyzebridge import transcripts, visitors


@pytest.fixture
def files(monkeypatch, tmp_path):
    monkeypatch.setattr(transcripts, "SETTINGS_FILE", str(tmp_path / "transcripts.json"))
    monkeypatch.setattr(visitors, "VISITORS_FILE", str(tmp_path / "visitors.json"))
    monkeypatch.setattr(transcripts, "available", lambda: True)
    return tmp_path


def test_off_until_the_owner_confirms_the_law(files):
    assert not transcripts.enabled()
    with pytest.raises(ValueError, match="allowed where you live"):
        transcripts.set_enabled(True)
    assert not transcripts.enabled()
    assert transcripts.set_enabled(True, agreed=True) and transcripts.enabled()
    assert json.loads((files / "transcripts.json").read_text())["agreed_at"] > 0
    transcripts.set_enabled(False)
    assert not transcripts.enabled()


def test_silence_is_not_sent_to_whisper(files):
    transcripts.set_enabled(True, agreed=True)
    said = mock.Mock()
    quiet = (np.zeros(transcripts.RATE * 2, "<i2")).tobytes()
    with mock.patch.object(transcripts, "record", return_value=quiet), mock.patch.object(transcripts, "transcribe") as whisper:
        transcripts.after_ring("door", said)
    assert not whisper.called and not said.called


def test_speech_text_reaches_the_visitor_entry(files):
    transcripts.set_enabled(True, agreed=True)
    visitors.add("door", "Front Door", 100)
    loud = (np.sin(np.arange(transcripts.RATE * 2) / 3) * 8000).astype("<i2").tobytes()
    out = mock.Mock(returncode=0, stdout=b"loading model\nTRANSCRIPT:Hi, package for you.\n")
    with mock.patch.object(transcripts, "record", return_value=loud), mock.patch.object(transcripts.subprocess, "run", return_value=out) as run:
        transcripts.after_ring("door", lambda text: visitors.set_transcript("door", 100, text))
    assert run.call_args.kwargs["input"] == loud  # the sound goes over a pipe, never to disk
    assert visitors.load()[0]["transcript"] == "Hi, package for you."


def test_a_crashed_child_gives_no_text(files):
    with mock.patch.object(transcripts.subprocess, "run", return_value=mock.Mock(returncode=-4, stdout=b"")):
        assert transcripts.transcribe(b"\0\0") == ""
