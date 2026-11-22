# 图文里插音频：真机测绘与自动化实现

> 目标：让本地音频进入公众号图文的正文。
> 变成用户能点的音频卡片。
> 结论先说：**官方开放接口没有这条路，后台有，而且只存在于后台 DOM 里。**

## 1. 为什么官方接口做不到

| 路径 | 结局 |
|---|---|
| `material/add_material?type=voice` | 只收 ≤2MB / ≤60s 的语音，且返回的是 `media_id`，不是卡片要的 `voice_encode_fileid` |
| `draft/add` 的 `content` | 是 HTML 字符串；音频卡片是编辑器 schema 里的**插件节点**，不在白名单里的标签会被静默丢弃 |
| `operate_appmsg` 手拼 `<mpvoice>` | 服务端有专门校验：`64510 内容不能包含音频` / `64511 内容不能包多个音频` / `64512 文章中音频错误,请使用音频添加按钮重新添加。` |

所以唯一入口是后台编辑器工具栏的**音频按钮**——它由编辑器插件生成节点，服务端认。

## 2. 真机测绘（2026-11-22，公众号「<账号名>」）

### 2.1 工具栏

```html
<!-- 下拉菜单，常驻 DOM，不依赖 hover 也能点 -->
<ul class="tpl_dropdown_menu js_audio_dropdown_menu">
  <li class="tpl_dropdown_menu_item js_insertaudio">音频</li>
  <li class="tpl_dropdown_menu_item js_insertmusic">音乐</li>
</ul>
```

点 `.js_insertaudio` → 打开「插入音频」弹窗 `.js_audio_music_dialog`：

```
插入音频 | 从素材库选择 / 从视频号选择 | 上传音频 | 列表 | 已选择 0 / 10 个音频 | 插入 取消
```

> 计数所见：`已选择 0 / 10 个音频`。音乐侧与"第 11 个"行为**未观察**，不要当成接口约束。

### 2.2 上传弹窗（关键）

点「上传音频」→ 出现子弹窗，原文写着：

> 格式支持 mp3、wma、wav、amr、m4a，**文件大小不超过 1G**。上传后音频将进行**转码和审核**。

> 注意：**1GB 是上传弹窗的 UI 文案**，不是接口契约。实际可用上限还受单次 evaluate 超时（约 90s）、页面整文件进 Blob 内存、MIME 推断等制约。**实测样本只有 10 秒 / 40KB**，大文件未验证。

必填项：音频文件 + 封面（可直接「使用账号头像」）+ 标题（≤64 字）+ 勾选协议。

### 2.3 上传协议（抓包所得）

```
POST /cgi-bin/audioupload?action=init_upload
POST /cgi-bin/audioupload?action=upload_block&part_sha=<sha1>&audio_id=wxaudio_<id>&part_num=1&f=json&ajax=1&token=<token>
POST /cgi-bin/audioupload?action=upload_finish&audio_id=wxaudio_<id>&screen_shot_type=undefined&f=json&ajax=1&token=<token>
```

分块上传，`audio_id` 形如 `wxaudio_xxxxxxxx`。上传完成 ≠ 可用：列表里会显示
**「转码中」**，转码（以及内容审核）通过后才能真正选中插入。

**坑：列表不会自动刷新。** 真机上转码其实几分钟就完成了，但弹窗一直显示「转码中」，
必须关掉弹窗再重新打开（取消 → 音频 → 音频），状态才会更新；代码里是**每轮轮询都重开一次**，并且把「审核中」也算作未就绪。
`insert_audio.py` 的 `refresh_audio_list()` 做的就是这件事。

### 2.4 插入后的节点（2026-11-22 实测）

**注意：新版编辑器里不是 `<mpvoice>`，而是一个 `<section>` 插件节点**：

```html
<section
  src="/cgi-bin/readtemplate?t=tmpl/audio_tmpl&amp;name=…&amp;play_length=10秒"
  isaac2="1" low_size="6.36" source_size="6.4" high_size="78.41"
  name="自动化测试音频" play_length="10000" author="<账号名>"
  data-topic_id="" data-topic_name=""
  data-pluginname="insertaudio"
  data-trans_state="1" data-verify_state="3"
  voice_encode_fileid="Mzcw***（已脱敏）"
  class="js_editor_audio audio_iframe res_iframe js_uneditable custom_select_card">
</section>
```

