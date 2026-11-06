// 执行原生 GetVariable/Invoke 包装验证参数、虚表槽与引用释放；不替代真实 Scaleform 弹窗验收。
using System;
using System.IO;
using System.Runtime.InteropServices;
class MatchLockFixture {
    [DllImport("kernel32.dll")] static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocation, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Entry();
    static void Require(bool condition) { if(!condition) throw new Exception("lock ABI assertion failed"); }
    static void Allocate(int address,int size) { Require(VirtualAlloc(new IntPtr(address),new UIntPtr((uint)size),0x3000,0x40)==new IntPtr(address)); }
    static void Write(int address,byte[] bytes) { Marshal.Copy(bytes,0,new IntPtr(address),bytes.Length); }
    static void WriteInteger(int address,int value) { Marshal.WriteInt32(new IntPtr(address),value); }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static int Main(string[] arguments) {
        try {
            Allocate(0x20000000,0x10000); Allocate(0x21000000,0x1000); Allocate(0x28000000,0x10000);
            Write(0x20000a00,File.ReadAllBytes(arguments[0]));
            Write(0x28000010,new byte[]{0xc3});
            Write(0x28000ac0,File.ReadAllBytes(arguments[1]));
            Write(0x28000490,File.ReadAllBytes(arguments[2]));
            Write(0x28001bc0,new byte[]{0x31,0xc0,0xc3});
            Write(0x28001c80,new byte[]{0xc3});
            WriteInteger(0x21000000,0x21000100);
            WriteInteger(0x2100010c,0x21000140);
            WriteInteger(0x21000140,0x21000200);
            WriteInteger(0x210002c8,0x28002000);
            // Movie::GetVariable(value*, path)，thiscall ret8；返回带引用的普通 AS 对象。
            Write(0x28002000,new byte[]{0x89,0x0d,0x1c,0,0,0x21,0x8b,0x44,0x24,4,
                0xc7,0x40,8,0,3,0,0x21,0xc7,0x40,12,0x46,0,0,0,
                0xc7,0x40,16,0,4,0,0x21,
                0x8b,0x54,0x24,8,0x89,0x15,4,0,0,0x21,0xb0,1,0xc2,8,0});
            WriteInteger(0x21000300,0x21000340);
            WriteInteger(0x21000358,0x28003000);
            WriteInteger(0x21000348,0x28003100);
            // ObjectInterface::Invoke(object, result, method, arguments, count, isDisplayObject)。
            Write(0x28003000,new byte[]{
                0x8b,0x44,0x24,4,0xa3,0x20,0,0,0x21,
                0x8b,0x44,0x24,8,0xa3,0x24,0,0,0x21,
                0x8b,0x44,0x24,12,0xa3,0x14,0,0,0x21,
                0x8b,0x44,0x24,16,0xa3,0x0c,0,0,0x21,
                0x8b,0x44,0x24,20,0xa3,0x10,0,0,0x21,
                0x8b,0x44,0x24,24,0x25,0xff,0,0,0,0xa3,0x28,0,0,0x21,
                0x89,0x0d,0x30,0,0,0x21,0xff,5,0x18,0,0,0x21,0xb0,1,0xc2,24,0});
            // 原生 Invoke 包装应且仅应释放一次它收到的 Value 引用。
            Write(0x28003100,new byte[]{0x8b,0x44,0x24,4,0x8b,0x40,12,0xa3,8,0,0,0x21,
                0x8b,0x44,0x24,8,0xa3,0x38,0,0,0x21,0xff,5,0x2c,0,0,0x21,0xc2,8,0});
            Entry entry=(Entry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20000a00),typeof(Entry));
            for(int iteration=0;iteration<100;iteration++) entry();
            Require(Read(0x21000004)==0xdd5c84 && Read(0x21000008)==0x46);
            Require(Read(0x2100000c)==0 && Read(0x21000010)==0);
            Require(Read(0x21000014)==0x20006000 && Read(0x21000018)==100);
            Require(Read(0x2100001c)==0x21000140 && Read(0x21000020)==0x21000400);
            Require(Read(0x21000024)==0 && Read(0x21000028)==0);
            Require(Read(0x2100002c)==100 && Read(0x21000030)==0x21000300 && Read(0x21000038)==0x21000400);
            // 查找失败留下 undefined，原生包装应跳过调用且无需释放引用。
            Write(0x28002000,new byte[]{0x30,0xc0,0xc2,8,0});
            for(int iteration=0;iteration<100;iteration++) entry();
            Require(Read(0x21000018)==100 && Read(0x2100002c)==100);
            Console.WriteLine("original GFx wrappers, arguments, references and repeated-call stack passed"); return 0;
        } catch(Exception error) { Console.WriteLine(error); return 1; }
    }
}
