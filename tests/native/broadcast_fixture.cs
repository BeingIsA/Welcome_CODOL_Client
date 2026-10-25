// 独立 x86 夹具执行真实广播桩，GFx 与成功通知由断言桩替代；不启动游戏。
using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;

class BroadcastFixture {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocationType, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int One(int value);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Three(int first, int second, int third);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Five(int method, int output, int arguments, int count, int controller);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Run();
    static List<Delegate> callbacks = new List<Delegate>();
    static Dictionary<string, object> fields = new Dictionary<string, object>();
    static int confirmations, messages, allocations, expectedSpeakerType;
    const int State = 0x21000000;
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static void Write(int address, int value) { Marshal.WriteInt32(new IntPtr(address), value); }
    static void Bytes(int address, byte[] contents) { Marshal.Copy(contents, 0, new IntPtr(address), contents.Length); }
    static void Map(int address, int size) {
        if (VirtualAlloc(new IntPtr(address), new UIntPtr((uint)size), 0x3000, 0x40).ToInt32() != address)
            throw new Exception("allocation failed " + address.ToString("x"));
    }
    static string Text(int address) {
        List<byte> contents = new List<byte>();
        for (int offset = 0; offset < 65536; offset++) {
            byte character = Marshal.ReadByte(new IntPtr(address + offset));
            if (character == 0) return System.Text.Encoding.UTF8.GetString(contents.ToArray());
            contents.Add(character);
        }
        throw new Exception("unterminated text");
    }
    static void Callback(int address, Delegate callback, byte[] prefix, byte[] suffix) {
        callbacks.Add(callback);
        List<byte> code = new List<byte>(prefix);
        code.Add(0xb8);
        code.AddRange(BitConverter.GetBytes(Marshal.GetFunctionPointerForDelegate(callback).ToInt32()));
        code.AddRange(new byte[]{0xff, 0xd0});
        code.AddRange(suffix);
        Bytes(address, code.ToArray());
    }
    static int Create(int value) {
        if (allocations != 0) throw new Exception("previous object leaked");
        allocations++;
        fields.Clear();
        Write(value + 8, 1); Write(value + 12, 0x48); Write(value + 16, 1);
        return 1;
    }
    static int Set(int instance, int name, int value) {
        if (instance != State + 32 || allocations != 1) throw new Exception("invalid object");
        int kind = Read(value + 12), contents = Read(value + 16);
        fields[Text(name)] = kind == 6 ? (object)Text(contents) : contents;
        return 1;
    }
    static int Release(int value) {
        allocations--;
        Write(value + 8, 0); Write(value + 12, 0);
        return 1;
    }
    static int Complete(int success, int error, int speakerType) {
        if (success != 1 || error != 0 || speakerType != expectedSpeakerType) throw new Exception("wrong completion arguments");
        confirmations++;
        return 1;
    }
    static int Dispatch(int method, int output, int arguments, int count, int controller) {
        if (method != 0xdd601c || output != 0 || arguments != State + 32 || count != 1 || controller != 3)
            throw new Exception("wrong receive ABI");
        if ((int)fields["channelID"] != 0 || (int)fields["loudSpeakerType"] != expectedSpeakerType
            || (string)fields["senderID"] != "12345" || (string)fields["senderName"] != "中文玩家"
            || (string)fields["message"] != "一起玩吗？") throw new Exception("wrong message");
        messages++;
        return 1;
    }
    static int Main(string[] arguments) {
        try {
            expectedSpeakerType = int.Parse(arguments[3]);
            Map(0x20000000, 65536); Map(State, 65536); Map(0x28000000, 65536);
            Callback(0x2800fd70, new One(Create), new byte[]{0xff,0x74,0x24,4}, new byte[]{0x83,0xc4,4,0xc3});
            Callback(0x28006750, new Three(Set), new byte[]{0xff,0x74,0x24,8,0xff,0x74,0x24,8,0x51}, new byte[]{0x83,0xc4,12,0xc2,8,0});
            Callback(0x28006660, new One(Release), new byte[]{0x51}, new byte[]{0x83,0xc4,4,0xc3});
            Callback(0x28004ab0, new Three(Complete), new byte[]{0xff,0x74,0x24,12,0xff,0x74,0x24,12,0xff,0x74,0x24,12}, new byte[]{0x83,0xc4,12,0xc2,12,0});
            Callback(0x28000570, new Five(Dispatch), new byte[]{0x57,0xff,0x74,0x24,20,0xff,0x74,0x24,20,0xff,0x74,0x24,20,0xff,0x74,0x24,20}, new byte[]{0x83,0xc4,20,0xc3});
            Bytes(0x20000000, File.ReadAllBytes(arguments[0]));
            Run run = (Run)Marshal.GetDelegateForFunctionPointer(new IntPtr(int.Parse(arguments[1])), typeof(Run));
            run();
            Write(State + 12, expectedSpeakerType); Write(State, 1); run(); run();
            if (confirmations != 1 || messages != 0) throw new Exception("HTTP inserted a message");
            Bytes(State, File.ReadAllBytes(arguments[2])); run(); run();
            if (confirmations != 1 || messages != 1 || allocations != 0) throw new Exception("push not independent");
            Bytes(State, File.ReadAllBytes(arguments[2])); Write(State + 12, expectedSpeakerType); Write(State, 1); run(); run();
            if (confirmations != 2 || messages != 2 || allocations != 0) throw new Exception("duplicate notification");
            GC.KeepAlive(callbacks);
            Console.WriteLine("native x86 broadcast: completion, receive ABI, ownership, repeated ticks passed");
            return 0;
        } catch (Exception error) { Console.WriteLine(error); return 1; }
    }
}