要点：

- `voice_encode_fileid` 仍然在，解码为 `<账号 uin>_<音频 id>` —— 正是
  **`<账号 uin>_<音频 id>`**，与之前从老文章里反推出的结构一致。
- `play_length` 单位是**毫秒**（10000 = 10 秒）；`name` 是素材标题；`author` 取公众号名。
- `data-trans_state` / `data-verify_state` 是转码与审核状态位 —— 自动化可以据此判断
  卡片是否「可用」，比读列表文案更可靠。
- 卡片在正文里还有一份**渲染态**：

  ```html
  <section class="appmsg_card_context appmsg_card_active audio_card js_audio_card">
    <section class="audio_card_wrp">
      <mp-common-mpaudio src="…" cover="https://wx.qlogo.cn/…" author="…"
        name="…" play_length="00:10" duration="10" …></mp-common-mpaudio>
    </section>
  </section>
  ```

  所以「老文章里的 `mpvoice`」和「新编辑器里的 `section[data-pluginname=insertaudio]`」
  是同一件事的两代写法，抓取/校验时两种都要认。

### 2.5 音频封面（2026-11-22 实测到一半）

入口：上传弹窗里的「上传封面」→ 打开「选择图片」弹窗：

```
选择图片 | 选择封面 / 编辑封面 | 最近使用 (0) / 我的图片 (0) / 未分组 (0) / 新建分组
原"公共图片库"已下线，推荐使用"AI 配图" | 上传文件 | 下一步
```

坑（已踩）：这个弹窗里的 file input **和音频文件的 input 不是同一个**。
音频 input 在 `.weui-desktop-form__controls` 里、`accept=""`；
封面 input 是 `input[type=file][accept*=image]`。
注错位置的表现是上传弹窗报「重新上传 _cover.png / 上传失败：不支持该音频格式」。

流程：上传文件 → 下一步 → 编辑封面（裁剪）→ 确定。

**状态**：代码已实现（`set_cover()` + `--cover-file`），但**没有任何一次成功记录**；选择器按上面的 DOM 写好，
但端到端**未跑通验证**（见 §7 环境问题）。保守用法仍是 `--cover avatar`（用账号头像）。

## 2.6 发表闸口（只见到弹窗文案，未实际点发表）

点发表时弹出下面两条（只观察到弹窗，**未走完发表流程**）：

- 「公众号尚未实名　前往实名」
- 「未设置头像和名称　发表内容需要完善公众号头像和名称　去设置」

这是平台侧闸口，不是脚本能绕的。**草稿是否可存，本仓库尚未成功验证过（见 §7）。**

## 3. 与 BrowserSkill 的配合

编辑器是 ProseMirror，页面持续重绘：**bsk 的 observation 很容易在两条命令之间过期**，
表现为 `the control context no longer belongs to the current document`。

两个绕法，本模块都用了：

1. **用 `evaluate` 里的 `el.click()` 代替 bsk 的 click**——同一进程内完成，
   不跨命令，绕开 ref 失效。
2. **不用 bsk 的 upload**（本机实测在该页面持续报上下文过期），改成
   「本机 CORS 服务 + 页面内 `fetch` → `new File()` → `input.files = dt.files`
   → 派发 `change`」。页面组件照常收到事件，上传协议照常跑。

## 4. 前提与用法

先决条件（缺一样就跑不起来，文档原先没写）：

| 需要什么 | 怎么确认 |
|---|---|
| `bsk` CLI（BrowserSkill） | `bsk --version` |
| 浏览器装了 BrowserSkill 扩展并已连接 | `bsk doctor` 里有 `extension connected` |
| 该浏览器已登录公众号后台 | 打开 mp.weixin.qq.com 能看到后台首页 |
| 能开 Agent Window | `bsk open https://mp.weixin.qq.com/` 有正常输出 |

先跑前置检查（不碰浏览器；`ok:false` 时进程以 exit 2 退出）：

```bash
python -m scripts.mp_audio.insert_audio --file a.mp3 --title t --dry-run
```

