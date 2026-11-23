"""窗口控件只在主线程更新，网络和安装操作在后台执行。"""
import ctypes
import json
import msvcrt
from pathlib import Path
from queue import Empty, SimpleQueue
import subprocess
import sys
from threading import Thread
import tkinter as tk
from tkinter import messagebox, ttk

from codol_rebuild.updater import check_release, install_release, version_number

PUBLIC_URL = 'https://pub-aa6c360bfe0e4f8eba38db385971c090.r2.dev'


class Launcher:
    def __init__(self, window, directory):
        self.window = window
        self.directory = directory
        self.program = directory / 'CODOL.exe'
        self.release = None
        self.events = SimpleQueue()
        self.operation = None
        self.current = None
        try:
            self.current = json.loads((directory / 'installed-version.json').read_text('utf8'))['version']
            version_number(self.current)
        except (OSError, ValueError, KeyError, TypeError):
            self.current = None
        window.title('使命召唤 Online · 启动器')
        panel = ttk.Frame(window, padding=22)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Welcome CODOL', font=('Microsoft YaHei UI', 18)).pack(anchor='w')
        ttk.Label(panel, text=f'安装目录：{directory}', wraplength=520).pack(anchor='w', pady=8)
        self.versions = ttk.Label(panel)
        self.versions.pack(anchor='w')
        self.resources = ttk.Label(panel, wraplength=520)
        self.resources.pack(anchor='w', pady=6)
        self.status = ttk.Label(panel, wraplength=520)
        self.status.pack(anchor='w')
        self.progress = ttk.Progressbar(panel, maximum=100)
        self.progress.pack(fill='x', pady=10)
        buttons = ttk.Frame(panel)
        buttons.pack(anchor='e')
        self.check = ttk.Button(buttons, text='检查更新', command=self.check_update)
        self.install = ttk.Button(buttons, command=self.download)
        self.play = ttk.Button(buttons, text='开始游戏', command=self.start_game)
        for button in (self.check, self.install, self.play):
            button.pack(side='left', padx=4)
        window.protocol('WM_DELETE_WINDOW', self.close)
        self.check_update()
        window.after(80, self.poll)

    def resources_ready(self):
        packed = self.directory / 'resources/v1649370/packed'
        return ((packed / 'packed-index.json.gz').is_file() and
                (packed / 'IIPS/IFS2.dll').is_file() and (packed / 'IIPS/IIPSDownload').is_dir())

    def refresh(self):
        installed = self.program.is_file()
        latest = self.release['version'] if self.release else None
        newer = latest and (not installed or not self.current or version_number(latest) > version_number(self.current))
        local = (self.current or '未知') if installed else '未安装'
        self.versions.configure(text=f'本机：{local}     最新：{latest or "待检查"}')
        self.resources.configure(text='游戏资源已就绪' if self.resources_ready() else
                                 '游戏资源需单独准备，请将 resources 放入同级目录。')
        self.install.configure(text='安装更新' if installed else '下载最新版',
                               state='normal' if newer and not self.operation else 'disabled')
        self.check.configure(state='disabled' if self.operation else 'normal')
        self.play.configure(state='normal' if installed and self.operation != 'install' else 'disabled')

    def run_task(self, operation, action):
        self.operation = operation
        self.refresh()
        def worker():
            try:
                self.events.put(('done', action()))
            except Exception as error:
                self.events.put(('error', str(error)))
        Thread(target=worker, daemon=True).start()

    def check_update(self):
        self.status.configure(text='正在检查更新…')
        self.run_task('check', lambda: check_release(PUBLIC_URL))

    def download(self):
        self.status.configure(text='正在下载，完成后将校验并安装…')
        self.progress.configure(value=0)
        self.run_task('install', lambda: install_release(self.directory, PUBLIC_URL, self.release,
                         progress=lambda count: self.events.put(('progress', count))))

    def poll(self):
        while True:
            try:
                event, value = self.events.get_nowait()
            except Empty:
                break
            if event == 'progress':
                self.progress.configure(value=min(100, value * 100 / self.release['size']))
                continue
            operation, self.operation = self.operation, None
            if event == 'error':
                self.status.configure(text=f'操作失败：{value}')
            elif operation == 'install':
                self.current = self.release['version']
                self.status.configure(text='安装完成。')
            else:
                self.release = value
                commit_file = self.directory / 'release-commit.txt'
                if self.program.is_file() and not self.current and commit_file.is_file():
                    try:
                        if commit_file.read_text().strip() == value.get('commit'):
                            self.current = value['version']
                    except OSError:
                        pass
                self.status.configure(text='检查完成。')
            self.refresh()
        self.window.after(80, self.poll)

    def close(self):
        if self.operation == 'install':
            messagebox.showinfo('正在安装', '请等待安装结束后关闭启动器。', parent=self.window)
        else:
            self.window.destroy()

    def start_game(self):
        if not self.resources_ready():
            messagebox.showinfo('缺少游戏资源', f'请准备游戏原包后再开始。\n{self.directory / "resources/v1649370/packed"}',
                                parent=self.window)
            return
        try:
            subprocess.Popen([str(self.program)], cwd=self.directory)
        except OSError as error:
            messagebox.showerror('启动失败', str(error), parent=self.window)
            return
        self.window.destroy()


def main():
    directory = Path(sys.argv[0]).resolve().parent
    with (directory / '.launcher.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            ctypes.windll.user32.MessageBoxW(None, '另一个启动器正在运行。', 'CODOL 启动器', 0)
            return
        window = tk.Tk()
        Launcher(window, directory)
        window.mainloop()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), 'CODOL 启动器', 16)
