// 换队适配执行夹具：真实槽位函数，模拟成员通知和角色通知。
using System;
using System.IO;
using System.Runtime.InteropServices;
class RoomTeamFixture {
    [DllImport("kernel32.dll")] static extern IntPtr VirtualAlloc(IntPtr address, UIntPtr size, uint allocation, uint protection);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void Entry();
    static void Require(bool condition) { if (!condition) throw new Exception("native ABI assertion failed"); }
    static void Allocate(int address, int size) { Require(VirtualAlloc(new IntPtr(address), new UIntPtr((uint)size),0x3000,0x40)==new IntPtr(address)); }
    static void Write(int address, byte[] bytes) { Marshal.Copy(bytes,0,new IntPtr(address),bytes.Length); }
    static void Word(int address, int value) { Marshal.WriteInt32(new IntPtr(address),value); }
    static int Read(int address) { return Marshal.ReadInt32(new IntPtr(address)); }
    static byte Slot(int index) { return Marshal.ReadByte(new IntPtr(0x2200e418+index*0x1d0)); }
    static void Stub(int address,int counter,bool stackArgument) {
        byte[] bytes = stackArgument ? new byte[]{0xff,0x05,0,0,0,0,0xc2,4,0} : new byte[]{0xff,0x05,0,0,0,0,0xc3};
        Array.Copy(BitConverter.GetBytes(counter),0,bytes,2,4); Write(address,bytes);
    }
    static int Main(string[] arguments) {
        try {
            Allocate(0x20000000,0x1000);Allocate(0x21000000,0x1000);
            Allocate(0x22000000,0x20000);Allocate(0x28000000,0x10000);
            Allocate(0x08490000,0x10000);
            Write(0x20000000,File.ReadAllBytes(arguments[0]));Write(0x28009ff0,File.ReadAllBytes(arguments[1]));
            Stub(0x280005f0,0x21000800,true);Stub(0x2800bab0,0x21000804,true);Stub(0x2800b290,0x21000808,false);
            // 通知桩记录通知发生时的展示阵营，验证同步先于模型刷新。
            Write(0x280005f0,new byte[]{0xa1,0x18,0xb6,0x49,0x08,0xa3,0x0c,0x08,0x00,0x21,
                0xff,0x05,0x00,0x08,0x00,0x21,0xc2,4,0});
            Word(0x21000000,0x22000000);Word(0x21000004,2);Word(0x21000008,(1<<18)-1);Word(0x2100000c,1);
            Word(0x220105d8,18);
            for(int index=0;index<18;index++) {
                int member=0x2200e368+index*0x1d0;
                Word(member,index+1);Word(member+0x88,index<12?2:6);
                Marshal.WriteByte(new IntPtr(member+0xb0),255);
            }
            Entry entry=(Entry)Marshal.GetDelegateForFunctionPointer(new IntPtr(0x20000000),typeof(Entry));
            entry();
            Require(Read(0x21000800)==1 && Read(0x21000804)==2 && Read(0x21000808)==1);
            Require(Read(0x2100080c)==2);
            for(int index=0;index<18;index++) {
                Require(Slot(index)<19);
                for(int other=index+1;other<18;other++)
                    if((index<12)==(other<12)) Require(Slot(index)!=Slot(other));
            }
            Word(0x21000008,0);Word(0x2100000c,0);entry();
            Require(Read(0x21000800)==2 && Read(0x21000804)==4 && Read(0x21000808)==1);
            byte[] stableSlots=new byte[18];
            for(int index=0;index<18;index++) stableSlots[index]=Slot(index);
            // 本人与远端交替换队；未变化成员的槽位必须保持原值。
            for(int iteration=0;iteration<60;iteration++) {
                int changedIndex=iteration%2;
                int targetTeam=new int[]{1,2,6}[iteration%3];
                int changedMember=0x2200e368+changedIndex*0x1d0;
                Word(changedMember+0x88,targetTeam);
                Marshal.WriteByte(new IntPtr(changedMember+0xb0),255);
                Word(0x21000008,1<<changedIndex);Word(0x2100000c,changedIndex==0?1:0);
                int previousDisplayTeam=Read(0x0849b618);
                entry();
                Require(Read(0x2100080c)==(changedIndex==0?targetTeam:previousDisplayTeam));
                for(int index=0;index<18;index++) {
                    if(index!=changedIndex) Require(Slot(index)==stableSlots[index]);
                    Require(Slot(index)<19);
                    for(int other=index+1;other<18;other++)
                        if(Read(0x2200e3f0+index*0x1d0)==Read(0x2200e3f0+other*0x1d0))
                            Require(Slot(index)!=Slot(other));
                    stableSlots[index]=Slot(index);
                }
                Word(0x21000008,0);Word(0x2100000c,0);entry();
                for(int index=0;index<18;index++) Require(Slot(index)==stableSlots[index]);
            }
            Console.WriteLine("12 participants + 6 spectators: original allocator and notification ABI passed");
            return 0;
        } catch(Exception error) {Console.WriteLine(error);return 1;}
    }
}
