"""配置选择原生本地网络初始化；只修改已批准的两个调用实参字节。"""

# 先核对完整调用指令窗口，实际写入仅覆盖push指令的立即数。
CALL_WINDOWS = (
    (0x7E9911, bytes.fromhex('6a01e8a879e3ff')),
    (0x621ABE, bytes.fromhex('6a01893d209de500e8f5f7ffff')),
)


def native_arguments(bind_address, bind_port):
    """原生2916/2917负责构造本机地址对象；不直接写CommonAddr或成功状态。"""
    return ['+set', '2916', bind_address, '+set', '2917', str(bind_port)]


def install(process):
    """在CRT暂停点核对全部窗口后安装，保留原生调用及维护流程。"""
    for address, expected in CALL_WINDOWS:
        if process.read(address, len(expected)) != expected:
            raise ValueError(f'原生网络初始化调用不匹配：{address:#x}')
    for address, expected in CALL_WINDOWS:
        process.write(address + 1, b'\x00')
        if process.read(address, len(expected)) != expected[:1]+b'\x00'+expected[2:]:
            raise ValueError(f'原生网络初始化参数回读不匹配：{address:#x}')
    return {'mode': 'local', 'changed_bytes': [hex(address+1) for address, _ in CALL_WINDOWS]}
