# 个人微信 iLink 连接器

OpenWorker 的 `wechat_ilink` 连接器用于把发给已扫码账号的个人微信私聊交给本机 agent，并在当前运行期会话上下文内回复文本。它与企业微信自建应用连接器 `wecom` 完全分离。

## 能力范围

支持：

- 微信扫码登录与重新认证；
- 多个个人微信账号并列连接；
- 每账号独立允许列表；
- 私聊文本入站与文本回复；
- 图片、文件和视频转换为明确占位文字；
- 语音优先使用 iLink 服务返回的转写，没有转写时显示语音占位；
- 长轮询、断线重连、消息去重和登录失效检测；
- 收到联系人消息后，使用只存在于内存中的当前会话上下文进行有限主动发送。

不支持，也不会在 UI/API 中声称支持：

- 群聊或 thread；
- 图片、语音、文件或视频出站；
- 下载或读取真实附件；
- 没有当前会话上下文时主动发送；
- 进程重启后保留会话上下文；
- 交互按钮或真正的 delivery-confirmed streaming。

## 连接与授权

1. 打开“设置 → 集成 → 连接器 → 个人微信”。
2. 点击“连接”或“添加账号”。
3. 用个人微信扫描二维码并在微信中确认。
4. 让联系人向该账号发送一条私聊消息。
5. 在对应账号的“最近发送者”或等待消息中选择“允许”或“允许并投递”。

账号级 target 格式为：

```text
wechat_ilink:<account_id>/<user_id>
```

`account_id` 必不可少。相同联系人 ID 在两个微信账号下属于不同授权范围。

## 凭据与运行期数据

SecretStore profile：

```text
wechat_ilink:account:<account_id>
wechat_ilink:default
```

安全边界：

- `bot_token` 只存在于后端和 SecretStore；
- QR polling transaction 只存在于后端短期 attempt registry；
- `context_token`、long-poll cursor 和去重窗口只存在于 adapter 内存；
- renderer、HTTP 公开 DTO、日志、模型 prompt 和工具参数不接收上述 secret；
- 前端只收到随机 `attempt_id`、可显示的 QR 内容和脱敏状态；
- QR 内容由前端在本地 canvas 渲染，不向第三方图片 URL 发起请求；
- iLink API 继续使用 sidecar 的 `X-OpenWorker-Token` 鉴权。

进程重启会清除所有 `context_token`。这时账号仍可接收入站消息，但必须先收到某个联系人的新消息，才能向该联系人发送文本。

## 网络边界

默认服务 origin：

```text
https://ilinkai.weixin.qq.com
```

后端传输层：

- 仅接受 HTTPS、默认端口和微信 vendor hostname policy；
- QR 确认返回的 `baseurl` 会重新验证；
- 拒绝 URL userinfo、IP literal、私网、loopback、link-local、CGNAT、metadata、reserved 和 multicast 地址；
- 禁止自动 redirect；
- `trust_env=False`，使用明确超时和响应体上限；
- 公开异常只包含稳定错误码，不反射 token、header、响应体或 secret URL。

## 状态与排障

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `live` | 正常长轮询 | 无需操作 |
| `reconnecting` | 网络或可恢复协议错误 | 等待自动退避重连 |
| `auth_required` | 服务端返回 `-14` 或凭据失效 | 点击“重新扫码” |
| `offline` | worker 未运行或账号被禁用 | 检查 Gateway/账号状态 |
| `failed` | 不可继续的安全或协议错误 | 查看脱敏错误，重新认证 |

重新认证会绑定原账号 ID；如果二维码确认到不同账号，后端会拒绝覆盖原 profile。

断开账号只删除本机凭据与运行期上下文。当前观察到的协议没有可验证的远端 revoke API，因此 UI 不会声称远端令牌已撤销。

## 协议来源与稳定性

该实现是基于已观察 iLink 行为的独立 Python behavioral reimplementation。OpenWorker 没有复制参考项目的 TypeScript 源码、注释、类型或测试数据，也没有把 iLink 表述为微信官方公开稳定 SDK。

服务端协议变化可能导致连接失效、需要重新扫码或要求更新 OpenWorker。真实扫码测试时不得把二维码、token、context、日志或截图加入 fixture、issue、文档或提交记录。
