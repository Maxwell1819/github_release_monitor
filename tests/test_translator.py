from src.translator import Translator


class FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if callable(r):
            r = r(params)
        return r


def test_translate_ok(monkeypatch):
    sess = FakeSession([FakeResp({"trans_result": [{"dst": "你好，世界"}]})])
    monkeypatch.setattr("src.translator.requests.get", lambda url, params, timeout: sess.get(url, params, timeout))
    t = Translator("appid", "secret")
    assert t.translate("hello world") == "你好，世界"


def test_translate_api_error_returns_none(monkeypatch):
    sess = FakeSession([FakeResp({"error_code": "52003", "error_msg": "UNAUTHORIZED USER"})])
    monkeypatch.setattr("src.translator.requests.get", lambda url, params, timeout: sess.get(url, params, timeout))
    t = Translator("appid", "secret")
    assert t.translate("x") is None


def test_translate_empty():
    t = Translator("appid", "secret")
    assert t.translate("") == ""


def test_translate_long_text_segmented(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["q"])
        return FakeResp({"trans_result": [{"dst": "译文:" + params["q"][:5]}]})

    monkeypatch.setattr("src.translator.requests.get", fake_get)
    t = Translator("appid", "secret")
    long_text = "a\n\n" * 3000  # >5000 字符，含段落边界
    out = t.translate(long_text)
    assert len(calls) >= 2
    assert out.startswith("译文:a")


def test_translate_segment_failure_returns_none(monkeypatch):
    """后续分段失败时整体返回 None，不返回截断的译文"""
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["q"])
        if len(calls) == 1:
            return FakeResp({"trans_result": [{"dst": "第一部分"}]})
        return FakeResp({"error_code": "54003", "error_msg": "slow down"})

    monkeypatch.setattr("src.translator.requests.get", fake_get)
    t = Translator("appid", "secret")
    long_text = "a\n\n" * 3000
    assert t.translate(long_text) is None
    assert len(calls) >= 2
