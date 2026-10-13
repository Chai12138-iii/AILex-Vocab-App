# -*- coding: utf-8 -*-
"""
run.py —— 桌面版（EXE）打包专用入口
====================================================================
职责分工：
  - python app.py            ：开发调试模式，Flask 自带开发服务器 + 浏览器访问（原行为不变）
  - python run.py            ：桌面窗口模式，waitress 生产服务器 + pywebview 原生窗口
  - PyInstaller 打包         ：以 run.py 为程序入口（见下方打包命令）

整体流程：
  1. 初始化数据库（init_database 建表、补齐 4 本 AI 分类词书、平滑迁移）
  2. 自动探测一个空闲端口（避免 5000 被占用时启动失败）
  3. 在【后台守护线程】里用 waitress 启动生产级 WSGI 服务器
  4. 等服务真正可连接后，用 pywebview 打开一个原生桌面窗口加载首页
  5. 用户关闭窗口 -> webview.start() 返回 -> 主进程结束 ->
     daemon=True 的服务器线程随之自动退出（安全收尾，不留僵尸进程）

PyInstaller 打包命令（在 third-optimized 目录下执行）：
  pyinstaller --onefile --name AILex ^
      --add-data "templates;templates" ^
      --add-data "static;static" ^
      --add-data "..\4 六级-乱序.txt;." ^
      run.py
  说明：
    - templates / static 是只读资源，打进包里由 app.py 的 RESOURCE_DIR（_MEIPASS）读取；
    - "..\\4 六级-乱序.txt;." 把上级目录的默认词库源文件放进包根目录，
      这样用户即使没有旧数据库，首次启动也能自动导入默认词书；
    - cet6.db / config.txt 等数据文件【不用】打包，运行时会自动在 EXE
      同目录生成（app.py 的 DATA_DIR 逻辑）。
====================================================================
"""

import socket
import threading
import time

import webview                      # pywebview：把网页包装成原生桌面窗口
from waitress import serve          # waitress：生产级 WSGI 服务器（替代 Flask 开发服务器）

import app as appmod                # 导入 Flask 应用实例 app 和 init_database()


def find_free_port():
    """探测一个当前可用的空闲端口。

    做法：向操作系统申请"临时绑定 0 端口"（系统会随机分配一个空闲端口），
    拿到端口号后立刻关闭连接把它让出来给 waitress 用。
    极端情况下存在被其他程序抢先占用的可能，所以失败时回退到 5000。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]
        except OSError:
            return 5000             # 理论上极少发生：兜底用约定端口


def wait_server_ready(port, timeout=10):
    """阻塞等待 waitress 服务器真正开始接受连接。

    不等待就开窗口的话，窗口可能先于服务器就绪，用户会看到加载失败页。
    这里用"尝试 TCP 连接"的方式轮询，连上即认为服务就绪。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)         # 还没就绪，稍等再试
    return False


def main():
    # ---- 第 1 步：初始化数据库（建表 / 补齐 AI 分类词书 / 旧数据迁移）----
    # 注意：init_database() 在 app.py 里只写在 __main__ 中，import app 并不会执行，
    # 所以桌面版入口必须在这里显式调用一次，保证首次运行就能正常建库
    appmod.init_database()

    # ---- 第 2 步：选端口，拼出首页地址 ----
    port = find_free_port()
    url = f'http://127.0.0.1:{port}'

    # ---- 第 3 步：后台守护线程启动 waitress ----
    # daemon=True 是"窗口关闭时服务器安全退出"的关键：
    # webview.start() 返回后主线程继续往下走、main() 结束、主进程退出，
    # 此时守护线程会被 Python 直接终结，不会留一个占着端口的僵尸进程
    server_thread = threading.Thread(
        target=serve,
        args=(appmod.app,),                          # 要跑的 Flask 应用
        kwargs={'host': '127.0.0.1', 'port': port},  # 只监听本机回环，外部无法访问
        daemon=True,
    )
    server_thread.start()

    # ---- 第 4 步：等服务就绪后打开桌面窗口 ----
    if not wait_server_ready(port):
        # 10 秒内服务都没起来（理论上不会发生），弹窗提示而不是白屏
        print('服务器启动失败，请重新运行程序')
        return

    webview.create_window(
        'AILex 词汇学习',            # 窗口标题
        url,                         # 加载本机 waitress 提供的页面
        width=1000,
        height=760,
        min_size=(760, 560),         # 小于这个尺寸布局会挤坏
    )
    # start() 会阻塞，直到用户关闭窗口
    webview.start()

    # ---- 第 5 步：窗口已关闭，收尾 ----
    # 服务器线程是 daemon，主进程到这里结束时会自动带走它
    print('窗口已关闭，程序退出。')


if __name__ == '__main__':
    main()
