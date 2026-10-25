"""联机用例与通信适配共用的异常契约，不依赖具体业务模块。"""


class OnlineError(Exception):
    """可展示的联机错误；uncertain 表示写请求可能已在服务端执行。"""

    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain
