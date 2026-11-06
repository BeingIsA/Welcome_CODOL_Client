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
            Allocate(0x20000000, 0x7000);
            Allocate(0x21000000, 0x1000);
            Allocate(0x22000000, 0x20000);
            Allocate(0x28000000, 0x10000);
            Write(0x20000000, File.ReadAllBytes(arguments[0]));
            Write(0x20001000, File.ReadAllBytes(arguments[1]));
            Write(0x20002000, File.ReadAllBytes(arguments[2]));
            Write(0x20004000, File.ReadAllBytes(arguments[4]));
            Write(0x20005000, File.ReadAllBytes(arguments[5]));
            Write(0x20006000, File.ReadAllBytes(arguments[6]));
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
            Require(Read(0x21000800) == 1 && Read(0x21000810) == 0);
            Word(0x220105d8, 0);
            notify();
            Require(Read(0x21000800) == 1);
            // 序列化和接收替身遵守真实清栈约定，记录接收 metadata 和准备值。
            Write(0x28004220, new byte[]{0xb8,0,6,0,0x21,0xc3});
            Write(0x280078e0, new byte[]{0x8b,0xc1,0xc2,8,0});
            Write(0x28009000, new byte[]{0xc2,4,0});
            Word(0x21000a00, 0x28009000);
            Write(0x280087d0, new byte[]{0xc2,4,0});
            Write(0x28004870, new byte[]{
                0x8b,0x15,0,8,0,0x21,0xc1,0xe2,4,
                0x8b,0x44,0x24,4,0x89,0x82,0x10,8,0,0x21,
                0x8b,0x44,0x24,0x10,0x89,0x82,0x14,8,0,0x21,
                0x8b,0x44,0x24,0x14,0x89,0x82,0x18,8,0,0x21,
                0xa1,8,4,0,0x21,0x89,0x82,0x1c,8,0,0x21,
                0xff,5,0,8,0,0x21,0xc2,0x18,0});
            Word(0x21000800, 0);
            Word(0x21000004, 2);
            Word(0x21000008, 11); Word(0x2100000c, 22); Word(0x21000010, 0);
            Word(0x21000014, 33); Word(0x21000018, 44); Word(0x2100001c, 1);
            NotifyEntry receive = (NotifyEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20004000), typeof(NotifyEntry));
            receive();
            Require(Read(0x21000800) == 2 && Read(0x21000004) == 0);
            Require(Read(0x21000810) == 0x22000000 && Read(0x21000814) == 11
                && Read(0x21000818) == 22 && Read(0x2100081c) == 0);
            Require(Read(0x21000820) == 0x22000000 && Read(0x21000824) == 33
                && Read(0x21000828) == 44 && Read(0x2100082c) == 1);
            receive();
            Require(Read(0x21000800) == 2);
            // 仅离线适配且会话仍有效时才调用原生判断；原生函数接收 EDI=room。
            Write(0x28002a70, new byte[]{0x89,0x3d,0x1c,0x0b,0,0x21,
                0xff,5,0x18,0x0b,0,0x21,0xc3});
            Write(0x28009010, new byte[]{0xc3});
            Word(0x21000b00, 0x22000000); Word(0x21000b04, 0x22018000);
            Word(0x21000b10, 0x22000000); Word(0x21000b14, 0x21000b40);
            Word(0x2200001c, 0x22018000); Word(0x22018000, 1); Word(0x2201a5c8, 0x22000000);
            Word(0x21000d00, 1);
            NotifyEntry evaluate = (NotifyEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20005000), typeof(NotifyEntry));
            NotifyEntry tick = (NotifyEntry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20006000), typeof(NotifyEntry));
            for (int iteration = 0; iteration < 5; iteration++) tick();
            Require(Read(0x21000b18) == 5 && Read(0x21000b1c) == 0x22000000);
            Word(0x21000d00, 0); tick(); Require(Read(0x21000b18) == 5);
            int[] invalidAddresses = {0x21000b4c, 0x21000b10, 0x21000b04, 0x2200001c, 0x22018000, 0x2201a5c8};
            foreach (int address in invalidAddresses) {
                int saved = Read(address);
                Word(address, address == 0x21000b4c ? 1 : 0);
                evaluate(); Require(Read(0x21000b18) == 5);
                Word(address, saved);
            }
            evaluate(); Require(Read(0x21000b18) == 6);
            Console.WriteLine("ready GFx, failure completion and receive ABI passed");
            return 0;
        } catch (Exception error) { Console.WriteLine(error); return 1; }
    }
}
