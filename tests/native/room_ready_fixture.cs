// 准备适配 x86 执行验证：真实 GFx 返回值尾部，准备监听器用参数记录桩。
using System;
using System.IO;
using System.Runtime.InteropServices;

class RoomReadyFixture {
    [DllImport("kernel32.dll")]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocation, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    delegate void GfxEntry(IntPtr parameters);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void NotifyEntry();

    static void Require(bool condition) {
        if (!condition) throw new Exception("ready ABI assertion failed");
    }
    static void Allocate(int address, int size) {
        Require(VirtualAlloc(new IntPtr(address), new UIntPtr((uint)size), 0x3000, 0x40) == new IntPtr(address));
    }
    static void Write(int address, byte[] instructionBytes) {
        Marshal.Copy(instructionBytes, 0, new IntPtr(address), instructionBytes.Length);
    }
    static void Word(int address, int value) { Marshal.WriteInt32(new IntPtr(address), value); }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }

    static int Main(string[] arguments) {
        try {
            Allocate(0x20000000, 0x5000);
            Allocate(0x21000000, 0x1000);
            Allocate(0x22000000, 0x20000);
            Allocate(0x28000000, 0x10000);
            Write(0x20000000, File.ReadAllBytes(arguments[0]));
            Write(0x20001000, File.ReadAllBytes(arguments[1]));
            Write(0x20002000, File.ReadAllBytes(arguments[2]));
            Write(0x2800f895, File.ReadAllBytes(arguments[3]));
            // 模拟暂停点写入保存的 EAX：调用前 AL 对应确认的状态，INT3 已替换 NOP。
            byte[] wrapper = new byte[]{0xb8,0,0,0,0,0xff,0x74,0x24,0x04,0xe8,0,0,0,0,0xc2,4,0};
            Array.Copy(BitConverter.GetBytes(0x20001000-0x20003000-14), 0, wrapper, 10, 4);
            Write(0x20003000, wrapper);
            Word(0x21000000, 0x21000100);
            GfxEntry getter = (GfxEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20003000), typeof(GfxEntry));
            GfxEntry setter = (GfxEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20000000), typeof(GfxEntry));
            for (int iteration = 0; iteration < 100; iteration++) {
                int ready = iteration % 2;
                Word(0x20003001, ready);
                getter(new IntPtr(0x21000000));
                Require(Read(0x2100010c) == 2);
                Require(Marshal.ReadByte(new IntPtr(0x21000110)) == ready);
                setter(new IntPtr(0x21000000));
            }
            // 原生监听器 ABI：栈上成员指针，ret 4；按顺序记录成员 +0x90。
            Write(0x2800b4d0, new byte[]{0x8b,0x44,0x24,4,0x0f,0xb6,0x80,0x90,0,0,0,
                0x8b,0x15,0,8,0,0x21,0x89,4,0x95,0x10,8,0,0x21,
                0xff,5,0,8,0,0x21,0xc2,4,0});
            Word(0x21000000, 0x22000000);
            Word(0x220105d8, 2);
            Marshal.WriteByte(new IntPtr(0x2200e3f8), 0);
            Marshal.WriteByte(new IntPtr(0x2200e5c8), 1);
            NotifyEntry notify = (NotifyEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20002000), typeof(NotifyEntry));
            notify();
            Require(Read(0x21000800) == 2 && Read(0x21000810) == 0 && Read(0x21000814) == 1);
            Word(0x220105d8, 0);
            notify();
            Require(Read(0x21000800) == 2);
            Console.WriteLine("ready getter/setter stack and member notification order passed");
            return 0;
        } catch (Exception error) { Console.WriteLine(error); return 1; }
    }
}
