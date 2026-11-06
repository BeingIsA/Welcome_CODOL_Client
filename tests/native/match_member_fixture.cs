// 成员通知调用桩的 x86 执行回归；外部原生函数用严格清栈的记录桩替代。
using System;
using System.IO;
using System.Runtime.InteropServices;
class MatchMemberFixture {
    [DllImport("kernel32.dll")]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocation, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Entry();
    static void Require(bool value) { if (!value) throw new Exception("member notification ABI mismatch"); }
    static void Allocate(int address, int size) {
        Require(VirtualAlloc(new IntPtr(address), new UIntPtr((uint)size), 0x3000, 0x40) == new IntPtr(address));
    }
    static void Write(int address, byte[] data) { Marshal.Copy(data, 0, new IntPtr(address), data.Length); }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static int Main(string[] arguments) {
        try {
            Allocate(0x20000000, 0x10000); Allocate(0x22000000, 0x10000);
            Allocate(0x28000000, 0x10000); Allocate(0x29000000, 0x10000); Allocate(0x2a000000, 0x10000);
            Write(0x20000600, File.ReadAllBytes(arguments[0]));
            Write(0x29004220, new byte[]{0xb8,0,0,0,0x22,0xc3});
            Write(0x290078e0, new byte[]{0x8b,0xc1,0xc2,8,0});
            // 引用增加桩：返回实际持有引用数，stdcall 清理一个参数。
            Write(0x22001000, new byte[]{0x8b,0x44,0x24,4,0xff,0,0x8b,0,0xc2,4,0});
            Marshal.WriteInt32(new IntPtr(0x2a008128), 0x22001000);
            // 序列化桩保存 ECX 与唯一参数，验证 descriptor 和 buffer 引用地址。
            Write(0x28005be0, new byte[]{0x89,0x0d,0x20,0,0,0x22,0x8b,0x44,0x24,4,0xa3,0x24,0,0,0x22,0xc2,4,0});
            Write(0x22002000, new byte[]{0xc3});
            Marshal.GetDelegateForFunctionPointer<Entry>(new IntPtr(0x20000600))();
            Require(Read(0x22000004) == 1);
            Require(Read(0x22000020) == 0x20004000);
            Require(Read(0x22000024) == 0x2000400c);
            Require(Read(0x2000400c) == 0x22000000);
            Console.WriteLine("member notification x86 ABI passed"); return 0;
        } catch (Exception failure) { Console.Error.WriteLine(failure); return 1; }
    }
}
