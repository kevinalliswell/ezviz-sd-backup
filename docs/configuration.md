# 配置与凭据约定

版本 1 配置契约已由 P1 `probe` 和凭据设置命令使用。[完整示例](../examples/config.example.toml)可被静态检查解析；每日备份运行时仍未实现，不能据此启动自动备份。

## 默认目录

| 内容 | 路径 |
| --- | --- |
| 配置与数据根目录 | `~/Library/Application Support/ezviz-sd-backup/` |
| 配置 | 根目录下的 `config.toml` |
| 进度数据库 | 根目录下的 `state.sqlite3` |
| 暂存及完成原片 | 根目录下的 `spool/` |
| aliyunpan 私有配置 | 根目录下的 `aliyunpan/` |
| 脱敏日志 | `~/Library/Logs/ezviz-sd-backup/` |
| LaunchAgent | `~/Library/LaunchAgents/io.github.kevinalliswell.ezviz-sd-backup.plist` |

根目录权限设为 `0700`，私有文件设为 `0600`。TOML 中的 `~` 由应用展开；launchd plist 不展开 `~`、变量或 shell 表达式，必须填写绝对路径。

## 字段

| 字段 | 类型 / 默认值 | 约束与含义 |
| --- | --- | --- |
| `config_version` | 整数 / `1` | 不支持的版本直接失败 |
| `camera.name` | 字符串 / `客厅` | 本地展示和远端目录名；净化路径字符 |
| `camera.device_serial` | 字符串 | 用户私有配置填写，公开示例仅占位 |
| `camera.channel` | 整数 / `1` | 正整数 |
| `ezviz.keychain_service` | 字符串 | 用于定位 AppKey、AppSecret 及需要的视频验证码 |
| `backup.timezone` | IANA 时区 / `Asia/Shanghai` | 明确事件日期语义，不能使用固定 UTC 偏移替代 |
| `backup.lookback_days` | 整数 / `3` | 每次发现最近三个完整自然日；已知未完成任务另行恢复 |
| `backup.max_segment_seconds` | 整数 / `1800` | 长事件确定性分段上限；不是截断上限 |
| `backup.max_run_seconds` | 整数 / `10800` | 达到上限后保留待办并退出 6 |
| `backup.download_concurrency` | 整数 / `1` | MVP 只接受 1 |
| `backup.upload_concurrency` | 整数 / `1` | MVP 只接受 1 |
| `storage.data_dir` | 路径 | 数据库、令牌缓存、暂存目录的父目录 |
| `storage.max_spool_bytes` | 整数 / `2147483648` | 2 GiB；完成原片与 partial 都计入 |
| `storage.min_free_bytes` | 整数 / `5368709120` | 5 GiB；写入过程中持续检查 |
| `media.ffmpeg_path` | 绝对路径 | 指向用户已安装的 FFmpeg |
| `media.ffprobe_path` | 绝对路径 | 指向同一发行版本的 ffprobe |
| `alipan.binary_path` | 绝对路径 | 指向固定版本的 aliyunpan |
| `alipan.config_dir` | 路径 | 专用工具凭据目录，不能指向仓库 |
| `alipan.remote_root` | 字符串 / `/家庭监控` | 云端目录前缀，不是本地路径 |
| `cleanup.enabled` | 布尔 / `false` | 默认不删除本地视频 |
| `cleanup.keep_days` | 整数 / `14` | 开启后仍须满足核验等级及保留天数 |
| `cleanup.require_verification` | 字符串 / `sha1` | MVP 只接受 sha1，不自动降级 |
| `logging.level` | 字符串 / `INFO` | 不提供会落盘输出凭据的调试模式 |
| `logging.directory` | 路径 | 应用与调度器的脱敏日志目录 |

API 超时、最多三次网络尝试及退避使用经过测试的程序默认值，初期不暴露大量调优字段。凭据错误、设备不支持和无进展分页不走普通网络重试。

## Keychain 契约

使用 macOS 登录钥匙串，服务名默认 `io.github.kevinalliswell.ezviz-sd-backup`，账号键分别为 `ezviz.app_key`、`ezviz.app_secret` 和可选的 `ezviz.video_code`。

`set-credentials` 在本机隐藏输入，并通过 Security framework 写入 Keychain，不把密钥放在命令参数、shell 历史、TOML、环境文件或项目仓库里。访问令牌缓存由应用以受限权限保存并提前刷新；所有预期错误在输出之前脱敏。

Keychain 在锁屏、重新登录和重启之后的非交互读取行为必须实测。读取被拒绝时退出并提示用户处理，不能修改系统的钥匙串安全设置以强行绕过。

## 示例路径说明

示例采用 Apple Silicon 常见的 `/opt/homebrew/bin/`；Intel 常见路径可能是 `/usr/local/bin/`。这些都是示例而非自动探测结果，部署时必须用实际安装路径替换。

默认不提供真实设备序列号、密钥和访问令牌。`examples/` 中的文件适合公开，复制到用户 Library 并填入真实配置后的文件必须留在仓库外。
