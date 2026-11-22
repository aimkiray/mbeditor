"""BrowserSkill (bsk) 驱动的公众号图文音频插入。

模块划分：
- ``bsk``          : bsk CLI 的薄封装（sync / evaluate / click / open / close）
- ``audio_server``: 本机 CORS 静态服务，把音频文件喂给页面里的 <input type=file>
- ``insert_audio``: 编排「打开编辑器 → 插入音频 → 存草稿 → 回读校验」

设计约束：
- 只用标准库，不引入新依赖（沿用仓库 backend 的保守做法，AGENTS.md 未强制）。
- 不触碰 backend/app/api/v1/publish.py，发布主链路保持原样。
- 这条能力只服务「音频卡片」，是官方草稿接口结构上做不到的那一格。
"""

__all__ = ["bsk", "audio_server", "insert_audio"]
__version__ = "0.1.0"

