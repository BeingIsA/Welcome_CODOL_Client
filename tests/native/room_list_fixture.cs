// 独立 x86 夹具：执行真实发布机器码，模拟 GFx API；不加载或启动游戏。
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;

class RoomListFixture {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint kind, uint protect);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int One(int p);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Three(int p, int name, int value);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Four(int method, int output, int args, int count);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Run();
    static List<Delegate> pinned = new List<Delegate>();
    static Dictionary<int, object> objects = new Dictionary<int, object>();
    static int serial, dispatched, created, released, setters, failAt;
    static int State = 0x21000000;
    static int Read(int p) { return Marshal.ReadInt32(new IntPtr(p)); }
    static void Write(int p, int v) { Marshal.WriteInt32(new IntPtr(p), v); }
    static void Bytes(int p, byte[] bytes) { Marshal.Copy(bytes, 0, new IntPtr(p), bytes.Length); }
    static string Text(int p) {
        List<byte> bytes = new List<byte>();
        for (int i=0; i<4096; i++) {
            byte b = Marshal.ReadByte(new IntPtr(p+i));
            if (b==0) return System.Text.Encoding.UTF8.GetString(bytes.ToArray());
            bytes.Add(b);
        }
        throw new Exception("unterminated text");
    }
    static void Map(int p, int n) {
        if (VirtualAlloc(new IntPtr(p), new UIntPtr((uint)n), 0x3000, 0x40).ToInt32()!=p)
            throw new Exception("allocation failed " + p.ToString("x"));
    }
    static void Callback(int address, Delegate callback, byte[] prefix, byte[] suffix) {
        pinned.Add(callback);
        List<byte> code = new List<byte>(prefix);
        code.Add(0xb8); code.AddRange(BitConverter.GetBytes(Marshal.GetFunctionPointerForDelegate(callback).ToInt32()));
        code.AddRange(new byte[]{0xff,0xd0}); code.AddRange(suffix);
        Bytes(address, code.ToArray());
    }
    static int Create(int value, bool array) {
        int id = ++serial;
        objects[id] = array ? (object)new List<Dictionary<string,object>>() : new Dictionary<string,object>();
        Write(value+8, 0x22000000); Write(value+12, array ? 0x49 : 0x48); Write(value+16, id);
        created++;
        return 1;
    }
    static int Release(int value) {
        if ((Read(value+12)&64)!=0) {
            if (!objects.Remove(Read(value+16))) throw new Exception("double release");
            released++;
        }
        Write(value+8, 0); Write(value+12, 0);
        return 1;
    }
    static int Set(int obj, int name, int value) {
        setters++;
        if (setters==failAt) return 0;
        int type = Read(value+12), data=Read(value+16);
        // 模拟 SetMember 持有子对象；临时值释放后父对象仍可访问其内容。
        object decoded = (type & 0x8f)==8 ? (object)new Dictionary<string,object>((Dictionary<string,object>)objects[data])
                         : type==6 ? (object)Text(data) : data;
        ((Dictionary<string,object>)objects[Read(obj+16)])[Text(name)] = decoded;
        return 1;
    }
    static int Push(int iface, int handle, int value) {
        if (iface!=0x22000000) throw new Exception("bad thiscall interface");
        var row = (Dictionary<string,object>)objects[Read(value+16)];
        ((List<Dictionary<string,object>>)objects[handle]).Add(new Dictionary<string,object>(row));
        return 1;
    }
    static int Dispatch(int method, int output, int args, int count) {
        if (method!=0xddcb84 || output!=0 || count!=1) throw new Exception("bad dispatch ABI");
        var rows = (List<Dictionary<string,object>>)objects[Read(args+16)];
        if (rows.Count!=Read(State+4)) throw new Exception("row count mismatch");
        for (int i=0; i<rows.Count; i++) {
            if ((string)rows[i]["name"]!="中文房主" || (int)rows[i]["index"]!=i)
                throw new Exception("wrong text/index");
            if ((string)rows[i]["password"]!="*" || (int)rows[i]["currentPlayers"]!=2)
                throw new Exception("wrong display fields");
            var mode = (Dictionary<string,object>)rows[i]["currentMatchMode"];
            if ((string)mode["id"] != (string)rows[i]["mode"])
                throw new Exception("missing or mismatched nested mode");
        }
        dispatched++;
        return 1;
    }
    static int Main(string[] args) {
        try {
            Map(0x28000000,0x10000); Map(0x20000000,0x10000);
            Map(State,0x200000); Map(0x22000000,0x10000);
            Write(0x22000000,0x22000100); Write(0x2200013c,0x22000200);
            // cdecl 函数转发原始实参；thiscall 包装器显式传递 ECX 并按原 ABI 清栈。
            Callback(0x2800fdd0, new One(delegate(int p){return Create(p,true);}),
                     new byte[]{0xff,0x74,0x24,4}, new byte[]{0x83,0xc4,4,0xc3});
            Callback(0x2800fd70, new One(delegate(int p){return Create(p,false);}),
                     new byte[]{0xff,0x74,0x24,4}, new byte[]{0x83,0xc4,4,0xc3});
            Callback(0x28006660, new One(Release), new byte[]{0x51}, new byte[]{0x83,0xc4,4,0xc3});
            byte[] three = {0xff,0x74,0x24,8,0xff,0x74,0x24,8,0x51};
            byte[] ret8 = {0x83,0xc4,12,0xc2,8,0};
            Callback(0x28006750,new Three(Set),three,ret8);
            Callback(0x22000200,new Three(Push),three,ret8);
            Callback(0x28000570,new Four(Dispatch),
                     new byte[]{0xff,0x74,0x24,16,0xff,0x74,0x24,16,0xff,0x74,0x24,16,0xff,0x74,0x24,16},
                     new byte[]{0x83,0xc4,16,0xc3});
            Bytes(0x20000000,File.ReadAllBytes(args[0]));
            Run run = (Run)Marshal.GetDelegateForFunctionPointer(new IntPtr(int.Parse(args[1])),typeof(Run));
            foreach (string file in new string[]{args[2], args[3], args[2]}) {
                Bytes(State,File.ReadAllBytes(file)); Write(State,1);
                int before=dispatched; run(); run();
                if (dispatched!=before+1 || objects.Count!=0 || Read(State+8)!=2)
                    throw new Exception("dispatch/cleanup/idempotence failed");
            }
            // 覆盖普通字段、子对象字段、父子挂接及第二行失败时的清理。
            foreach (int failure in new int[]{3,12,13,25,26}) {
                Bytes(State,File.ReadAllBytes(args[2])); Write(State,1); failAt=setters+failure;
                int saved=dispatched; run();
                if (dispatched!=saved || objects.Count!=0 || created!=released || Read(State+8)!=1)
                    throw new Exception("failed conversion leaked or published partial list");
            }
            GC.KeepAlive(pinned);
            Console.WriteLine("native x86 GFx ABI: rows, empty list, refresh, failure cleanup passed");
            return 0;
        } catch(Exception e) {Console.WriteLine(e.ToString()); return 1;}
    }
}
