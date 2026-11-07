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
