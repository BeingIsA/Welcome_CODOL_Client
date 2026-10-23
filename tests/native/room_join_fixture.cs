// 独立 x86 加入夹具：执行真实生成桩，模拟原生函数并核对 thiscall 清栈与通知。
using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;
class RoomJoinFixture {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint kind, uint protect);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Run();
    const int State=0x21000000, Room=0x22000000, Session=0x24000000;
    static void Map(int address, int size) {
        if(VirtualAlloc(new IntPtr(address), new UIntPtr((uint)size),0x3000,0x40).ToInt32()!=address)
            throw new Exception("allocation failed");
    }
    static void Bytes(int address, byte[] code) {Marshal.Copy(code,0,new IntPtr(address),code.Length);}
    static int Read(int address) {return Marshal.ReadInt32(new IntPtr(address));}
    static void Write(int address,int value) {Marshal.WriteInt32(new IntPtr(address),value);}
    static void Count(int address, int counter, int pop) {
        var code=new List<byte>(new byte[]{0xff,0x05});
        code.AddRange(BitConverter.GetBytes(State+counter));
        code.AddRange(pop==0 ? new byte[]{0xc3} : new byte[]{0xc2,(byte)pop,0});
        Bytes(address,code.ToArray());
    }
    static Run Function(int address) {return (Run)Marshal.GetDelegateForFunctionPointer(new IntPtr(address),typeof(Run));}
    static int Main(string[] args) {
        try {
            Map(0x28000000,0x10000);
            Map(0x20000000,0x10000); Map(State,0x10000); Map(Room,0x40000);
            Map(0x23000000,0x10000); Map(Session,0x10000);
            Bytes(0x20000000,File.ReadAllBytes(args[0]));
            Write(State+8,Room);
            Write(Room+4,Room+0x30000); Write(Room+0x30000,Room+0x30100);
            Write(Room+0x30100,Room+0x30200);
            Write(Room+0x30204,Room+0x30300); Write(Room+0x30248,Room+0x30400);
            // 加入监听器记录 bool/error 参数，同时验证入房通知先于设置通知。
            var listener=new List<byte>(new byte[]{0x8b,0x44,0x24,4,0xa3});
            listener.AddRange(BitConverter.GetBytes(State+0x114));
            listener.AddRange(new byte[]{0x8b,0x44,0x24,8,0xa3});
            listener.AddRange(BitConverter.GetBytes(State+0x118));
            listener.AddRange(new byte[]{0xff,0x05}); listener.AddRange(BitConverter.GetBytes(State+0x110));
            listener.AddRange(new byte[]{0xc2,8,0}); Bytes(Room+0x30300,listener.ToArray());
            Count(Room+0x30400,0x104,8); Count(0x28003770,0x100,0);
            Count(0x2800af10,0x10c,4); Count(0x28005b10,0x11c,4); Count(0x280005f0,0x108,4);
            Bytes(0x28007910,new byte[]{0xb8,0,0,0,0x24,0xc3});
            Run success=Function(int.Parse(args[1])), failure=Function(int.Parse(args[2]));
            success();
            if(Read(Room+0x1c)!=Session || Read(Session+0x25c8)!=Room || Read(State+12)!=Session)
                throw new Exception("session ownership mismatch");
            if(Read(State+0x100)!=19 || Read(State+0x104)!=1 || Read(State+0x108)!=1 || Read(State+0x10c)!=1
               || Read(State+0x110)!=1 || Read(State+0x114)!=1 || Read(State+0x118)!=0 || Read(State+0x11c)!=3)
                throw new Exception("success callback ABI mismatch");
            failure();
            if(Read(State+0x110)!=2 || Read(State+0x114)!=0 || Read(State+0x118)!=3)
                throw new Exception("failure callback ABI mismatch");
            Bytes(0x28007910,new byte[]{0x33,0xc0,0xc3}); success();
            if(Read(State+0x110)!=3 || Read(State+0x104)!=1 || Read(State+0x100)!=19)
                throw new Exception("allocation failure must not initialize or publish members");
            Console.WriteLine("native join success/failure/allocation failure ABI passed"); return 0;
        } catch(Exception error) {Console.WriteLine(error); return 1;}
    }
}
