"""固定版本启动观察断点：由运行入口与独立诊断共享。

Fixed-build startup observation points shared by launcher and diagnostics.
"""
BREAKPOINTS = (
    '0x4fd9b0:Com Init',
    '0x4fcc00:Initial fastfiles',
    '0x7ef0d0:Frontend tick',
    '0x77f8b8:Engine init returned',
    '0x4706d0:Switch to main menu',
    '0x4f82e0:Com Error',
    '0x77c930:Fatal error',
)
