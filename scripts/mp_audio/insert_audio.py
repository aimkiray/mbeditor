"""把一段本地音频插进公众号图文草稿。

流程（每一步都是真机验证过的选择器，见 docs/mp-audio-insert.md）：

1. 打开图文编辑器（已有草稿用 URL，新建用后台首页点「文章」）
2. 工具栏 音频 → 音频 → 打开「插入音频」弹窗
3. 点「上传音频」→ 填标题 / 封面（默认账号头像）/ 勾协议 → 保存
4. 轮询素材列表，等「转码中」消失
5. 选中该音频 → 插入
6. 校验正文里出现音频节点，然后存草稿

用法::

    python -m scripts.mp_audio.insert_audio --file a.mp3 --title "第 12 期"
    python -m scripts.mp_audio.insert_audio --file a.mp3 --title t --dry-run
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .audio_server import serve_audio
from .bsk import Bsk, BskError, Observation, redact

import mimetypes
import re
import os
from urllib.parse import quote

# --------------------------------------------------------------------- JS 片段
# 统一用 __X__ 占位，避免 f-string 与 JS 花括号打架。

JS_FILL_ARTICLE = """(() => {
  const pms = document.querySelectorAll(".ProseMirror");
  if (pms.length < 2) return "no-editor";
  const paste = (el, html, text) => {
    el.focus();
    const rg = document.createRange();
    rg.selectNodeContents(el);
    const sel = getSelection();
    sel.removeAllRanges();
    sel.addRange(rg);
    const dt = new DataTransfer();
    dt.setData("text/html", html);
    dt.setData("text/plain", text);
    return !el.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true })
    );
  };
  const title = __TITLE_LIT__;
  const paras = __BODY_LIT__;
  const okTitle = paste(pms[0], "<p>" + title + "</p>", title);
  const esc = (t) =>
    String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const html = paras.map((p) => "<p>" + esc(p) + "</p>").join("");
  const okBody = paste(pms[1], html, paras.join("\\n"));
  return JSON.stringify({
    okTitle,
    okBody,
    title: (pms[0].innerText || "").trim().slice(0, 40),
    bodyLen: (pms[1].innerText || "").length,
  });
})()"""

JS_GOTO_EDITOR = """(() => {
  const token = new URLSearchParams(location.search).get("token") || "";
  if (!token) return "no-token";
  location.href =
    "/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&createType=0&token=" +
    token + "&lang=zh_CN&timestamp=" + Date.now();
  return "ok";
})()"""

JS_CLICK_SEL = """(() => {
  const el = document.querySelector("__SEL__");
  if (!el) return "missing";
  el.click();
  return "ok";
})()"""

JS_OPEN_UPLOAD = """(() => {
  const d = document.querySelector(".js_audio_music_dialog");
  if (!d) return "no-dialog";
  const btn = [...d.querySelectorAll("button")].find(
    (b) => (b.innerText || "").trim() === "上传音频"
  );
  if (!btn) return "no-btn";
  btn.click();
  return "ok";
})()"""

JS_INJECT_FILE = """(async () => {
  const inp = document.querySelector(
    ".weui-desktop-form__controls .weui-desktop-upload-input__wrp input[type=file]"
  );
  if (!inp) return "no-input";
  const resp = await fetch("__URL__", { cache: "no-store" });
  if (!resp.ok) return "fetch-" + resp.status;
  const blob = await resp.blob();
  const file = new File([blob], "__NAME__", { type: "__MIME__" });
  const dt = new DataTransfer();
  dt.items.add(file);
  inp.files = dt.files;
  inp.dispatchEvent(new Event("input", { bubbles: true }));
  inp.dispatchEvent(new Event("change", { bubbles: true }));
  return JSON.stringify({ ok: true, size: blob.size, files: inp.files.length });
})()"""

JS_FILL_META = """(() => {
  const dlg = [...document.querySelectorAll(".weui-desktop-dialog")].find(
    (d) => (d.innerText || "").includes("重新上传")
  );
  if (!dlg) return "no-upload-dialog";
  const inp = dlg.querySelector("input[placeholder='填写标题'], input[type=text]");
  if (inp) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    ).set;
    setter.call(inp, __TITLE_LIT__);
    inp.dispatchEvent(new Event("input", { bubbles: true }));
    inp.dispatchEvent(new Event("change", { bubbles: true }));
  }
  let cover = "skipped";
  if (__COVER__ === "avatar" && !__SKIP_AVATAR__) {
    const a = [...dlg.querySelectorAll("a")].find(
      (x) => (x.innerText || "").trim() === "使用账号头像"
    );
    if (a) { a.click(); cover = "avatar"; }
  }
  const cb = dlg.querySelector("input.weui-desktop-form__checkbox");
  if (cb && !cb.checked) cb.click();
  return JSON.stringify({ title: inp && inp.value, cover, agreed: cb ? cb.checked : null });
})()"""

JS_SUBMIT_UPLOAD = """(() => {
  const dlg = [...document.querySelectorAll(".weui-desktop-dialog")].find(
    (d) => (d.innerText || "").includes("重新上传")
  );
  if (!dlg) return "no-upload-dialog";
  const btn = [...dlg.querySelectorAll("button")].find(
    (b) => (b.innerText || "").trim() === "保存"
  );
  if (!btn) return "no-save";
  btn.click();
  return "ok";
})()"""

JS_OPEN_COVER_PICKER = """(() => {
  const dlg = [...document.querySelectorAll(".weui-desktop-dialog")].find(
    (d) => (d.innerText || "").includes("重新上传")
  );
  if (!dlg) return "no-upload-dialog";
  const btn = [...dlg.querySelectorAll("button,a")].find(
    (b) => (b.innerText || "").trim() === "上传封面"
  );
  if (!btn) return "no-cover-btn";
  btn.click();
  return "ok";
})()"""

JS_INJECT_COVER = """(async () => {
  const inp = [...document.querySelectorAll("input[type=file]")].find(
    (i) => (i.accept || "").includes("image") && i.getBoundingClientRect().width > 0
  ) || [...document.querySelectorAll("input[type=file]")].find((i) => (i.accept || "").includes("image"));
  if (!inp) return "no-image-input";
  const resp = await fetch("__URL__", { cache: "no-store" });
  if (!resp.ok) return "fetch-" + resp.status;
  const blob = await resp.blob();
  const file = new File([blob], "__NAME__", { type: "image/png" });
  const dt = new DataTransfer();
  dt.items.add(file);
  inp.files = dt.files;
  inp.dispatchEvent(new Event("input", { bubbles: true }));
  inp.dispatchEvent(new Event("change", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 1200));
  return JSON.stringify({ ok: true, size: blob.size });
})()"""

JS_TOP_DIALOG_CLICK = """(async () => {
  const btns = [...document.querySelectorAll(".weui-desktop-dialog button")].filter(
    (b) => (b.innerText || "").trim() === "__LABEL__"
  );
  if (!btns.length) return "no-btn";
  btns[btns.length - 1].click();
  await new Promise((r) => setTimeout(r, 1200));
  return "ok";
})()"""

JS_CLOSE_DIALOG = """(() => {
  const d = document.querySelector(".js_audio_music_dialog");
  if (!d) return "no-dialog";
  const btns = [...d.querySelectorAll("button")].filter((b) => (b.innerText || "").trim() === "取消");
  const btn = btns[btns.length - 1];
  if (!btn) return "no-cancel";
  btn.click();
  return "ok";
})()"""

JS_DIALOG_TEXT = """(() => {
  const d = document.querySelector(".js_audio_music_dialog");
  return d ? (d.innerText || "").replace(/\\s+/g, " ").trim() : "";
})()"""

JS_SELECT_AND_INSERT = """(() => {
  const d = document.querySelector(".js_audio_music_dialog");
  if (!d) return "no-dialog";
  // 精确匹配优先，退化为子串；只认列表项，不认容器
  const items = [...d.querySelectorAll(".audio_item, li.weui-desktop-media-item, li")];
  const title = __TITLE_LIT__;
  const norm = (el) => (el.innerText || "").replace(/\\s+/g, " ").trim();
  // 列表项文本形如「烟测音频 01 10秒 2026/11/22」，只能用前缀匹配
  const node =
    items.find((el) => norm(el).startsWith(title)) ||
    items.find((el) => norm(el).includes(title));
  if (!node) return "item-not-found";
  const cb = node.querySelector("input[type=checkbox]");
  if (!cb) return "no-checkbox";
  if (cb.checked) return "already-selected";
  // 关键：不要用 .click()。Chromium 对程序化 checkbox.click() 会派发 trusted 的
  // input/change，BrowserSkill(<=0.5.0) 把它记成"人在输入"，当场撤销控制上下文，
  // 报 the user has control of this tab（上游 e5ab272b 已修；扩展没升级前只能绕）。
  // 改成设置 checked + 派发 untrusted 事件：扩展只认 trusted，不会误判。
  cb.checked = true;
  cb.dispatchEvent(new Event("input", { bubbles: true }));
  cb.dispatchEvent(new Event("change", { bubbles: true }));
  return "selected-untrusted";
})()"""

JS_CLICK_INSERT = """(() => {
  const d = document.querySelector(".js_audio_music_dialog");
  if (!d) return "no-dialog";
  const btn = [...d.querySelectorAll("button")].find(
    (b) => (b.innerText || "").trim() === "插入"
  );
  if (!btn) return "no-insert-btn";
  if (btn.disabled || (btn.className || "").includes("disabled")) return "insert-disabled";
  btn.click();
  return "ok";
})()"""

JS_FIND_AUDIO_NODE = """(() => {
  const pm = document.querySelectorAll(".ProseMirror");
  const root = pm.length ? pm[pm.length - 1] : document;
  const hits = [
    ...root.querySelectorAll(
      "section[data-pluginname=insertaudio], mpvoice, mp-common-mpaudio, [class*=audio_card]"
    ),
  ].map((el) => ({
    tag: el.tagName.toLowerCase(),
    cls: (el.className || "").toString().slice(0, 80),
    fileid: el.getAttribute("voice_encode_fileid") || "",
    name: el.getAttribute("name") || "",
    playLength: el.getAttribute("play_length") || "",
    transState: el.getAttribute("data-trans_state") || "",
    verifyState: el.getAttribute("data-verify_state") || "",
    html: el.outerHTML.slice(0, 400),
  }));
  return JSON.stringify({ count: hits.length, hits: hits.slice(0, 5) });
})()"""

JS_SAVE_DRAFT = """(() => {
  const btn = document.querySelector("#js_submit button");
  if (!btn) return "no-js-submit";
  if ((btn.innerText || "").trim() !== "保存为草稿") return "unexpected:" + (btn.innerText || "").trim();
  btn.click();
  return "ok";
})()"""


@dataclass
class Options:
    file: str
    title: str
    cover: str = "avatar"
    cover_file: str | None = None
    existing: bool = False
    article_title: str | None = None
    body_text: str | None = None
    scope: str = "mpaudio"
    tab_id: int | None = None
    editor_url: str | None = None
    transcode_timeout: int = 600
    poll_interval: int = 10
    dry_run: bool = False
    keep_open: bool = False
    log: list[str] = field(default_factory=list)


class AudioInserter:
    def __init__(self, opts: Options) -> None:
        self.opts = opts
        self.bsk = Bsk(scope=opts.scope, tab_id=opts.tab_id)

    # ------------------------------------------------------------------ 工具

    def _log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {redact(msg)}"
        self.opts.log.append(line)
        print(line, flush=True)

    def _js(self, template: str, **subs: str) -> str:
        js = template
        for key, value in subs.items():
            js = js.replace(f"__{key}__", value)
        return js

    def _eval(self, js: str, *, obs: Observation | None = None, retries: int = 3) -> Any:
        """执行 JS；页面帧切换导致上下文失效时，重新 sync 再试。"""
        last: BskError | None = None
        for attempt in range(retries):
            try:
                return self.bsk.evaluate_json(js, obs if attempt == 0 else None)
            except BskError as exc:
                last = exc
                text = f"{exc} {exc.stdout} {exc.stderr}"
                # 只有「动作还没派发就被拒」的错误才可以重放。
                # bsk 的 JSON 错误里 effect_state=none 才代表"什么都没发生"；
                # 拿不到 effect_state 时，只信参数检查类错误（它发生在派发之前）。
                # 注意：document_changed / 超时 / 结果未知 一律不重放（Never replay an uncertain action）。
                m = re.search(r'"effect_state"\s*:\s*"([a-z_]+)"', text)
                effect = m.group(1) if m else None
                param_check = (
                    "needs an explicit current observation" in text
                    or "the strict observation changed before this action" in text
                    # bsk 明说「在选动作之前帧就变了」= 什么都没派发
                    or "before choosing this action" in text
                )
                if effect != "none" and not (effect is None and param_check):
                    raise
                time.sleep(1.0 + attempt)
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------------ 步骤

    def preflight(self) -> dict[str, Any]:
        """不碰浏览器的前置检查：文件、bsk、扩展连接。"""
        checks: dict[str, Any] = {}
        checks["file"] = (
            os.path.isfile(self.opts.file) and os.path.getsize(self.opts.file) > 0
            if self.opts.file
            else True  # --existing 不需要本地文件
        )
        if self.opts.cover_file:
            checks["cover_file"] = os.path.isfile(self.opts.cover_file)
        try:
            out = self.bsk._run(["--version"])  # noqa: SLF001 - 只读探测
            checks["bsk"] = out.strip()
        except BskError as exc:
            checks["bsk"] = f"error: {exc}"
        try:
            doctor = self.bsk._run(["doctor"])  # noqa: SLF001
            checks["extension_connected"] = "extension connected" in doctor
            checks["protocol_warn"] = "different protocol version" in doctor
        except BskError as exc:
            checks["extension_connected"] = f"error: {exc}"
        checks["ok"] = bool(checks.get("file")) and checks.get("extension_connected") is True
        if self.opts.cover_file:
            checks["ok"] = checks["ok"] and checks.get("cover_file") is True
        return checks

    def open_editor(self) -> Observation:
        if self.opts.editor_url:
            self._log(f"打开编辑页 {self.opts.editor_url}")
            obs = self.bsk.open(self.opts.editor_url)
        else:
            self._log("打开后台首页并进入图文编辑器")
            obs = self.bsk.open("https://mp.weixin.qq.com/")
            res = self._eval(JS_GOTO_EDITOR)
            if res != "ok":
                raise BskError("首页取不到 token（多半未登录）")

        for _ in range(40):
            if self._eval("document.querySelectorAll('.ProseMirror').length >= 2"):
                self._log(f"编辑器就绪 tab={obs.tab_id}")
                return obs
            time.sleep(0.5)
        raise BskError("图文编辑器未就绪（未登录 / 改版 / 弹窗遮挡）")

    def fill_article(self) -> None:
        """把图文标题与正文写进去（否则保存时会因标题为空被拒）。"""
        if not self.opts.article_title:
            return
        paras = [p.strip() for p in (self.opts.body_text or "").split("\n") if p.strip()]
        if not paras:
            paras = [f"{self.opts.article_title}（自动生成的正文占位）。"]
        info = self._eval(
            self._js(
                JS_FILL_ARTICLE,
                TITLE_LIT=json.dumps(self.opts.article_title, ensure_ascii=False),
                BODY_LIT=json.dumps(paras, ensure_ascii=False),
            )
        )
        if not isinstance(info, dict) or not info.get("okTitle"):
            raise BskError(f"图文标题写入失败：{info}")
        self._log(f"图文已就绪：{info}")

    def open_audio_dialog(self) -> None:
        self._log("打开 音频 菜单")
        res = self._eval(self._js(JS_CLICK_SEL, SEL=".js_insertaudio"))
        if res != "ok":
            raise BskError(f"音频菜单打不开：{res}")

        for _ in range(20):
            if self._eval("!!document.querySelector('.js_audio_music_dialog')"):
                self._log("「插入音频」弹窗已打开")
                return
            time.sleep(0.5)
        raise BskError("「插入音频」弹窗未出现")

    def upload_audio(self, url: str, name: str) -> None:
        self._log("切到「上传音频」并注入文件")
        res = self._eval(JS_OPEN_UPLOAD)
        if res != "ok":
            raise BskError(f"上传音频入口不可用：{res}")

        for _ in range(20):
            if self._eval(
                "!!document.querySelector('.weui-desktop-form__controls input[type=file]')"
            ):
                break
            time.sleep(0.5)
        else:
            raise BskError("上传输入框未出现")

        mime = mimetypes.guess_type(name)[0] or "audio/mpeg"
        injected = self._eval(
            self._js(JS_INJECT_FILE, URL=url, NAME=name, MIME=mime)
        )
        if not isinstance(injected, dict) or not injected.get("ok"):
            raise BskError(f"文件注入失败：{injected}")
        self._log(f"文件已注入（{injected.get('size')} bytes）")

    def set_cover(self, url: str, name: str) -> None:
        """上传音频封面（不传则走「使用账号头像」）。"""
        self._log("上传音频封面")
        res = self._eval(JS_OPEN_COVER_PICKER)
        if res != "ok":
            raise BskError(f"封面入口不可用：{res}")

        for _ in range(20):
            if self._eval(
                "!!document.querySelector('.weui-desktop-dialog input[type=file][accept*=image]')"
            ):
                break
            time.sleep(0.5)
        else:
            raise BskError("封面选择弹窗未出现")

        injected = self._eval(
            self._js(JS_INJECT_COVER, URL=url, NAME=name)
        )
        if not isinstance(injected, dict) or not injected.get("ok"):
            raise BskError(f"封面注入失败：{injected}")

        if self._eval(self._js(JS_TOP_DIALOG_CLICK, LABEL="下一步")) != "ok":
            raise BskError("封面「下一步」不可用")
        # 编辑封面页的确认按钮可能是「确定」或「完成」，两个都试
        for label in ("确定", "完成"):
            if self._eval(self._js(JS_TOP_DIALOG_CLICK, LABEL=label)) == "ok":
                break
        self._log("封面上传流程走完")

    def fill_meta(self) -> None:
        self._log("填标题 / 封面 / 协议")
        info = self._eval(
            self._js(
                JS_FILL_META,
                TITLE_LIT=json.dumps(self.opts.title, ensure_ascii=False),
                COVER=json.dumps(self.opts.cover, ensure_ascii=False),
                SKIP_AVATAR="true" if self.opts.cover_file else "false",
            )
        )
        if not isinstance(info, dict):
            raise BskError(f"表单填写失败：{info}")
        self._log(f"表单：{info}")

    def submit_upload(self) -> None:
        self._log("保存音频素材")
        res = self._eval(JS_SUBMIT_UPLOAD)
        if res != "ok":
            raise BskError(f"保存失败：{res}")

    def refresh_audio_list(self) -> None:
        """关掉再打开「插入音频」，逼它重新拉一次列表。

        真机发现：素材列表不会自己刷新，转码完成后仍显示「转码中」，必须重开弹窗。
        """
        self._log("刷新音频列表")
        self._eval(JS_CLOSE_DIALOG)
        time.sleep(0.8)
        for _ in range(3):
            if self._eval(self._js(JS_CLICK_SEL, SEL=".js_insertaudio")) == "ok":
                break
            time.sleep(0.5)
        for _ in range(20):
            if self._eval("!!document.querySelector('.js_audio_music_dialog')"):
                return
            time.sleep(0.5)
        raise BskError("重新打开「插入音频」失败")

    def wait_transcoded(self) -> None:
        self._log(f"等待转码（上限 {self.opts.transcode_timeout}s）")
        deadline = time.time() + self.opts.transcode_timeout
        time.sleep(min(10, self.opts.poll_interval))  # 给保存请求一点时间落地
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            # 列表不会自动刷新：每轮都关掉弹窗重开一次（第一轮也要）
            self.refresh_audio_list()
            text = self._eval(JS_DIALOG_TEXT) or ""
            if not isinstance(text, str) or not text.strip():
                raise BskError("音频列表读不到内容（弹窗可能被关掉了）")
            if self.opts.title and self.opts.title not in text:
                # 列表异步加载，首几轮读不到很正常，继续等
                self._log("列表里还没出现目标音频，继续等")
                time.sleep(self.opts.poll_interval)
                continue
            if "转码中" not in text and "审核中" not in text:
                self._log(f"转码结束：{text[:120]}")
                return
            time.sleep(self.opts.poll_interval)
        raise BskError("等待转码/审核超时（音频可能仍在处理，稍后重试可续）")

    def insert_into_article(self) -> list[dict[str, Any]]:
        self._log("选中音频并插入")
        sel = self._eval(
            self._js(
                JS_SELECT_AND_INSERT,
                TITLE_LIT=json.dumps(self.opts.title, ensure_ascii=False),
            )
        )
        if sel not in ("selected", "selected-untrusted", "already-selected"):
            raise BskError(f"选择音频失败：{sel}")
        time.sleep(0.6)
        res = self._eval(JS_CLICK_INSERT)
        if res != "ok":
            raise BskError(f"插入失败：{res}")
        time.sleep(1.5)
        found = self._eval(JS_FIND_AUDIO_NODE)
        hits = found.get("hits", []) if isinstance(found, dict) else (found or [])
        self._log(f"正文音频节点 {len(hits)} 个：{json.dumps(hits, ensure_ascii=False)[:300]}")
        if not hits:
            raise BskError("插入后正文里找不到音频节点（可能被编辑器丢弃）")
        if not any(h.get("fileid") for h in hits):
            raise BskError(f"音频节点缺少 voice_encode_fileid：{json.dumps(hits, ensure_ascii=False)[:300]}")
        return hits

    def save_draft(self) -> None:
        if not self.opts.article_title:
            raise BskError("没有图文标题，拒绝保存（保存空标题会被后台拒绝，且看不出失败）")
        self._log("保存草稿")
        res = self._eval(JS_SAVE_DRAFT)
        if res != "ok":
            raise BskError(f"保存草稿失败：{res}（正文可能已插入但未落盘）")
        self._log("保存结果为 ok，仍需回读草稿确认")

    # ------------------------------------------------------------------ 编排

    def run(self) -> dict[str, Any]:
        if self.opts.dry_run:
            self._log("dry-run：只做前置检查，不碰浏览器")
            checks = self.preflight()
            self._log(f"前置检查：{checks}")
            return {"dry_run": True, "ok": checks.get("ok", False), "checks": checks}

        try:
            self.open_editor()
            self.open_audio_dialog()
            if self.opts.existing:
                self._log(f"复用素材库已有音频：{self.opts.title}")
                self.fill_article()
            else:
                with serve_audio(self.opts.file) as url:
                    name = os.path.basename(self.opts.file)
                    self.upload_audio(url, name)
                if self.opts.cover_file:
                    with serve_audio(self.opts.cover_file) as curl:
                        self.set_cover(curl, os.path.basename(self.opts.cover_file))
                self.fill_article()
                self.fill_meta()
                self.submit_upload()
                self.wait_transcoded()
            nodes = self.insert_into_article()
            self.save_draft()
            return {"ok": True, "nodes": nodes}
        finally:
            if not self.opts.keep_open:
                self.bsk.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把音频插进公众号图文草稿（BrowserSkill 驱动）")
    parser.add_argument("--file", default=None, help="本地音频（mp3/wav/m4a…）")
    parser.add_argument("--title", required=True, help="音频素材标题（≤64 字）")
    parser.add_argument("--cover", default="avatar", help="头像 avatar 或封面图路径")
    parser.add_argument(
        "--cover-file", default=None, help="音频封面图（≤900x383 最佳），给了就上传封面而不是用头像"
    )
    parser.add_argument("--existing", action="store_true", help="复用素材库已有音频（跳过上传）")
    parser.add_argument("--article-title", default=None, help="图文标题；不给只插音频不建稿")
    parser.add_argument("--body-text", default=None, help="正文纯文本，换行分段")
    parser.add_argument("--scope", default="mpaudio", help="bsk scope")
    parser.add_argument("--tab-id", type=int, default=None)
    parser.add_argument("--editor-url", default=None, help="已有草稿的编辑页 URL")
    parser.add_argument("--transcode-timeout", type=int, default=600)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    opts = Options(
        file=args.file,
        title=args.title,
        cover=args.cover,
        cover_file=args.cover_file,
        existing=args.existing,
        article_title=args.article_title,
        body_text=args.body_text,
        scope=args.scope,
        tab_id=args.tab_id,
        editor_url=args.editor_url,
        transcode_timeout=args.transcode_timeout,
        poll_interval=args.poll_interval,
        dry_run=args.dry_run,
        keep_open=args.keep_open,
    )
    inserter = AudioInserter(opts)
    try:
        result = inserter.run()
    except BskError as exc:
        print(f"失败：{exc}\n{exc.detail}", flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())




















