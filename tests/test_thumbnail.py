from unittest import mock

from wyzebridge import wyze_api
from wyzecam.api_models import WyzeCredential


def resp(status):
    r = mock.Mock(status_code=status, content=b"jpg", headers={})
    r.raise_for_status.side_effect = None if status == 200 else wyze_api.HTTPError(f"{status} Client Error")
    return r


def test_thumbnail_retries_401_with_wyze_token(tmp_path, monkeypatch):
    monkeypatch.setattr(wyze_api, "IMG_PATH", f"{tmp_path}/")
    api = wyze_api.WyzeApi()
    api.auth = WyzeCredential(access_token="tok", phone_id="p")
    url = "https://prod-sight-safe-auth.wyze.com/resource/MAC/2026-10-24/x/y.jpg?st=a&kid=b&nonce=c"
    with mock.patch.object(wyze_api, "get", side_effect=[resp(401), resp(200)]) as get:
        assert api.save_thumbnail("cam", url)
    assert get.call_args.kwargs["headers"] == {"Authorization": "tok"}
    assert (tmp_path / "cam.jpg").read_bytes() == b"jpg"

    with mock.patch.object(wyze_api, "get", side_effect=[resp(401), resp(401)]):
        assert not api.save_thumbnail("cam2", url)


def test_url_timestamp_reads_both_url_formats():
    new = "https://prod-sight-safe-auth.wyze.com/resource/80482C293A7F/2026-10-24/80482C293A7F131792835831_1792835832510_13_1_0/b7_-1.jpg?st=a"
    assert wyze_api.url_timestamp(new) == 1792835832
    old = "https://s3.amazonaws.com/bucket/x/MAC_1700000000000_1/y.jpg"
    assert wyze_api.url_timestamp(old) == 1700000000
    assert wyze_api.url_timestamp("https://example.com/a.jpg") == 0
