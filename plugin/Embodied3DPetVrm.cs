using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

namespace Marisa.Embodied3DPet;

internal static class Embodied3DPetVrm
{
    static string Folder(string root)=>Path.Combine(root,"character-agent","data","models");
    internal static (string,string)[] List(string root)
    {
        if(!Directory.Exists(Folder(root)))return Array.Empty<(string,string)>();
        return Directory.GetFiles(Folder(root),"*.vrm").OrderBy(p=>p).Select(p=>{
            string id=Path.GetFileNameWithoutExtension(p),name=id;
            string label=p+".name";if(File.Exists(label))name=File.ReadAllText(label);
            return (id,name);
        }).ToArray();
    }
    internal static async Task<string> ImportAsync(string root,string filename,Stream source)
    {
        if(!filename.EndsWith(".vrm",StringComparison.OrdinalIgnoreCase))throw new ArgumentException("请选择 .vrm 文件");
        if(!File.Exists(Path.Combine(root,"character-agent","config","motion.json")))throw new InvalidOperationException("请先启用一次模块，完成运行环境初始化后再导入 VRM");
        string folder=Folder(root);Directory.CreateDirectory(folder);
        string id="vrm_"+Guid.NewGuid().ToString("N"),temporary=Path.Combine(folder,id+".partial");
        try {
            await using(var output=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write))await source.CopyToAsync(output);
            using(var input=File.OpenRead(temporary))using(var reader=new BinaryReader(input,Encoding.UTF8)){
                if(input.Length<20||input.Length>300L*1024*1024||reader.ReadUInt32()!=0x46546C67||reader.ReadUInt32()!=2||reader.ReadUInt32()!=input.Length)throw new InvalidDataException("不是有效的 VRM / GLB 文件");
                uint length=reader.ReadUInt32(),kind=reader.ReadUInt32();
                if(kind!=0x4E4F534A||length>16*1024*1024||length>input.Length-20)throw new InvalidDataException("VRM 元数据无效");
                using var json=JsonDocument.Parse(reader.ReadBytes((int)length));
                if(!json.RootElement.TryGetProperty("extensions",out var extensions)||!(extensions.TryGetProperty("VRM",out _)||extensions.TryGetProperty("VRMC_vrm",out _)))throw new InvalidDataException("文件没有 VRM 扩展");
            }
            string target=Path.Combine(folder,id+".vrm");File.Move(temporary,target);
            File.WriteAllText(target+".name",Path.GetFileNameWithoutExtension(filename));return id;
        }finally{if(File.Exists(temporary))File.Delete(temporary);}
    }
}
