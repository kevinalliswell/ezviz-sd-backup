# Mac 部署设计

本文描述应用实现后的部署流程。当前只有文档、配置示例和 plist 模板，**不能安装为可工作的备份任务**。

## 运行环境

计划支持已安装 Python 3.12+、FFmpeg/ffprobe 和固定版本 aliyunpan 的 macOS。Apple Silicon 与 Intel 都列为目标，尚未实测，不先承诺最低 macOS 版本。

Python 使用独立环境；即使运行时最终只有标准库，也不要依赖系统自带 Python。FFmpeg 与 aliyunpan 作为外部可执行文件，不随本仓库打包、不自动升级；安装来源、版本和第三方许可证由部署文档在实测后补充。

部署者应先完成 [测试计划](testing.md) 的能力探测和手动单次备份，再安装定时任务。登录、扫码、Keychain 初次授权在用户自己的 Mac 上完成。

## 每日调度

选用用户级 LaunchAgent，每天 **03:15** 启动一次任务。时间采用 **Mac 系统本地时区**，TOML 的 `backup.timezone` 只决定录像日期及归档语义，不会改变 launchd 的调度时区。

模板：[io.github.kevinalliswell.ezviz-sd-backup.plist](../examples/launchd/io.github.kevinalliswell.ezviz-sd-backup.plist)。应用实现后，替换全部占位符：

| 占位符 | 应填内容 |
| --- | --- |
| `__PYTHON_BIN__` | 应用独立环境内 Python 的绝对路径 |
| `__APP_DIR__` | 包已可导入的应用目录绝对路径 |
| `__CONFIG_PATH__` | 用户 Library 下真实 TOML 的绝对路径 |
| `__LOG_DIR__` | 已预先创建的脱敏日志目录绝对路径 |

launchd 不通过 shell 执行，不展开 `~`、环境变量或重定向。路径可包含空格；保持每个参数为单独的 XML `string`，不要添加 shell 引号。替换时遵循 XML 转义规则，用 `plutil -lint` 检查渲染后的文件。仓库静态检查只验证未渲染模板。

模板不设置 `KeepAlive` 或 `RunAtLoad`；不会因为失败而持续重启，也不在用户每次登录时立即补跑。使用 `ProcessType=Background`、低 CPU 调度优先级和低磁盘 I/O 优先级；这些设置不等于 CPU、内存或网络的硬限额。

## 实现后的安装顺序

1. 按 [配置约定](configuration.md) 创建受限权限的数据、日志目录和真实配置；真实文件留在仓库外。
2. 安装经固定版本测试的应用和外部工具，填写绝对路径。
3. 在目标登录用户下完成凭据设置、`probe`、手动 `run` 及远端核验。上述 CLI 仍待实现。
4. 渲染模板到 `~/Library/LaunchAgents/`，检查文件没有未替换占位符，日志父目录已经存在，权限只允许当前用户修改。
5. 通过 `launchctl bootstrap gui/<uid> <plist绝对路径>` 注册。`<uid>` 是当前用户数值 ID；此处为操作说明，不能原样执行。
6. 用 `launchctl print gui/<uid>/io.github.kevinalliswell.ezviz-sd-backup` 检查注册状态；用 `kickstart` 触发一次受控测试，核对退出码和数据库运行摘要。
7. 观察连续三天自动运行，记录资源、耗时及补漏结果后才启用日常备份。

更新 plist 或应用路径时先 `bootout` 移除注册，再检查并重新 `bootstrap`。卸载只移除 LaunchAgent 注册及其 plist；应用数据、已下载原片和云端备份均保留，由用户单独管理。

## 登录、锁屏和休眠

| 主机状态 | 设计预期 / 限制 |
| --- | --- |
| 用户已登录，主机醒着 | 在计划时间启动；后台权限及 Keychain 仍需实测 |
| 锁屏 | 不等于退出登录；非交互 Keychain 和网盘凭据读取需单独验收 |
| 重启但未登录 | 用户级 LaunchAgent 尚未加载，不能保证备份 |
| 睡眠 | 不主动唤醒 Mac；日历任务可能在唤醒后合并执行一次，需实测，不能当作准时保证 |
| 关机或离线多天 | 不保证逐次补跑；后续一次运行按回看窗口发现，已知待办继续恢复 |

Mac “24 小时在线”仍需区分机器开机与允许自动睡眠。本项目不修改系统电源策略、不启用自动登录，也不申请管理员权限。部署者根据自己的使用方式配置主机，测试休眠和重启后的实际行为。

## 日志与权限

只输出脱敏摘要到 stdout/stderr。模板中的两个文件由 launchd 追加写入，应用实现时须对它们和应用日志一起轮转，设置文件数量及总大小上限；仅轮转 Python 自身日志不足以控制这些文件。

launchd 的运行环境和交互式终端不同，不能依赖 `.zshrc`、Homebrew 的隐式 PATH 或当前工作目录。凭据不可放进 plist、环境变量或进程参数。Keychain 拒绝访问时退出并记录可定位错误，不弹出无限等待的交互登录。