```bash
# 不传 --editor-url 时：打开后台首页 → 自动跳到新建图文 → 开始操作
python -m scripts.mp_audio.insert_audio \
  --file D:\audio\ep12.mp3 \
  --title "第 12 期：长音频测试"
```

参数：

| 参数 | 说明 |
|---|---|
| `--file` | 本地音频，mp3/wma/wav/amr/m4a（上限以实测为准，见 §2.2） |
| `--title` | 音频素材标题，≤64 字 |
| `--cover` | 只接受 `avatar`（默认）；要传图请用 `--cover-file` |
| `--cover-file` | 音频封面图；给了就不再用账号头像（**该流程未端到端验证**） |
| `--editor-url` | 已有草稿的编辑页 URL；不传则由首页自动跳到新建图文 |
| `--transcode-timeout` | 等转码的上限，默认 600s |
| `--keep-open` | 结束后不关会话，便于人工复核 |

## 5. 已知边界

- **必须先有登录态**：脚本不碰账号密码，走用户已登录的浏览器。
- **转码+审核是异步的**：小文件也要几分钟，长音频更久；`--transcode-timeout` 要放宽。
- **未实名账号发不出去**：点发表会被拦（平台限制，非脚本问题）。「能存草稿」这条**尚未验证**（本仓库还没有一次成功保存草稿的记录）。
- **改版即失效**：选择器是按 2026-11 的后台版本抓的，微信一改就得重新测绘。
- **风控**：短时间批量插入大量音频属于异常行为，生产使用请限速。

## 6. 与仓库其它部分的关系

- 不动 `backend/app/api/v1/publish.py`，不动 `publish_adapter` 的两条既有权重路径。
- 本模块是**旁路工具**：只负责「把音频卡片放进草稿」，排版/图片/发布仍走原有链路。
- 需要把「音频素材 id」记进文章元数据时，由调用方（前端或脚本）自行保存，本模块不写库。

## 7. 环境问题：bsk 扩展版本不匹配（2026-11-22 实测）

当天 daemon 是 0.5.0（protocol 1.9），浏览器扩展还是 0.4.2（protocol 1.8）。`bsk doctor` 已经明说：

```
WARN feature availability  unavailable: extension 1b50fcc5 (): read;
                           extension 1b50fcc5 (): evaluate --unverified;
hint: Please update the BrowserSkill extension in the affected browser profile
```

症状：**任意写操作被拒**，报

```
error: the user has control of this tab
details: User activity revoked this action
```

换新 scope、新窗口、等 90 秒、只用 evaluate 不用原生 click，都压不住。
读操作（`sync` / `snapshot` / 纯读的 `evaluate`）正常，所以**很容易误判成"是人在操作浏览器"**。

处置顺序（**按这个顺序查，别一上来就怪版本**）：

1. `bsk ledger status` —— 有没有 `pending_unknown` / `frozen`；有就先 `ledger resolve`；
2. `bsk cooperate status` —— `ready` 是否 true、控制权在谁手上；
3. `bsk doctor` —— 扩展协议版本是否对得上（本例 0.4.2/1.8 vs daemon 0.5.0/1.9，只是诱因）。

**归因结论（2026-11-22 评审修正）**：「版本不匹配」不足以解释「同一会话先能用、后全拒」
（评审置信度：不成立 0.60 / 部分成立 0.38 / 成立 0.02）。更可能是
**a) 控制上下文作废**（未 reconcile 的 UNKNOWN 让 tab 级「人持有控制」标记一直挂着），
**b) 自伤**（`evaluate` 里的 `el.click()` 与合成 `input/change` 被扩展记成"人"的活动）。
两者常常一前一后。

这也说明：**换 scope / 换窗口救不了**——标记是 tab 级的，新会话拿到的还是同一个 tab。

### 2026-11-22 上午的复现（关键补充）

按上面的顺序先查了 ledger（`frozen:false`、`pending_unknown:[]`，干净），然后**在全新窗口、新 tab、不做任何 paste** 的情况下重试：

| 动作 | 结果 |
|---|---|
| 打开首页 → `location.href` 进编辑器 | ✅ ok |
| 点 `.js_insertaudio` 打开音频弹窗 | ✅ ok |
| **紧接着**选中素材 / 点插入（第二个 evaluate） | ❌ `the user has control of this tab` |

