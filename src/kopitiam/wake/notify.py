"""Windows 通知：用 Windows PowerShell 5.1 自带的 WinRT 通知接口，不装别的东西。

通知的发送者显示为「kopitiam」：在 HKCU\\Software\\Classes\\AppUserModelId\\Kopitiam 登记一个名字
（Windows 给没有安装包的程序用的标准做法，不需要管理员权限）。没登记时退回用 PowerShell 的身份。
点通知打开 launch 链接（比如 codex://threads/<对话ID> 直接打开那个 Codex 对话）。
"""
from __future__ import annotations

import base64
import os
import subprocess
from xml.sax.saxutils import escape, quoteattr

APP_ID = "Kopitiam"
APP_KEY = rf"Software\Classes\AppUserModelId\{APP_ID}"
POWERSHELL_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

# 内容走环境变量，不用在命令行里转义
_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:KOPITIAM_TOAST_XML)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:KOPITIAM_TOAST_APP).Show($toast)
"""


def register() -> None:
    """登记通知发送者的名字（kopitiam setup codex 时调用）。"""
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_KEY) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "kopitiam")


def unregister() -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, APP_KEY)
    except OSError:
        pass


def _app_id() -> str:
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_KEY).Close()
        return APP_ID
    except OSError:
        return POWERSHELL_ID


def toast_xml(title: str, body: str, launch: str | None = None) -> str:
    head = f"<toast activationType=\"protocol\" launch={quoteattr(launch)}>" if launch else "<toast>"
    return (head + "<visual><binding template=\"ToastGeneric\">"
            f"<text>{escape(title)}</text><text>{escape(body)}</text></binding></visual></toast>")


def toast(title: str, body: str, launch: str | None = None) -> None:
    """弹一个通知；不等它弹完。不是 Windows 就什么都不做。"""
    if os.name != "nt":
        return
    env = os.environ | {"KOPITIAM_TOAST_XML": toast_xml(title, body, launch), "KOPITIAM_TOAST_APP": _app_id()}
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
