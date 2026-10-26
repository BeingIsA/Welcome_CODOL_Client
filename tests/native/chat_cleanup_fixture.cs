// 独立 x86 夹具：执行镜像原有清理分支，验证 GFx 释放、栈、寄存器及安全 cookie。
using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;

class ChatCleanupFixture {
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocationType, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.StdCall)] delegate int ReleaseValue(int instance, int payload);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int CheckCookie(int cookie);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Invoke();
    const int Runner = 0x20000000, Cleanup = 0x20001000, State = 0x21000000;
    const int Interface = 0x28001000, Vtable = 0x28001100, Release = 0x28001200;
    const int CookieCheck = 0x28001300, Cookie = 0x12345678, Payload = 0x13579bdf;
    static List<Delegate> callbacks = new List<Delegate>();
    static int releases, cookies;
    static void Map(int address) {
        if (VirtualAlloc(new IntPtr(address), new UIntPtr(65536), 0x3000, 0x40).ToInt32() != address)
            throw new Exception("allocation failed");
    }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static void Write(int address, int value) { Marshal.WriteInt32(new IntPtr(address), value); }
    static void Bytes(int address, byte[] contents) { Marshal.Copy(contents, 0, new IntPtr(address), contents.Length); }
    static void Emit(List<byte> instructions, params byte[] contents) { instructions.AddRange(contents); }
    static void Word(List<byte> instructions, int number) { instructions.AddRange(BitConverter.GetBytes(number)); }
    static void Call(List<byte> instructions, int entry, int target) {
        Emit(instructions, 0xe8); Word(instructions, target - entry - instructions.Count - 4);
    }
    static int ReleaseObject(int instance, int payload) {
        if (instance != Read(State + 24) || payload != Payload)
            throw new Exception("wrong GFx cleanup arguments");
        releases++;
        return 1;
    }
    static int VerifyCookie(int cookie) {
        if (cookie != Cookie) throw new Exception("stack cookie mismatch");
        cookies++;
        return 1;
    }
    static void InstallCallbacks() {
        ReleaseValue releaseCallback = new ReleaseValue(ReleaseObject);
        CheckCookie cookieCallback = new CheckCookie(VerifyCookie);
        callbacks.Add(releaseCallback); callbacks.Add(cookieCallback);
        List<byte> instructions = new List<byte>();
        Emit(instructions, 0xb8); Word(instructions, Marshal.GetFunctionPointerForDelegate(releaseCallback).ToInt32());
        Emit(instructions, 0xff, 0xe0); Bytes(Release, instructions.ToArray());
        instructions.Clear();
        Emit(instructions, 0x51, 0xb8); Word(instructions, Marshal.GetFunctionPointerForDelegate(cookieCallback).ToInt32());
        Emit(instructions, 0xff, 0xd0, 0x83, 0xc4, 4, 0xc3); Bytes(CookieCheck, instructions.ToArray());
        Write(Interface, Vtable); Write(Vtable + 8, Release);
    }
    static void InstallRunner() {
        List<byte> instructions = new List<byte>();
        // 与 SendChatMessage 原始栈帧一致，唯一业务对象是已提取参数的 GFx 临时值。
        Emit(instructions, 0x55, 0x8b, 0xec, 0x83, 0xe4, 0xf8, 0x81, 0xec); Word(instructions, 0xdec);
        Emit(instructions, 0xb8); Word(instructions, Cookie);
        Emit(instructions, 0x33, 0xc4, 0x89, 0x84, 0x24); Word(instructions, 0xde8);
        Emit(instructions, 0x53, 0x56, 0x57, 0x8d, 0x44, 0x24, 0x28, 0xa3); Word(instructions, State + 24);
        Emit(instructions, 0xc7, 0x44, 0x24, 0x30); Word(instructions, Interface);
        Emit(instructions, 0x8b, 0x45, 8, 0x89, 0x44, 0x24, 0x34);
        Emit(instructions, 0xc7, 0x44, 0x24, 0x38); Word(instructions, Payload);
        Emit(instructions, 0xe9); Word(instructions, Cleanup - Runner - instructions.Count - 4);
        Bytes(Runner, instructions.ToArray());
    }
    static void RunCase(int kind, int expectedReleases) {
        const int Entry = 0x20003000;
        List<byte> instructions = new List<byte>();
        Emit(instructions, 0x55, 0x8b, 0xec, 0x53, 0x56, 0x57);
        Emit(instructions, 0xbb); Word(instructions, 0x11223344);
        Emit(instructions, 0xbe); Word(instructions, 0x22334455);
        Emit(instructions, 0xbf); Word(instructions, 0x33445566);
        Emit(instructions, 0x89, 0x25); Word(instructions, State);
        Emit(instructions, 0x68); Word(instructions, kind);
        Call(instructions, Entry, Runner);
        Emit(instructions, 0x89, 0x25); Word(instructions, State + 4);
        Emit(instructions, 0x89, 0x1d); Word(instructions, State + 8);
        Emit(instructions, 0x89, 0x35); Word(instructions, State + 12);
        Emit(instructions, 0x89, 0x3d); Word(instructions, State + 16);
        Emit(instructions, 0x5f, 0x5e, 0x5b, 0x8b, 0xe5, 0x5d, 0xc3);
        Bytes(Entry, instructions.ToArray());
        releases = 0; cookies = 0;
        Invoke invoke = (Invoke)Marshal.GetDelegateForFunctionPointer(new IntPtr(Entry), typeof(Invoke));
        invoke();
        if (Read(State) != Read(State + 4) || Read(State + 8) != 0x11223344
            || Read(State + 12) != 0x22334455 || Read(State + 16) != 0x33445566)
            throw new Exception("native cleanup did not preserve caller stack/registers");
        if (releases != expectedReleases || cookies != 1)
            throw new Exception("wrong GFx release or cookie check count");
    }
    static int Main(string[] arguments) {
        try {
            Map(Runner); Map(State); Map(0x28000000);
            Bytes(Cleanup, File.ReadAllBytes(arguments[0]));
            Bytes(0x20002000, File.ReadAllBytes(arguments[1]));
            InstallCallbacks(); InstallRunner();
            RunCase(0x46, 1); RunCase(6, 0); RunCase(0x46, 1);
            GC.KeepAlive(callbacks);
            Console.WriteLine("native chat cleanup: managed/unmanaged GFx, repeated release, stack, registers, cookie passed");
            return 0;
        } catch (Exception error) { Console.WriteLine(error); return 1; }
    }
}
