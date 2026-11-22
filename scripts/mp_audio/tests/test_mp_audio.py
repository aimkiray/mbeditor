"""离线单测：不碰浏览器，只测纯逻辑与本地服务。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from scripts.mp_audio.audio_server import serve_audio  # noqa: E402
from scripts.mp_audio.insert_audio import (  # noqa: E402
    JS_CLICK_SEL,
    JS_FILL_META,
    JS_INJECT_FILE,
    JS_SELECT_AND_INSERT,
    AudioInserter,
    Options,
)


class AudioServerTest(unittest.TestCase):
    def test_serves_exact_bytes_with_cors(self) -> None:
        payload = b"ID3fake-mp3-payload" * 100
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clip.mp3")
            with open(path, "wb") as fh:
                fh.write(payload)
            with serve_audio(path) as url:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.read(), payload)
                    self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_rejects_other_paths_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clip.mp3")
            with open(path, "wb") as fh:
                fh.write(b"x")
            with serve_audio(path) as url:
                base = url.rsplit("/", 1)[0]
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + "/other.mp3", timeout=5)

        with self.assertRaises(FileNotFoundError):
            with serve_audio(os.path.join(tmp, "nope.mp3")):
                pass


class TemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.inserter = AudioInserter(Options(file="a.mp3", title='a "quoted" title'))

    def test_click_template(self) -> None:
        js = self.inserter._js(JS_CLICK_SEL, SEL=".js_insertaudio")
        self.assertIn(".js_insertaudio", js)
        self.assertNotIn("__", js)

    def test_inject_template(self) -> None:
        js = self.inserter._js(JS_INJECT_FILE, URL="http://127.0.0.1:1/a.mp3", NAME="a.mp3")
        self.assertIn("http://127.0.0.1:1/a.mp3", js)
        self.assertIn('"a.mp3"', js)
        self.assertNotIn("__URL__", js)

    def test_fill_meta_escapes_quotes(self) -> None:
        js = self.inserter._js(
            JS_FILL_META,
            TITLE_LIT=json.dumps(self.inserter.opts.title, ensure_ascii=False),
            COVER="avatar",
        )
        # 注意：类名里有 BEM 的 `form__checkbox`，所以只断言占位符消失，不能断言没有 "__"
        self.assertNotIn("__TITLE_LIT__", js)
        self.assertNotIn("__COVER__", js)
        # 引号必须被转义成 JSON 字面量，否则会把 JS 字符串截断
        self.assertIn('\\"quoted\\"', js)

    def test_js_literal_survives_newline_and_backslash(self) -> None:
        nasty = 'line1\nline2 \\ "end"'
        js = self.inserter._js(
            JS_SELECT_AND_INSERT, TITLE_LIT=json.dumps(nasty, ensure_ascii=False)
        )
        self.assertNotIn("__TITLE_LIT__", js)
        self.assertIn("\\n", js)  # 换行被转义
        self.assertIn('\\\\', js)  # 反斜杠被转义



class RedactionTest(unittest.TestCase):
    def test_token_is_masked(self) -> None:
        from scripts.mp_audio.bsk import redact
        raw = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=x&token=SECRETVALUE123"
        out = redact(raw)
        self.assertNotIn("SECRETVALUE123", out)
        self.assertIn("token=***", out)

    def test_plain_text_untouched(self) -> None:
        from scripts.mp_audio.bsk import redact
        self.assertEqual(redact("hello tokenless"), "hello tokenless")

class _FakeBsk:
    """只记录调用次数，按脚本抛预置异常。"""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def evaluate_json(self, js, obs=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


class RetryPolicyTest(unittest.TestCase):
    def _inserter(self, bsk):
        ins = AudioInserter(Options(file="x.mp3", title="t"))
        ins.bsk = bsk
        return ins

    def test_predispatch_error_is_retried(self):
        from scripts.mp_audio.bsk import BskError
        err = BskError("boom", stdout="", stderr="the strict observation changed before this action")
        fake = _FakeBsk([err, err])
        ins = self._inserter(fake)
        self.assertEqual(ins._eval("1"), "ok")
        self.assertEqual(fake.calls, 3)

    def test_uncertain_outcome_is_not_replayed(self):
        from scripts.mp_audio.bsk import BskError
        err = BskError("boom", stdout="", stderr="operation timed out; never replay an uncertain action")
        fake = _FakeBsk([err])
        ins = self._inserter(fake)
        with self.assertRaises(BskError):
            ins._eval("1")
        self.assertEqual(fake.calls, 1)


if __name__ == "__main__":
    unittest.main()
