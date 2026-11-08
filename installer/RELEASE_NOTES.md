Windows 10/11（x64 或支持 x64 仿真的 ARM64）用户下载 **WeChatAssistant-Setup-x.y.z.exe**，双击按向导安装；自带 Python，无需管理员权限。首次启动按使用引导完成 Claude Code 安装、微信登录和绑定。

安装包未做代码签名。确认从本仓库下载且 SHA-256 与 SHA256SUMS.txt 一致后，如果 SmartScreen 提示，可选择“更多信息 → 仍要运行”。

升级：退出所有窗口后运行新版安装包覆盖安装，个人数据保留。卸载：在 Windows 设置的应用列表卸载；默认保留个人数据，也可选择删除。工作目录和收件目录始终保留。

WECHAT-BOT-x.y.z.zip 是源码与应用内更新包，不包含 Python。发布前 CI 会执行 Windows 回归、界面自检和实际安装／覆盖安装／卸载检查。
