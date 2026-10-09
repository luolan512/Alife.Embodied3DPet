using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Threading;
using Alife.Framework;
using Microsoft.AspNetCore.Components;
using Microsoft.AspNetCore.Components.Forms;
using Microsoft.AspNetCore.Components.Rendering;

namespace Marisa.Embodied3DPet;

public sealed class Embodied3DPetUI : ModuleUIBase<Embodied3DPetModule, Embodied3DPetConfig>, IDisposable
{
    [Inject] public PluginSystem PluginSystem { get; set; } = null!;
    [Inject] public ConfigurationSystem ConfigurationSystem { get; set; } = null!;
    string root="", loadedRoot="", message="", encoderMode="cpu";
    string proxy="", environmentStatus="", pluginDirectory="";
    string importTarget="";
    CancellationTokenSource? environmentJob;
    bool environmentBusy;
    int layerChunk=2;
    async Task EnvironmentRun(Func<CancellationToken,Task<string>> action)
    {
        if(environmentBusy)return;environmentBusy=true;environmentJob=CancellationTokenSource.CreateLinkedTokenSource(refreshStop.Token);
        try{environmentStatus=await action(environmentJob.Token);}catch(OperationCanceledException){environmentStatus="操作已取消，已安装文件保留。";}catch(Exception e){environmentStatus=e.Message;}
        finally{environmentBusy=false;environmentJob.Dispose();environmentJob=null;}
    }
    string EnvironmentFile=>Path.Combine(root,"character-agent","config","environment.json");
    void SaveProxy(){if(!Directory.Exists(Path.GetDirectoryName(EnvironmentFile)))throw new InvalidOperationException("请先启用模块准备运行环境");File.WriteAllText(EnvironmentFile,System.Text.Json.JsonSerializer.Serialize(new{proxy}));environmentStatus="网络设置已保存，仅用于本插件下载。";}
    Task ImportModel(InputFileChangeEventArgs e)=>EnvironmentRun(async ct=>{
        var asset=EmbodiedEnvironment.Catalog(pluginDirectory).Single(a=>a.path==importTarget);
        if(e.File.Size!=asset.bytes)throw new InvalidDataException("导入文件大小不符合所选模型版本");
        await using var stream=e.File.OpenReadStream(asset.bytes,ct);await EmbodiedEnvironment.ImportAsync(root,asset,stream,ct);
        return EmbodiedEnvironment.Summary(root,pluginDirectory)+"\n模型已导入并校验。";
    });
    Task DownloadModels()=>EnvironmentRun(async ct=>{SaveProxy();await EmbodiedEnvironment.DownloadAsync(root,pluginDirectory,proxy,"全部",text=>{_ = InvokeAsync(()=>{environmentStatus=text;StateHasChanged();});},ct);return EmbodiedEnvironment.Summary(root,pluginDirectory)+"\n模型下载和校验完成。";});
    bool busy, motionDirty, encoderDirty;
    readonly CancellationTokenSource refreshStop=new();
    string externalStamp="";
    protected override void OnInitialized(){_ = WatchSettings();}
    public void Dispose(){refreshStop.Cancel();environmentJob?.Cancel();}
    async Task WatchSettings(){try {using var timer=new PeriodicTimer(TimeSpan.FromSeconds(1));while(await timer.WaitForNextTickAsync(refreshStop.Token))await InvokeAsync(()=>{if(busy||string.IsNullOrEmpty(root))return;string m=Path.Combine(root,"character-agent","config","motion.json"),e=Path.Combine(root,"character-agent","config","text-encoder.json");if(!File.Exists(m)||!File.Exists(e))return;var stamp=File.GetLastWriteTimeUtc(m).Ticks+":"+File.GetLastWriteTimeUtc(e).Ticks;if(stamp==externalStamp)return;if(!motionDirty)motion=Embodied3DPetMotionSettings.Read(root,"motion.json");if(!encoderDirty){var enc=Embodied3DPetMotionSettings.Read(root,"text-encoder.json");encoderMode=Convert.ToString(enc["mode"],CultureInfo.InvariantCulture)??"cpu";layerChunk=int.Parse(Convert.ToString(enc["layer_chunk"],CultureInfo.InvariantCulture)!,CultureInfo.InvariantCulture);}if(!motionDirty&&!encoderDirty)externalStamp=stamp;StateHasChanged();});}catch(OperationCanceledException){}catch(Exception){}}
    Dictionary<string,object> motion=new();
    (string,string)[] models=Array.Empty<(string,string)>();
    protected override void OnParametersSet()
    {
        try {
            pluginDirectory=PluginSystem.PluginContext.GetPluginDirectoryPath("Marisa.Embodied3DPet");
            root=Module?.RuntimeDirectory ?? Embodied3DPetRuntime.ResolveRuntimeDirectory(PluginSystem.PluginContext.GetPluginDirectoryPath("Marisa.Embodied3DPet"));
            models=Embodied3DPetVrm.List(root);
            if(root!=loadedRoot && File.Exists(Path.Combine(root,"character-agent","config","motion.json"))) {
                motion=Embodied3DPetMotionSettings.Read(root,"motion.json");
                var encoder=Embodied3DPetMotionSettings.Read(root,"text-encoder.json");
                encoderMode=Convert.ToString(encoder["mode"],CultureInfo.InvariantCulture)??"cpu";
                layerChunk=int.Parse(Convert.ToString(encoder["layer_chunk"],CultureInfo.InvariantCulture)!,CultureInfo.InvariantCulture);
                loadedRoot=root;
                if(File.Exists(EnvironmentFile)){using var env=System.Text.Json.JsonDocument.Parse(File.ReadAllText(EnvironmentFile));proxy=env.RootElement.GetProperty("proxy").GetString()??"";}
                environmentStatus=EmbodiedEnvironment.Summary(root,pluginDirectory);importTarget=EmbodiedEnvironment.Catalog(pluginDirectory).First(a=>a.path.EndsWith(".safetensors")).path;
            }
        }catch(Exception e){message=e.Message;}
    }
    async Task Run(Func<Task> action)
    {
        busy=true;message="处理中…";
        try {await action();}catch(Exception e){message=e.Message;}finally{busy=false;}
    }
    void Persist()
    {
        Configuration.DynamicFace=true;Configuration.EnableDragging=true;Configuration.ForwardInteractions=false;
        ConfigurationSystem.SetConfiguration(typeof(Embodied3DPetModule),Configuration,Character?.StorageKey??"");
        if(Module!=null)Module.Configuration=Configuration;
        message="默认角色已保存，重新启用模块后生效。";
    }
    Task Import(InputFileChangeEventArgs e)=>Run(async()=>{
        var file=e.File;
        if(!file.Name.EndsWith(".vrm",StringComparison.OrdinalIgnoreCase))throw new ArgumentException("请选择 .vrm 文件");
        await using var stream=file.OpenReadStream(300L*1024*1024);
        Configuration.Model=await Embodied3DPetVrm.ImportAsync(root,file.Name,stream);
        models=Embodied3DPetVrm.List(root);Persist();
        message="VRM 已导入并设为默认角色；重新启用模块后显示。";
    });
    async Task SaveMotion()
    {
        var patch=new[]{"history_frames","denoising_steps","inference_backend"}.ToDictionary(k=>k,k=>motion[k]);
        if(Module!=null)await Module.SaveMotionSettingsAsync(patch);else Embodied3DPetMotionSettings.SaveMotion(root,patch);
        motionDirty=false;message="已应用，桌宠动作配置同步更新。";
    }
    async Task SaveEncoder()
    {
        if(Module!=null)await Module.SaveEncoderSettingsAsync(encoderMode,layerChunk);else Embodied3DPetMotionSettings.SaveEncoder(root,encoderMode,layerChunk);
        encoderDirty=false;message="编码方式已同步保存；重新启用模块后生效。";
    }
    protected override void BuildRenderTree(RenderTreeBuilder b)
    {
        int s=0;
        b.OpenElement(s++,"section");b.AddAttribute(s++,"class","embodied3d-settings");
        b.AddMarkupContent(s++,"<style>"+Styles+"</style>");
        Heading(b,ref s,"具身3D桌宠");
        b.OpenElement(s++,"label");b.AddContent(s++,"导入 VRM 文件 ");
        b.OpenComponent<InputFile>(s++);b.AddAttribute(s++,"OnChange",EventCallback.Factory.Create<InputFileChangeEventArgs>(this,Import));
        b.AddAttribute(s++,"AdditionalAttributes",new Dictionary<string,object>{{"accept",".vrm"},{"disabled",busy}});b.CloseComponent();b.CloseElement();
        Select(b,ref s,"默认 VRM",Configuration.Model,v=>{Configuration.Model=v;Persist();},models);
        Text(b,ref s,"导入后自动设为默认角色，重新启用模块后显示。");
        if(motion.Count>0){
            Heading(b,ref s,"动作生成参数");
            Number(b,ref s,"历史长度（帧，20 fps）","history_frames",0,200,1);
            Text(b,ref s,"16 帧约 0.8 秒；表示参考前一动作的范围，不是对话记忆。");
            Number(b,ref s,"去噪步数","denoising_steps",1,10,1);
            Select(b,ref s,"推理后端",Value("inference_backend"),v=>{motion["inference_backend"]=v;motionDirty=true;},new[]{("none","PyTorch"),("trt-fp16","TensorRT FP16"),("torch-compile","Torch Compile")});
            Text(b,ref s,"切换推理后端会重新加载模型；TensorRT 引擎不匹配时由后台处理回退。");
            Button(b,ref s,"应用生成参数",()=>Run(SaveMotion));
            Heading(b,ref s,"动作文本编码");
            Select(b,ref s,"编码方式",encoderMode,v=>{encoderMode=v;encoderDirty=true;},new[]{("cpu","CPU"),("vulkan-stream","Vulkan GPU 分块"),("vulkan-resident","Vulkan GPU 全模型常驻")});
            if(encoderMode=="vulkan-stream")InputNumber(b,ref s,"GPU 分块层数",layerChunk,1,8,1,v=>{layerChunk=(int)v;encoderDirty=true;});
            Text(b,ref s,"GPU 常驻需要更多显存；此设置重新启用模块后生效。");
            Button(b,ref s,"保存编码方式",()=>Run(SaveEncoder));
        }else Text(b,ref s,"首次启用模块后可设置动作生成参数。");
        b.OpenElement(s++,"details");b.AddAttribute(s++,"class","environment-config");
        b.OpenElement(s++,"summary");b.AddContent(s++,"环境配置");b.CloseElement();
        Text(b,ref s,"独立 Python、Torch / CUDA、TensorRT 与全部模型都是本插件必备组件。安装和修复限定在本插件目录。");
        Text(b,ref s,"模型下载需要访问 Hugging Face、Google Drive 和 GitHub。连接失败或较慢时，可能需要先配置代理或其他网络设置。请先测试连接，也可从来源自行获取文件。");
        b.OpenElement(s++,"label");b.AddContent(s++,"下载代理");b.OpenElement(s++,"input");b.AddAttribute(s++,"type","text");b.AddAttribute(s++,"placeholder","留空使用默认网络");b.AddAttribute(s++,"value",proxy);b.AddAttribute(s++,"onchange",EventCallback.Factory.Create<ChangeEventArgs>(this,e=>proxy=e.Value?.ToString()??""));b.CloseElement();b.CloseElement();
        Button(b,ref s,"保存网络设置",()=>Run(()=>{SaveProxy();return Task.CompletedTask;}),environmentBusy);
        Button(b,ref s,"检查环境",()=>EnvironmentRun(ct=>EmbodiedEnvironment.CheckAsync(root,pluginDirectory,ct)),environmentBusy);
        Button(b,ref s,"测试下载连接",()=>EnvironmentRun(ct=>EmbodiedEnvironment.TestNetworkAsync(proxy,ct)),environmentBusy);
        Button(b,ref s,"下载 / 修复必备模型",DownloadModels,environmentBusy);
        b.OpenElement(s++,"details");b.OpenElement(s++,"summary");b.AddContent(s++,"手动导入模型");b.CloseElement();
        var imports=EmbodiedEnvironment.Catalog(pluginDirectory).Where(a=>new[]{".safetensors",".gguf",".chkpt",".pkl",".npy"}.Contains(Path.GetExtension(a.path))).ToArray();
        Select(b,ref s,"导入目标",importTarget,v=>importTarget=v,imports.Select(a=>(a.path,a.group+" / "+Path.GetFileName(a.path))).ToArray());
        b.OpenComponent<InputFile>(s++);b.AddAttribute(s++,"OnChange",EventCallback.Factory.Create<InputFileChangeEventArgs>(this,ImportModel));b.AddAttribute(s++,"AdditionalAttributes",new Dictionary<string,object>{{"disabled",environmentBusy}});b.CloseComponent();b.CloseElement();
        if(environmentBusy){b.OpenElement(s++,"button");b.AddAttribute(s++,"type","button");b.AddAttribute(s++,"onclick",EventCallback.Factory.Create(this,()=>environmentJob?.Cancel()));b.AddContent(s++,"取消操作");b.CloseElement();}
        b.OpenElement(s++,"pre");b.AddAttribute(s++,"style","white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px");b.AddContent(s++,environmentStatus);b.CloseElement();
        foreach(var item in new[]{("ARDY 来源","https://huggingface.co/nvidia/ARDY-Core-RP-20FPS-Horizon40/tree/main"),("Kimodo 来源","https://huggingface.co/LocalAI-io/Llama-3-Kimodo-GGML/tree/main"),("表情资源来源","https://github.com/WalkerMitty/EmoAva")}){b.OpenElement(s++,"a");b.AddAttribute(s++,"href",item.Item2);b.AddAttribute(s++,"target","_blank");b.AddAttribute(s++,"rel","noopener");b.AddAttribute(s++,"style","margin-right:12px");b.AddContent(s++,item.Item1);b.CloseElement();}
        b.CloseElement();
        b.OpenElement(s++,"p");b.AddAttribute(s++,"role","status");b.AddContent(s++,message);b.CloseElement();b.CloseElement();
    }
    const string Styles="""
        .embodied3d-settings{box-sizing:border-box;width:100%;max-width:740px;margin:8px auto;padding:24px 28px;font:14px/1.65 'Segoe UI','Microsoft YaHei',sans-serif;color:var(--text-color,#342e45);background:var(--background-color,#fbf9ff);border:1px solid #8b74ac25;border-radius:20px;box-shadow:0 8px 28px #35234608}
        .embodied3d-settings h4{font-size:15px;letter-spacing:.02em;margin:26px 0 16px;padding-top:20px;border-top:1px solid #8b74ac25;color:inherit}.embodied3d-settings h4:first-of-type{border:0;margin:0 0 20px;padding:0;font-size:22px;font-weight:650;color:#8061aa}
        .embodied3d-settings label{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:14px 0;font-size:14px}.embodied3d-settings select,.embodied3d-settings input[type=number]{box-sizing:border-box;width:230px;max-width:58%;padding:9px 12px;border:1px solid #8b74ac40;border-radius:10px;background:var(--background-color,#fff);color:inherit;font:inherit;outline:none}.embodied3d-settings input:focus,.embodied3d-settings select:focus{border-color:#9a7ac1;box-shadow:0 0 0 3px #9a7ac115}.embodied3d-settings input[type=file]{font:12px inherit;max-width:62%;color:inherit}.embodied3d-settings input::file-selector-button{border:1px solid #8b74ac40;border-radius:9px;background:#eee7f655;color:inherit;padding:8px 12px;cursor:pointer;margin-right:10px}
        .embodied3d-settings p{font-size:12px;color:#8b7d9b;line-height:1.8;margin:8px 0 14px}.embodied3d-settings button{border:0;border-radius:10px;background:#8263a8;color:white;padding:9px 17px;margin:8px 0;font:13px inherit;cursor:pointer;box-shadow:0 3px 10px #8263a815}.embodied3d-settings button:hover{background:#715192}.embodied3d-settings button:disabled{opacity:.55;cursor:wait}.embodied3d-settings .environment-config{border:1px solid #8b74ac30;border-radius:12px;padding:12px 16px;margin-top:24px}.embodied3d-settings summary{font-weight:600;cursor:pointer;color:#8061aa}.embodied3d-settings input[type=text]{width:230px;max-width:58%;padding:9px 12px;border:1px solid #8b74ac40;border-radius:10px;background:var(--background-color,#fff);color:inherit}.embodied3d-settings .environment-config button{margin-right:8px}.embodied3d-settings [role=status]{color:#8061aa;margin:14px 0 0;min-height:20px}@media(max-width:440px){.embodied3d-settings{padding:18px}.embodied3d-settings label{gap:10px}.embodied3d-settings select,.embodied3d-settings input[type=number]{width:55%}}
        """;
    string Value(string key)=>motion.TryGetValue(key,out var v)?Convert.ToString(v,CultureInfo.InvariantCulture)??"":"";
    bool Bool(string key)=>bool.TryParse(Value(key),out bool v)&&v;
    void Number(RenderTreeBuilder b,ref int s,string label,string key,double min,double max,double step)=>InputNumber(b,ref s,label,double.Parse(Value(key),CultureInfo.InvariantCulture),min,max,step,v=>{motion[key]=v;motionDirty=true;});
    static void Heading(RenderTreeBuilder b,ref int s,string text){b.OpenElement(s++,"h4");b.AddContent(s++,text);b.CloseElement();}
    static void Text(RenderTreeBuilder b,ref int s,string text){b.OpenElement(s++,"p");b.AddContent(s++,text);b.CloseElement();}
    void Button(RenderTreeBuilder b,ref int s,string label,Func<Task> action,bool disabled=false){b.OpenElement(s++,"button");b.AddAttribute(s++,"type","button");b.AddAttribute(s++,"disabled",busy||disabled);b.AddAttribute(s++,"onclick",EventCallback.Factory.Create(this,action));b.AddContent(s++,label);b.CloseElement();}
    void InputNumber(RenderTreeBuilder b,ref int s,string label,double value,double min,double max,double step,Action<double> change)
    {
        b.OpenElement(s++,"label");b.AddContent(s++,label+" ");b.OpenElement(s++,"input");b.AddAttribute(s++,"type","number");b.AddAttribute(s++,"min",min);b.AddAttribute(s++,"max",max);b.AddAttribute(s++,"step",step);b.AddAttribute(s++,"value",value.ToString(CultureInfo.InvariantCulture));b.AddAttribute(s++,"onchange",EventCallback.Factory.Create<ChangeEventArgs>(this,e=>{if(double.TryParse(e.Value?.ToString(),NumberStyles.Float,CultureInfo.InvariantCulture,out var n)&&double.IsFinite(n)&&n>=min&&n<=max&&(step<1||n==Math.Floor(n)))change(n);else message=label+" 超出范围或需要整数";}));b.CloseElement();b.CloseElement();
    }
    void Select(RenderTreeBuilder b,ref int s,string label,string value,Action<string> change,(string,string)[] choices)
    {
        b.OpenElement(s++,"label");b.AddContent(s++,label+" ");b.OpenElement(s++,"select");b.AddAttribute(s++,"value",value);b.AddAttribute(s++,"onchange",EventCallback.Factory.Create<ChangeEventArgs>(this,e=>change(e.Value?.ToString()??"")));
        foreach(var option in choices){b.OpenElement(s++,"option");b.AddAttribute(s++,"value",option.Item1);b.AddAttribute(s++,"selected",option.Item1==value);b.AddContent(s++,option.Item2);b.CloseElement();}b.CloseElement();b.CloseElement();
    }
    void Check(RenderTreeBuilder b,ref int s,string label,bool value,Action<bool> change){b.OpenElement(s++,"label");b.OpenElement(s++,"input");b.AddAttribute(s++,"type","checkbox");b.AddAttribute(s++,"checked",value);b.AddAttribute(s++,"onchange",EventCallback.Factory.Create<ChangeEventArgs>(this,e=>change(e.Value is true)));b.CloseElement();b.AddContent(s++," "+label);b.CloseElement();}
}
