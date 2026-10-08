# 项目长期记忆（WORK 工作区）

## 版本控制约定（用户明确要求，长期生效）

- **凡涉及代码/文件的操作，助手必须自动完成版本控制全流程**：改动后自动 `git add -A` + `git commit`（提交信息写清「做了什么 + 为什么」），提交后由 post-commit 钩子自动 push 到 origin。用户不需要额外下达提交或推送指令。
- 仓库：`C:\Users\zyy\Desktop\WORK`，分支 `main`，远端 origin = `git@github.com:zyy2539448313-byte/test.git`（SSH 免密）。
- 仓库级身份「张雨阳 <zyy2539448313@163.com>」；`autocrlf=false`、`longpaths=true`。
- 已装快捷别名：`git save "信息"`（add+commit，自动推送）、`git undo`（撤销上次提交保留改动）、`git sync`（pull --rebase + push）。
- 大改动（重构、新功能）先开分支；删除/回滚等危险操作先说明再执行。

## 环境约束（实测）

- 带界面 GUI 程序（如 Chrome）被沙箱拦截，无法启动；Chrome 无头模式可运行，但拒绝在默认 user-data-dir 开远程调试，因此无法复用浏览器登录态 —— 依赖登录态的网页自动化（Boss直聘/智联等）在本机不可行。
- 无 LibreOffice/pandoc/wkhtmltopdf，无法做 PDF 转换；HTML→Word 走 `~/.venv-html-to-docx`（Python 3.12）。
- 无 gh CLI；包管理器仅 winget。
