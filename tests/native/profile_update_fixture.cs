// 独立 x86 夹具：验证档案采集桩的寄存器/栈恢复，以及房外原生虚调用 ABI。
using System;
using System.IO;
using System.Runtime.InteropServices;

class ProfileUpdateFixture {
    [DllImport("kernel32.dll")]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocation, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void Entry();

    static void Require(bool condition) {
        if (!condition) throw new Exception("profile update ABI assertion failed");
    }
    static void Write(int address, byte[] instructionBytes) {
        Marshal.Copy(instructionBytes, 0, new IntPtr(address), instructionBytes.Length);
    }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static void Allocate(int address) {
        Require(VirtualAlloc(new IntPtr(address), new UIntPtr(0x10000), 0x3000, 0x40) == new IntPtr(address));
    }
    static int Main(string[] arguments) {
        try {
            Allocate(0x20000000);
            Allocate(0x21000000);
            Allocate(0x28000000);
            Write(0x20000000, File.ReadAllBytes(arguments[0]));
            Write(0x20002000, File.ReadAllBytes(arguments[1]));
            Write(0x20004000, File.ReadAllBytes(arguments[2]));
            // 采集器替身刻意破坏易失寄存器和标志，累计采集次数。
            Write(0x20001000, new byte[]{0xff,5,0,1,0,0x21,0x31,0xc0,0x31,0xc9,0x31,0xd2,0xc3});
            // 原生尾部 push edi; call eax; ret；被调方法 ret 4 清除参数。
            Write(0x2800657e, new byte[]{0x57,0xff,0xd0,0xc3});
            Write(0x20003000, new byte[]{0x8b,0x44,0x24,4,0xa3,4,1,0,0x21,
                                       0xff,5,8,1,0,0x21,0xc2,4,0});
            Marshal.WriteInt32(new IntPtr(0x21000200), 0x21000300);
            Marshal.WriteInt32(new IntPtr(0x21000424), 0x20003000);
            Entry capture = (Entry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20002000), typeof(Entry));
            Entry fallback = (Entry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20004000), typeof(Entry));
            for (int iteration = 0; iteration < 100; iteration++) {
                capture();
                Require(Read(0x21000000) == Read(0x21000004));
                Require(Read(0x21000008) == 0x12345678);
                Require(Read(0x2100000c) == 0x21000200);
                Require(Read(0x21000010) == 0x34567812);
                Require(Read(0x21000014) == 0x45678123);
                Require(Read(0x21000018) == 0x56781234);
                Require(Read(0x2100001c) == 0x67812345);
                Require(Read(0x21000020) == 0x78123456);
                Require((Read(0x21000024) & 1) == 1);
                fallback();
                Require(Read(0x21000000) == Read(0x21000004));
                Require(Read(0x21000104) == 0x78123456);
            }
            Require(Read(0x21000100) == 100 && Read(0x21000108) == 100);
            Console.WriteLine("100 capture/fallback cycles preserved stack and capture registers");
            return 0;
        } catch (Exception error) { Console.WriteLine(error); return 1; }
    }
}
