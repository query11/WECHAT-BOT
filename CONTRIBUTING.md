# 开发与维护

## 本地准备

使用 Windows、Python 3.10+ 和 Git。先执行 `python -m pip install -r requirements.txt`。离线测试自建模拟服务和临时目录，不需要真实微信配置或模型额度。

Python 程序在 `app/`，PowerShell 脚本和图标在 `desktop/`，测试位于 `tests/`，统一验证入口是 `tools/verify.py`。从根目录执行命令；测试模块用 `tests.selftest_*` 包名导入，`tests/__init__.py` 会把 `app/` 加入导入路径。

## 验证入口

```powershell
python tools/verify.py --list
python tools/verify.py --suite quick
python tools/verify.py --suite offline
python tools/verify.py --suite integration
```

- `quick`：提交前的快速子集，包括连接、运行状态、凭据、后端及钩子守卫。
- `offline`：旧入口 `selftest.py`、快速子集和登记的专项测试，报告中分别计数。
- `integration`：仅 Windows，验证真实子进程与 Job Object 隔离，不启动真实微信业务或用户应用。

单模块检查示例：

```powershell
python -m unittest tests.selftest_task_journal tests.selftest_desktop_files
python tests\selftest.py
```

不要只运行默认 `unittest discover` 就认为全量通过：本项目同时有函数式检查和 unittest，用例清单由验证入口明确登记。新增 `tests/selftest_*.py` 必须加入合适的套件（或由 `tests/selftest.py` 导入），否则 `tests.selftest_verify` 会失败；`FAST_MODULES` 与 `tools/pre-commit.ps1` 的清单必须一致。

报告保存到 `.runtime/verification/<轮次>/`。检查条目数与 unittest 用例数分开统计；跳过、超时、无有效用例和失败必须如实报告。离线通过后，影响微信真实行为的改动仍按[实机验收](docs/testing/实机验收.md)核对。

## Git 钩子和 CI

可选安装本地快速检查钩子：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install_hooks.ps1
```

GitHub Actions 在 Windows 上运行离线回归，并保存验证报告。进程隔离套件在本机显式执行；CI 不连接真实微信账号或付费模型服务。

## 修改约定

- 延用现有模块职责，避免在界面脚本重复实现后台任务和权限规则。
- 新的 Python 模块放 `app/`，新的桌面脚本放 `desktop/`。用户数据一律放项目根目录：Python 用 `project_paths.ROOT`，PowerShell 用 `$WeChatHome`；不要再用脚本自身所在目录定位配置。
- 含中文界面文本的 WinForms 脚本（清单见 `.editorconfig`）使用带 BOM 的 UTF-8，其余 `.ps1` 保持纯 ASCII，兼容 Windows PowerShell 5.1；`tests/selftest_ops.py` 会检查。
- JSON 和 Markdown 使用 UTF-8；示例配置使用通用目录和空凭据。
- 公开文档新增内容放在 `docs/` 对应分类，并更新[文档索引](docs/README.md)。
- 不提交真实配置、API Key、固定记忆、聊天、截图、生成文件、日志或 `.runtime/`。
- 提交说明写清问题、修改后的行为和验证范围。失败与未测项目应与通过结果分开记录。

根目录 `.editorconfig` 与 `.gitattributes` 约定基础格式和换行。本项目以 [MIT 许可证](LICENSE) 发布；提交贡献即表示同意以相同许可证授权。
