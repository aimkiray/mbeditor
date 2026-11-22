"""bsk CLI 的薄封装。

只做三件事：把命令拼对、把 JSON 解析出来、把错误翻译成人话。
不缓存状态——bsk 的 observation / context 是「一次性」的，缓存即过期。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Sequence

#: 公众号后台 URL 里的会话 token；任何日志/异常/返回值都必须先过一遍这个。
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")


def redact(text: str) -> str:
    """把 `token=xxx` 抹成 `token=***`，其余原样。"""
    return _TOKEN_RE.sub(r"\1***", text)


class BskError(RuntimeError):
    """bsk 调用失败。``detail`` 保留原文，便于定位是页面变了还是参数错了。"""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr

    @property
    def detail(self) -> str:
        return (self.stderr or self.stdout or "").strip()


@dataclass
class Observation:
    """一次 sync 的结果：observation_id 必须原样传给下一个写操作。"""

    observation_id: str
    ctx: str
    tab_id: int | None
    url: str
    vom: str

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Observation":
        return cls(
            observation_id=payload.get("observation_id", ""),
            ctx=payload.get("ctx", ""),
            tab_id=payload.get("tab_id"),
            url=payload.get("url", ""),
            vom=payload.get("vom", ""),
        )


class Bsk:
    """一个 scope 对应浏览器里的一个 Agent Window 会话。"""

    def __init__(
        self,
        scope: str = "mpaudio",
        tab_id: int | None = None,
        binary: str = "bsk",
        timeout: int = 90,
    ) -> None:
        self.scope = scope
        self.tab_id = tab_id
        self.binary = binary
        self.timeout = timeout

    # ---------------------------------------------------------------- 底层

    def _run(self, args: Sequence[str], *, timeout: int | None = None) -> str:
        cmd = [self.binary, *args]
        if self.tab_id:
            cmd += ["--tab-id", str(self.tab_id)]
        env = {**os.environ, "BSK_SCOPE": self.scope}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout or self.timeout,
            )
        except FileNotFoundError as exc:  # pragma: no cover - 环境问题
            raise BskError(f"找不到 bsk 可执行文件：{self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BskError(
                f"bsk {args[0]} 超时（{timeout or self.timeout}s）；"
                "页面可能仍在动，先 sync 观察再决定，不要重放"
            ) from exc
        if proc.returncode != 0:
            raise BskError(
                f"bsk {redact(' '.join(args))[:120]} 失败（exit {proc.returncode}）",
                stdout=redact(proc.stdout),
                stderr=redact(proc.stderr),
            )
        return proc.stdout

    # ---------------------------------------------------------------- 会话

    def open(self, url: str, *, retries: int = 3) -> Observation:
        # 上一个会话留下的 tab 还在重绘时，open 会撞 document_changed；
        # effect_state=none 说明什么都没派发，重开一次是安全的。
        last: BskError | None = None
        for attempt in range(retries):
            try:
                out = self._run(["open", url, "--json"])
                return Observation.from_json(json.loads(out))
            except BskError as exc:
                last = exc
                text = f"{exc} {exc.stdout}"
                if '"effect_state": "none"' not in text and "document_changed" not in text:
                    raise
                time.sleep(1.0 + attempt)
        raise last  # type: ignore[misc]

    def sync(self, *, retries: int = 3) -> Observation:
        # 页面在自己重绘时，sync 可能撞上 "Document changed during observation"；
        # 这只是观察瞬态，重新观察一次即可（不涉及任何写操作）。
        last: BskError | None = None
        for attempt in range(retries):
            try:
                out = self._run(["sync", "--json"])
                payload = json.loads(out)
                obs = Observation.from_json(payload)
                if obs.tab_id and not self.tab_id:
                    self.tab_id = obs.tab_id
                return obs
            except (BskError, json.JSONDecodeError) as exc:
                last = exc if isinstance(exc, BskError) else BskError(f"sync 输出不是 JSON：{exc}")
                text = f"{last} {getattr(last, 'stdout', '')}"
                if "Document changed during observation" not in text and "ref_not_found" not in text:
                    raise last
                time.sleep(0.8 + attempt)
        raise last  # type: ignore[misc]

    def close(self) -> None:
        try:
            self._run(["close"])
        except BskError:
            pass

    # ---------------------------------------------------------------- 动作

    def evaluate(self, expression: str, obs: Observation | None = None) -> str:
        """执行 JS 表达式并返回其字符串结果。

        ``obs`` 不传就现场 sync 一次——多一次调用，但省掉一类「上下文过期」故障。
        """
        obs = obs or self.sync()
        out = self._run(["evaluate", expression, "--observation", obs.observation_id])
        return out.strip()

    def evaluate_json(self, expression: str, obs: Observation | None = None) -> Any:
        raw = self.evaluate(expression, obs)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def click(self, selector: str, obs: Observation | None = None) -> None:
        obs = obs or self.sync()
        self._run(["click", "--selector", selector, "--observation", obs.observation_id])

    def press(self, key: str, obs: Observation | None = None) -> None:
        obs = obs or self.sync()
        self._run(["press", key, "--observation", obs.observation_id])

    def network(self, limit: int = 80) -> str:
        return self._run(["network", "--limit", str(limit)])

