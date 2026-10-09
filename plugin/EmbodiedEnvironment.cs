using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace Marisa.Embodied3DPet;

internal sealed record EnvironmentAsset(string group,string path,string url,long bytes,string sha256);
internal static class EmbodiedEnvironment
{
    internal static EnvironmentAsset[] Catalog(string plugin)=>JsonSerializer.Deserialize<EnvironmentAsset[]>(File.ReadAllText(Path.Combine(plugin,"Environment-Models.json")))??Array.Empty<EnvironmentAsset>();
    internal static string Target(string root,string relative)
    {
        string full=Path.GetFullPath(Path.Combine(root,relative));
        if(!full.StartsWith(Path.GetFullPath(root)+Path.DirectorySeparatorChar,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("模型目标路径越界");
        return full;
    }
    internal static string Summary(string root,string plugin)
    {
        var lines=new List<string>{File.Exists(Path.Combine(root,"runtime","python","python.exe"))?"独立 Python：已安装":"独立 Python：未安装",File.Exists(Path.Combine(root,"runtime","python","Lib","site-packages","torch","__init__.py"))?"Torch / CUDA：文件已安装，点击检查验证可用性":"Torch / CUDA：未安装",File.Exists(Path.Combine(root,"runtime","tensorrt","nvinfer_10.dll"))?"TensorRT：已安装":"TensorRT：未安装"};
        foreach(var group in Catalog(plugin).GroupBy(x=>x.group)) {
            int installed=group.Count(x=>File.Exists(Target(root,x.path))&&new FileInfo(Target(root,x.path)).Length==x.bytes);
            lines.Add(group.Key+"："+(installed==group.Count()?"文件齐全":"缺少或损坏")+"（"+installed+" / "+group.Count()+"）");
        }
        return string.Join("\n",lines);
    }
    internal static async Task<string> CheckAsync(string root,string plugin,CancellationToken ct)
    {
        string python=Path.Combine(root,"runtime","python","python.exe");
        if(!File.Exists(python))return Summary(root,plugin)+"\n请先启用模块准备独立运行环境。";
        var info=new ProcessStartInfo(python){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,WorkingDirectory=root};
        info.ArgumentList.Add(Path.Combine(root,"scripts","check-python.py"));
        foreach(string key in new[]{"PYTHONHOME","PYTHONPATH","CHARACTER_PYTHON"})info.Environment.Remove(key);
        using var process=Process.Start(info)??throw new IOException("环境检查进程无法启动");
        var output=process.StandardOutput.ReadToEndAsync(ct);var error=process.StandardError.ReadToEndAsync(ct);
        try{await process.WaitForExitAsync(ct);}catch{if(!process.HasExited)process.Kill(true);throw;}
        string result=await output;
        if(process.ExitCode!=0)return Summary(root,plugin)+"\n依赖检查失败："+result+await error;
        using var doc=JsonDocument.Parse(result.Trim());var d=doc.RootElement;
        return Summary(root,plugin)+"\nPython "+d.GetProperty("python").GetString()+" · Torch "+d.GetProperty("torch").GetString()+" · CUDA "+(d.GetProperty("cuda_available").GetBoolean()?"可用":"不可用")+"\n模型状态按文件大小检查；下载的新文件会另做 SHA256 校验。";
    }
    static HttpClient Client(string proxy)
    {
        var handler=new HttpClientHandler();
        if(!string.IsNullOrWhiteSpace(proxy)) {
            if(!Uri.TryCreate(proxy,UriKind.Absolute,out var uri)||!(uri.Scheme=="http"||uri.Scheme=="https"))throw new ArgumentException("代理地址须以 http:// 或 https:// 开头");
            handler.Proxy=new WebProxy(uri);handler.UseProxy=true;
        }
        return new HttpClient(handler){Timeout=Timeout.InfiniteTimeSpan};
    }
    internal static async Task<string> TestNetworkAsync(string proxy,CancellationToken ct)
    {
        using var client=Client(proxy);var lines=new List<string>();
        foreach(var url in new[]{"https://huggingface.co/nvidia/ARDY-Core-RP-20FPS-Horizon40/resolve/main/config.yaml","https://drive.google.com/uc?export=download&id=1halbLdJXOK3zPLDsd66nXkiqHut2ffn9","https://raw.githubusercontent.com/WalkerMitty/EmoAva/main/README.md"}) {
            using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(15));
            try{using var request=new HttpRequestMessage(HttpMethod.Head,url);using var r=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,timeout.Token);lines.Add(new Uri(url).Host+"："+(int)r.StatusCode+(r.IsSuccessStatusCode?" 连接成功":" 请检查网络或来源权限"));}
            catch(Exception e)when(!ct.IsCancellationRequested){lines.Add(new Uri(url).Host+"：连接失败（"+e.Message+"）");}
        }
        return string.Join("\n",lines)+"\n连接测试不等于完整大文件下载成功。";
    }
    internal static async Task DownloadAsync(string root,string plugin,string proxy,string group,Action<string> progress,CancellationToken ct)
    {
        if(!File.Exists(Path.Combine(root,"runtime","python","python.exe")))throw new InvalidOperationException("请先启用一次模块，准备独立运行环境后再下载模型。");
        using var client=Client(proxy);
        foreach(var asset in Catalog(plugin).Where(a=>group=="全部"||a.group==group)) {
            ct.ThrowIfCancellationRequested();string target=Target(root,asset.path);
            if(File.Exists(target)&&new FileInfo(target).Length==asset.bytes){progress("校验已安装文件："+Path.GetFileName(target));await using var installed=File.OpenRead(target);if(Convert.ToHexString(await SHA256.HashDataAsync(installed,ct)).Equals(asset.sha256,StringComparison.OrdinalIgnoreCase))continue;}
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);string partial=target+".download-partial-"+Guid.NewGuid().ToString("N");
            try {
                using var timeout=CancellationTokenSource.CreateLinkedTokenSource(ct);timeout.CancelAfter(TimeSpan.FromSeconds(45));
                var response=await client.GetAsync(asset.url,HttpCompletionOption.ResponseHeadersRead,timeout.Token);
                if(asset.url.Contains("drive.google.com")&&response.Content.Headers.ContentType?.MediaType=="text/html") {
                    string html=await response.Content.ReadAsStringAsync(timeout.Token);response.Dispose();
                    var action=Regex.Match(html,"<form[^>]*action=\"([^\"]+)\"");
                    if(!action.Success)throw new IOException("Google Drive 未提供下载确认，可能达到限额；请打开来源手动下载并导入");
                    var endpoint=WebUtility.HtmlDecode(action.Groups[1].Value);
                    if(!Uri.TryCreate(endpoint,UriKind.Absolute,out var uri)||uri.Scheme!="https"||uri.Host!="drive.usercontent.google.com")throw new IOException("下载确认地址不符合预期");
                    var fields=Regex.Matches(html,"<input[^>]*name=\"([^\"]+)\"[^>]*value=\"([^\"]*)\"").Cast<Match>().Select(m=>Uri.EscapeDataString(m.Groups[1].Value)+"="+Uri.EscapeDataString(WebUtility.HtmlDecode(m.Groups[2].Value)));
                    response=await client.GetAsync(endpoint+"?"+string.Join("&",fields),HttpCompletionOption.ResponseHeadersRead,timeout.Token);
                }
                using(response) {
                    response.EnsureSuccessStatusCode();
                    if(response.Content.Headers.ContentType?.MediaType=="text/html")throw new IOException("来源返回网页而非模型；请检查来源权限或手动导入");
                    await using var input=await response.Content.ReadAsStreamAsync(ct);
                    await using(var file=new FileStream(partial,FileMode.Create,FileAccess.Write,FileShare.None,1024*1024,true)) {
                        var buffer=new byte[1024*1024];long total=0;var last=DateTime.MinValue;
                        while(true){int n=await input.ReadAsync(buffer,ct);if(n==0)break;total+=n;if(total>asset.bytes)throw new InvalidDataException("下载大小超出清单");await file.WriteAsync(buffer.AsMemory(0,n),ct);if(DateTime.UtcNow-last>TimeSpan.FromMilliseconds(500)){progress(asset.group+" · "+Path.GetFileName(asset.path)+" · "+(total/1048576.0).ToString("0.0")+" / "+(asset.bytes/1048576.0).ToString("0.0")+" MiB");last=DateTime.UtcNow;}}
                        if(total!=asset.bytes)throw new InvalidDataException("下载文件不完整");
                    }
                }
                progress("校验："+Path.GetFileName(asset.path));
                await using(var file=File.OpenRead(partial)){string hash=Convert.ToHexString(await SHA256.HashDataAsync(file,ct));if(!hash.Equals(asset.sha256,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("模型 SHA256 校验不匹配，保留原文件");}
                File.Move(partial,target,true);
            }finally{if(File.Exists(partial))File.Delete(partial);}
        }
        progress("模型准备完成。" );
    }
    internal static async Task ImportAsync(string root,EnvironmentAsset asset,System.IO.Stream input,CancellationToken ct)
    {
        string target=Target(root,asset.path);Directory.CreateDirectory(Path.GetDirectoryName(target)!);string partial=target+".import-partial-"+Guid.NewGuid().ToString("N");
        try {
            await using(var file=new FileStream(partial,FileMode.CreateNew,FileAccess.Write,FileShare.None,1024*1024,true)){
                byte[] buffer=new byte[1024*1024];long total=0;while(true){int n=await input.ReadAsync(buffer,ct);if(n==0)break;total+=n;if(total>asset.bytes)throw new InvalidDataException("导入文件大小不符合清单");await file.WriteAsync(buffer.AsMemory(0,n),ct);}if(total!=asset.bytes)throw new InvalidDataException("导入文件不完整");
            }
            await using(var file=File.OpenRead(partial))if(!Convert.ToHexString(await SHA256.HashDataAsync(file,ct)).Equals(asset.sha256,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("模型版本或 SHA256 不匹配，原文件保留");
            File.Move(partial,target,true);
        }finally{if(File.Exists(partial))File.Delete(partial);}
    }
}