`bsk cooperate status` 里能看到一条 `{"label":"INPUT","target_id":53,"type":"input","sequence":1}` ——
某个 `<input>` 上出现了一条 `input` 活动记录（**来源未定位**：可能是页面自身，也可能是本模块 `input.files=...` 后派发的 `input/change` 被记成人为活动），扩展把它记成"人在输入"，
之后再想动这个 tab 的**那一步**就被拒（同一 tab 上更早的保存素材、填表、开弹窗都是成功的，所以**不是「写操作全被拒」**，是「这一步被拒」）。新 tab 未做 cooperate 采样。

三条旁证：

1. `example.com` 上做过「纯 mutation + 合成 input 事件」，两者都没触发；**但没做「开弹窗」这种复合动作的对照**，所以不能当旁证；
2. 同一 tab 上 `sync`/`snapshot`/纯读 JS 一直正常；
3. 20 秒内两次采样，这条 activity 的时间戳**不变**——只能说「这 20 秒没有新的输入」，
   「一次性事件 / 标记粘性」本身**未证**。

**根因（2026-11-22 已定位并验证）**：不是页面、不是版本不兼容，是**我们自己的点击**。

- BrowserSkill 扩展实装版本 **0.5.0**（2026-11-13 构建）。它的 `cooperation.js` 把所有
  `isTrusted` 且值发生变化的 `input/change` 都记成「人在输入」，**没有**过滤程序化激活。
- Chromium 对 `checkbox.click()`（即便 click 本身是 untrusted）会由 UA 派发 **trusted** 的
  `input` → 扩展记一条 human activity → 当场撤销控制上下文 → 报
  `the user has control of this tab`。
- 上游已在 **`e5ab272b`（2026-11-19）** 修掉这个误判，提交信息里点名
  `checkbox.click()` / `label.click()` 会造成 false human_intervention。0.5.0 早于该修复。

**绕过（已验证可用）**：选素材时不要点 checkbox，改成直接设值 + 派发 untrusted 事件：

```js
cb.checked = true;
cb.dispatchEvent(new Event("input", { bubbles: true }));
cb.dispatchEvent(new Event("change", { bubbles: true }));
```

扩展只认 trusted 事件，因此不会误判；页面照样把「已选择 1 / 10」更新出来。
`JS_SELECT_AND_INSERT` 已按这个写法改掉（返回值 `selected-untrusted`）。

**改完之后的结果（2026-11-22 10:01）**：三篇草稿全部由脚本建出并保存成功——

| 草稿 | 标题 | 结果 |
|---|---|---|
| A | 自动化烟测 A | 保存成功（appmsgid=100000004，手动验证） |
| B | 自动化烟测 B | `exit 0`，`ok:true`，正文 1 个音频节点 |
| C | 自动化烟测 C | `exit 0`，同 B |

草稿箱回读：`文章 4`，列表里可见「自动化烟测 C / A / B」。

**下一步**：升级扩展到含 `e5ab272b` 的版本后，这段 untrusted 写法可以保留（更安全），
但届时 `checkbox.click()` 也不再会误伤，两条路都能走。

写在这里免得下次又去怀疑选择器。



## 9. 多图文（1 头条 + N 次条）

公众号一条推送可以带多篇：头条 + 次条。入口是编辑器左侧的 **「增加一条」**
（`#js_add_appmsg`），点一次加一个次条，每个次条有独立的标题/正文/封面。

**实测（2026-11-22，未实名账号）**：该控件外层带 `js_readonly`，点了不生效；
点「选择封面」会直接弹「公众号尚未实名 / 未设置头像和名称」。
也就是说**未实名账号做不了多图文**，这不是脚本问题。

工具这边已经支持：

```bash
python -m scripts.mp_audio.insert_audio --existing \
  --title "音频素材标题" \
  --article-title "头条标题" --body-text "头条正文" \
  --extra-article "次条一标题::次条一正文" \
  --extra-article "次条二标题::次条二正文"
```

行为：头条插完音频后，逐个点「增加一条」并写入标题/正文，最后一起保存成**一个草稿**。
控件是只读时直接报 `add-readonly`，并说明「需要已实名 + 头条已有封面」，不会静默失败。

**未验证**：在已实名账号上这条路径能否走通（本仓库没有已实名账号可测）。
