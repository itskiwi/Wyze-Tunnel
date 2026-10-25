from wyzebridge import mtx_server


def test_path_bytes(monkeypatch):
    class Resp:
        def json(self):
            return {"items": [{"name": "front", "bytesReceived": 10, "bytesSent": 3}, {"name": "back"}]}

    monkeypatch.setattr(mtx_server.requests, "get", lambda *a, **k: Resp())
    assert mtx_server.path_bytes() == {"front": {"in": 10, "out": 3}, "back": {"in": 0, "out": 0}}


def test_path_bytes_when_mediamtx_is_down(monkeypatch):
    def down(*a, **k):
        raise mtx_server.requests.ConnectionError()

    monkeypatch.setattr(mtx_server.requests, "get", down)
    assert mtx_server.path_bytes() == {}
