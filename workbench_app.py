#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浙大科研工作台 —— 桌面应用入口
================================
双击（或打包后的 exe）即可使用：本程序同时完成
  1. 在本机启动浙大桥接服务（127.0.0.1，仅本机可访问）；
  2. 打开应用窗口（WebView2 内核，观感与普通桌面软件一致）。

关闭窗口 = 桥接服务一起退出，不留后台进程。
第一次使用：在「学在浙大」页登录浙大账号（可勾选记住密码，之后自动登录）。

源码运行：python workbench_app.py
打包 exe：pyinstaller --noconsole --onefile --icon wb.ico --collect-all webview workbench_app.py
"""
import os
import socket
import sys
import threading


def _app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ROOT = _app_root()
sys.path.insert(0, os.path.join(ROOT, '04-代码与数据', 'zju-bridge'))

import zju_bridge as zb   # noqa: E402


def find_port(start=8765):
    for p in range(start, start + 50):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            return p
        except OSError:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return start


def main():
    port = find_port()
    B = zb.Bridge()
    zb.Handler.bridge = B
    server = zb.QuietServer(('127.0.0.1', port), zb.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 后台预热：校历 + 校车（含经停站）
    threading.Thread(target=lambda: zb._safe(B.fetch_calendar), daemon=True).start()
    threading.Thread(target=lambda: zb._safe(B.fetch_bus), daemon=True).start()

    url = 'http://127.0.0.1:%d/' % port
    print('workbench running at %s (close the window to quit)' % url, flush=True)

    try:
        import webview
        webview.create_window('科研工作台', url,
                              width=1280, height=880, min_size=(980, 640))
        webview.start(private_mode=False)   # 关闭隐私模式：localStorage 跨启动持久化
    except Exception as e:
        # 无 GUI 环境或 WebView2 缺失：退回浏览器模式
        print('webview unavailable (%s); fallback to browser mode' % e, flush=True)
        try:
            os.startfile(url)
        except Exception:
            pass
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        print('bye.', flush=True)


if __name__ == '__main__':
    main()
