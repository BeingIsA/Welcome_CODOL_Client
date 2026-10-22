"""1649370 宿主边界布局：集中管理运行编排使用的固定地址与解码常量。

必须在镜像指纹校验后使用；其他版本应提供独立适配器，不能复用这些地址。
"""

CONNECTING_BRANCH = 0x60c6f7  # 连接提示条件分支
CONNECTING_FALSE = 0x60c6fd  # 连接提示失败返回
DW_DVAR = 0x849b874  # 在线服务开关对象
DISPLAY_MODE = 0xe89bb0  # 显示模式对象
CLIENT_SLOT = 0x82c3b58  # 客户端对象槽位
CLIENT_POINTERS = 0x1c1d188  # 客户端编码指针表
CLIENT_POINTER_XOR = 0xfbb2c97a  # 客户端指针解码常量
CLIENT_ALLOCATED = 0x3381398  # 客户端对象分配标志
UI_RESOURCE_COUNT = 0x34f3538  # 界面资源数量
IMAGE_BASE = 0x400000  # 固定镜像基址
CRT_ENTRY = 0xc427db  # 运行库入口
WIN_MAIN = 0x77ee40  # WinMain入口
ENGINE_RETURN = 0x77f8b8  # 引擎初始化返回点
MAIN_MENU = 0x4706d0  # 进入主菜单
FRONTEND_TICK = 0x7ef0d0  # 前端帧入口
INVENTORY_COUNT = 0x82c4a8c  # 原生库存数量
CATALOG_READY = 0x84abb2f  # 物品目录就绪
CATALOG_COUNT = 0x7795840  # 物品目录数量
CATALOG_RECORDS = 0x779583c  # 物品目录记录指针
UI_ACTIVATED = 0x82e1344  # 原生界面一次性激活标志
ROOT_TYPE = 0xe69744  # 根界面对象类型
ROOT_INTERFACE = 0xe69740  # 根界面对象接口
ROOT_VALUE = 0xe69748  # 根界面对象值
STARTUP_ARGUMENTS = 0x81a76e8  # 引擎启动参数
COMMAND_QUEUE = 0x4f2ec0  # 命令入队入口
COMMAND_DESCRIPTOR = 0x76595a4  # 命令缓冲描述
COMMAND_BUFFER = 0x33821b0  # 命令缓冲基址
COMMAND_LOCK = 0x81a55a0  # 命令缓冲临界区
