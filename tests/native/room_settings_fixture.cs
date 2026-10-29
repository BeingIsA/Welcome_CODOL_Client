// 独立x86执行两个UI设置调用边，验证配置指针、this指针及ret 4后的栈平衡。
using System;
using System.IO;
using System.Runtime.InteropServices;
class RoomSettingsFixture {
    [DllImport("kernel32.dll")]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocation, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void SettingsEntry();
    static void Require(bool condition) {
        if (!condition) throw new Exception("settings call edge ABI assertion failed");
    }
    static void Allocate(int address, int size) {
        Require(VirtualAlloc(new IntPtr(address), new UIntPtr((uint)size), 0x3000, 0x40) == new IntPtr(address));
    }
    static void Write(int address, byte[] bytes) { Marshal.Copy(bytes, 0, new IntPtr(address), bytes.Length); }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static int Main(string[] arguments) {
        try {
            Allocate(0x20000000, 0x4000);
            Allocate(0x21000000, 0x1000);
            Allocate(0x28000000, 0x10000);
            Write(0x20000000, File.ReadAllBytes(arguments[0]));
            Write(0x2800fe34, File.ReadAllBytes(arguments[1]));
            Write(0x2800fefc, File.ReadAllBytes(arguments[2]));
            // 接收原生thiscall参数并使用ret 4清理配置指针。
            Write(0x20002000, new byte[]{0x8b,0x54,0x24,4,0x89,0x15,8,0,0,0x21,
                0x89,0x0d,12,0,0,0x21,0xc2,4,0});
            byte[] continuation = new byte[]{0x89,0x25,16,0,0,0x21,0x83,0xc4,0x40,0x5b,0xc3};
            Write(0x2800fe3b, continuation);
            Write(0x2800ff03, continuation);
            for (int edgeIndex = 0; edgeIndex < 2; edgeIndex++) {
                // 保存调用前ESP和预期配置地址；以JMP进入实际补丁窗口。
                byte[] wrapper = new byte[]{0x53,0x83,0xec,0x40,0x89,0xe0,0xa3,0,0,0,0x21,
                    0x8d,0x54,0x24,0,0x89,0x15,4,0,0,0x21,0xb9,0x78,0x56,0x34,0x12,
                    0xb8,0,0x20,0,0x20,0xe9,0,0,0,0};
                wrapper[14] = (byte)(edgeIndex == 0 ? 0x30 : 8);
                int callSite = edgeIndex == 0 ? 0x2800fe34 : 0x2800fefc;
                Array.Copy(BitConverter.GetBytes(callSite - 0x20001000 - wrapper.Length), 0, wrapper, wrapper.Length-4, 4);
                Write(0x20001000, wrapper);
                SettingsEntry entry = (SettingsEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20001000), typeof(SettingsEntry));
                for (int iteration = 0; iteration < 100; iteration++) {
                    entry();
                    Require(Read(0x21000000) == Read(0x21000010));
                    Require(Read(0x21000004) == Read(0x21000008));
                    Require(Read(0x2100000c) == 0x12345678);
                }
            }
            Console.WriteLine("both UI settings call edges passed 100 executions each");
            return 0;
        } catch(Exception error) { Console.WriteLine(error); return 1; }
    }
}
