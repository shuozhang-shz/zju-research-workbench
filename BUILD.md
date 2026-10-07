# 构建（打包桌面应用 exe）

## 前置

- Python 3.10+ 与依赖：`pip install pywebview pyinstaller pillow`（ pillow 仅生成图标用）
- 应用本体是 `workbench_app.py`（内置桥接服务 + WebView 窗口），无其他编译步骤

## 打包 exe

```bat
python _mkicon.py          # 可选：重新生成应用图标 wb.ico
pyinstaller --noconsole --onefile --clean --name "ZJU-Workbench" --icon wb.ico --collect-all webview workbench_app.py
```

产物在 `dist/ZJU-Workbench.exe`，把它放到工作台根目录（与 `index.html` 同级）即为桌面应用。
**注意**：exe 需与 `index.html`、`calendar-data.js`、`04-代码与数据\` 等文件放在一起运行。

## 制作 Windows 安装包

使用 [Inno Setup](https://jrsoftware.org/isinfo.php) 编译仓库中的 `installer.iss`：

```bat
iscc installer.iss
```

产物 `Output/科研工作台-Setup-<版本>.exe`，双击即可安装（含桌面快捷方式、开始菜单、卸载支持）。

## 打包时务必排除的用户数据

以下文件包含账号会话与个人数据，**绝不能进入仓库或安装包**（.gitignore 已覆盖）：

```
04-代码与数据/zju-bridge/config.json   # 账号与加密密码
04-代码与数据/zju-bridge/session.json  # 登录会话 Cookie
04-代码与数据/zju-bridge/state.json    # 工作台个人数据
04-代码与数据/zju-bridge/seen.json     # 通知已读状态
04-代码与数据/zju-bridge/cache.json    # 接口缓存
```
