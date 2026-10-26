# 聊天节奏 · astrbot_plugin_chat_pacer

给 AstrBot 用的一个很小很小的插件：**把私聊里连发的几句话攒起来，合成一条再回**。

不用再为了「每一句都会触发一次回复」而憋一大段话，可以像微信聊天那样一句一句发。

## 它做什么

1. 你发消息 → 先不回复，进缓冲，等 `window_seconds` 秒
2. 窗口内又来新消息 → 计时重来，前一条被 `stop_event()` 掐掉，不产生回复
3. 窗口内没动静了 → 把攒的几句拼成一段，交给 LLM，**只回一次**
4. 你撤回了某条 → 从缓冲里剔掉；整批都撤回 → 这次不回

## 安装

把整个目录放进 AstrBot 的插件目录：

```
<AstrBot>/data/plugins/astrbot_plugin_chat_pacer/
├── main.py
├── metadata.yaml
├── _conf_schema.json
└── README.md
```

然后在 WebUI（默认 `http://localhost:6185`）→ 插件 → 重载该插件。

## 配置

| 项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关 |
| `window_seconds` | float | `4.0` | 合并窗口（秒）。4 秒比较自然，打字慢可以调到 6 |
| `recall_skip` | bool | `true` | 撤回的消息不参与回复；关掉则照常回 |
| `include_group` | bool | `false` | 群聊也生效。默认只管私聊 |
| `debug_log` | bool | `false` | 在 `data/plugin_data/.../pacer.log` 记录撤回事件，排查用 |

## 实现说明

钩子挂在 `OnWaitingLLMRequestEvent`（拿 LLM 锁之前）。框架在该钩子执行完后会检查
`event.is_stopped()`，若已停止就直接返回、不调用 LLM。
因此「同批消息只保留最后一条」这件事不需要改框架代码。

撤回事件由平台适配器以 `post_type == "notice"` 的形式送进来，`raw_message` 里带
`message_id`，据此把对应条目从缓冲中移除。

## 已知限制

- 只在进程内生效，AstrBot 重启后缓冲清空（这是期望行为）
- 撤回必须发生在窗口结束前，否则回复已经发出，收不回来
- 指令类消息（以 `/` 开头）不参与合并

## License

MIT
