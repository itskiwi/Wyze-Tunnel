from wyzebridge.wyze_api import wyze_media_url


def test_only_wyze_hosts_are_proxied():
    assert wyze_media_url("https://wyze-device-alarm-file.s3.us-west-2.amazonaws.com/a.mp4?X-Amz-Date=1")
    assert wyze_media_url("https://prod-sight-safe-auth.wyze.com/clip.mp4")
    assert not wyze_media_url("http://prod-sight-safe-auth.wyze.com/clip.mp4")
    assert not wyze_media_url("https://evil.com/?x=.wyze.com")
    assert not wyze_media_url("https://wyze.com.evil.com/a")
    assert not wyze_media_url("https://169.254.169.254/latest")
    assert not wyze_media_url("https://x.wyze.com:8443/a")
    assert not wyze_media_url("https://x.wyze.com:bad/a")
