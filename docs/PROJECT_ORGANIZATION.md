# 项目整理记录

整理日期：2026-11-07。

## 目录变化

| 原位置 | 新位置 |
| --- | --- |
| 根目录 `selftest_*.py`（75 个模块） | `tests/`，通过 `tests.selftest_*` 导入 |
| 原 `README.md` 的详细内容 | `docs/reference/功能详解.md` |
| 部署、使用速查、托盘和后端说明 | `docs/reference/` |
| `功能改进-*.md` | `docs/features/` |
| 可靠性检查、实机验收 | `docs/testing/` |
| 验收历史记录 | `docs/archive/` |
| `design/` | `docs/design/` |

根目录新增简明 README、贡献指南、编辑器格式约定和 Git 换行规则；`docs/` 新增快速开始、架构说明与索引。启动脚本和运行模块保持原路径，`selftest.py` 继续作为兼容入口。

## 配置与发布

示例配置使用独立工作目录，删除原作者的整盘目录授权与个人路径假设。真实 `config.json` 和 `BOT_MEMORY.md` 留在本机；仓库提供 `BOT_MEMORY.example.md`。忽略规则覆盖固定记忆、密钥、任务库、日志和开发缓存。

首次 GitHub 发布使用整理后的完整快照，包含整理前尚未提交的功能更新。旧提交历史含本机信息，因此保留在本地备份分支，不上传到公开仓库；整理前文件另存于本机 `.runtime/organization/before/`。

## 验证原则

迁移同步更新测试导入、仓库根路径、模拟子进程工作目录、钩子清单和文档链接。通过统一入口执行离线及 Windows 进程隔离检查。GitHub Actions 使用 Windows 执行离线回归。

本次整理不代表重新完成微信手机实机验收，也不把历史测试记录视为当前电脑的验证结果。

## 2026-11-08 补充整理

- `tools/verify.py` 的 offline 套件补登记 18 个此前未进入任何入口的测试模块（共 134 个用例），CI 现在会执行它们。其中 `tests.selftest_task_control` 的一个用例因任务前缀变化已过期，按原意修正为匹配任务正文结尾。
- 新增 `unregistered_test_modules()` 检查：`tests/selftest_*.py` 既未登记套件、也未被 `selftest.py` 导入时，`tests.selftest_verify` 失败。
- `.editorconfig` 为含中文界面的 WinForms 脚本声明 `utf-8-bom`，避免编辑器保存时去掉 BOM 导致 Windows PowerShell 5.1 解码错误；贡献指南同步写明实际编码规则。
- 运行模块当时仍保留在根目录（见下一节）。

## 2026-11-09 目录整理

根目录从一百多个文件收拢为几个文件夹：

| 原位置 | 新位置 |
| --- | --- |
| 根目录全部 Python 模块（67 个） | `app/` |
| 根目录全部 `.ps1`、`tray_theme.cs`、`independent_process.cs`、图标、`微信开关.bat` | `desktop/` |
| `selftest.py` | `tests/selftest.py` |

- 根目录只保留 `微信助手.bat`、说明文档、示例配置、`VERSION` 和 `requirements.txt`。
- 配置、固定记忆、日志、`.runtime/` 仍在项目根目录，已有数据不用迁移。Python 通过新的 `app/project_paths.py`，PowerShell 通过 `desktop/python_runtime.ps1` 中的 `$WeChatHome` 和 `$WeChatApp` 定位。
- 窗口脚本的 `-Root`、`-ConfigRoot` 默认值改为项目根目录；Python 模块统一从 `app/` 启动。
- 更新器的必需文件改为 `app/` 和 `desktop/` 下的路径，更新后从 `desktop\launch_tray.ps1` 重新打开。
- 旧的开机自启动快捷方式指向已不存在的根目录 `launch_tray.ps1`。双击一次 `微信助手.bat` 后，启动器会自动改指向新位置。
- 验证：Linux 上全部登记的 unittest 模块（800 个用例）只有 4 个已知的 Windows 专属失败，旧版自测 2091 项全部通过。Windows 实机和 Windows CI 尚未验证。
