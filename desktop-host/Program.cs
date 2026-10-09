using System;
using System.IO;
using System.Text.Json;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using Forms = System.Windows.Forms;

internal static class Program
{
    [STAThread] public static void Main(string[] args)
    {
        string project = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../.."));
        string url = "http://127.0.0.1:8092/pet.html";
        bool validate = false, debug=false, validateBubble=false, validateReply=false,validateShutdown=false;
        for(int i=0;i<args.Length;i++) {
            if(args[i]=="--project" && i+1<args.Length) project=Path.GetFullPath(args[++i]);
            else if(args[i]=="--url" && i+1<args.Length) url=args[++i];
            else if(args[i]=="--validate") validate=true;
            else if(args[i]=="--validate-shutdown") {validate=true;validateShutdown=true;}
            else if(args[i]=="--validate-bubble") {validate=true;validateBubble=true;}
            else if(args[i]=="--validate-reply") {validate=true;validateBubble=true;validateReply=true;}
            else if(args[i]=="--debug") debug=true;
        }
        var uri=new Uri(url);
        if(uri.Host!="127.0.0.1" || uri.Scheme!="http") throw new ArgumentException("Only local pet pages are supported.");
        var app=new System.Windows.Application();
        using var instance=new System.Threading.Mutex(false,"Local\\MarisaEmbodied3DPetDesktopPet");
        if(!instance.WaitOne(0))return;
        app.DispatcherUnhandledException+=(_,e)=> {
            Directory.CreateDirectory(Path.Combine(project,".desktop"));
            File.AppendAllText(Path.Combine(project,".desktop","host.log"),e.Exception.ToString());
            e.Handled=true;app.Shutdown(1);
        };
        var backend=CaptureBackend(project,uri.Port);
        try {app.Run(new PetWindow(project,uri,validate,debug,validateBubble,validateReply));}
        finally {if(!validate||validateShutdown)StopBackend(project,uri.Port,backend);}
    }

    private static (int pid,string started)? CaptureBackend(string project,int port)
    {
        try {
            using var record=JsonDocument.Parse(File.ReadAllText(Path.Combine(project,".run","server.json")));
            var data=record.RootElement;
            if(data.GetProperty("port").GetInt32()!=port)return null;
            return (data.GetProperty("pid").GetInt32(),data.GetProperty("started").GetString()!);
        }catch{return null;}
    }
    private static void StopBackend(string project,int port,(int pid,string started)? backend)
    {
        if(backend is null)return;
        try {
            var script=Path.Combine(project,"scripts","stop.ps1");
            if(!File.Exists(script))return;
            var info=new System.Diagnostics.ProcessStartInfo("powershell.exe") {UseShellExecute=false,CreateNoWindow=true,WindowStyle=System.Diagnostics.ProcessWindowStyle.Hidden,WorkingDirectory=project};
            foreach(var arg in new[]{"-NoProfile","-ExecutionPolicy","Bypass","-File",script,"-ExpectedPid",backend.Value.pid.ToString(),"-ExpectedStarted",backend.Value.started,"-ExpectedPort",port.ToString()})info.ArgumentList.Add(arg);
            using var process=System.Diagnostics.Process.Start(info);
            if(process is not null&&(!process.WaitForExit(5000)||process.ExitCode!=0))
                File.AppendAllText(Path.Combine(project,".desktop","host.log"),"Backend shutdown helper did not finish successfully."+Environment.NewLine);
        }catch(Exception e){File.AppendAllText(Path.Combine(project,".desktop","host.log"),"Backend shutdown: "+e.Message+Environment.NewLine);}
    }
}

internal sealed class SpeechBubbleWindow : Window
{
    [DllImport("user32.dll",EntryPoint="GetWindowLongPtrW")] private static extern IntPtr GetStyle(IntPtr window,int index);
    [DllImport("user32.dll",EntryPoint="SetWindowLongPtrW")] private static extern IntPtr SetStyle(IntPtr window,int index,IntPtr value);
    private readonly System.Windows.Controls.TextBlock text=new() {TextWrapping=TextWrapping.Wrap,FontSize=15,FontFamily=new FontFamily("Microsoft YaHei UI"),Foreground=new SolidColorBrush(Color.FromRgb(48,43,63)),MaxHeight=240,TextTrimming=TextTrimming.CharacterEllipsis};
    private DateTime expires;
    public SpeechBubbleWindow(Window owner)
    {
        Owner=owner;Title="小小伙伴 · 消息气泡";Width=320;SizeToContent=SizeToContent.Height;
        WindowStyle=WindowStyle.None;AllowsTransparency=true;Background=Brushes.Transparent;
        ShowInTaskbar=false;ShowActivated=false;Focusable=false;Topmost=true;ResizeMode=ResizeMode.NoResize;
        var stack=new System.Windows.Controls.StackPanel();
        var border=new System.Windows.Controls.Border {Background=new SolidColorBrush(Color.FromArgb(246,255,253,255)),BorderBrush=new SolidColorBrush(Color.FromRgb(207,196,230)),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(14),Padding=new Thickness(16,12,16,12),Child=text};
        stack.Children.Add(border);
        stack.Children.Add(new System.Windows.Shapes.Polygon {Points=new PointCollection {new System.Windows.Point(0,0),new System.Windows.Point(18,0),new System.Windows.Point(9,10)},Fill=border.Background,Width=18,Height=10,HorizontalAlignment=HorizontalAlignment.Center});Content=stack;
        SourceInitialized+=(_,_)=>{var h=new WindowInteropHelper(this).Handle;SetStyle(h,-20,new IntPtr(GetStyle(h,-20).ToInt64()|0x08000000|0x80|0x20));};
    }
    public void SetText(string value)
    {
        value=value.Trim();text.Text=value.Length>800?value[..800]+"…":value;
        expires=DateTime.UtcNow.AddSeconds(Math.Clamp(value.Length/8.0+4,6,30));
        if(value.Length==0){expires=DateTime.MinValue;Hide();}
    }
    public void Expire(){if(DateTime.UtcNow>=expires&&IsVisible)Hide();}
    public void Place(System.Windows.Point anchor,Rect area)
    {
        if(DateTime.UtcNow>=expires)return;
        Width=Math.Min(320,area.Width-16);((System.Windows.FrameworkElement)Content).Measure(new Size(Width,double.PositiveInfinity));
        var height=((System.Windows.FrameworkElement)Content).DesiredSize.Height;
        Left=Math.Clamp(anchor.X-Width/2,area.Left+8,Math.Max(area.Left+8,area.Right-Width-8));
        var above=anchor.Y-height-8;
        Top=Math.Clamp(above>=area.Top+8?above:anchor.Y+12,area.Top+8,Math.Max(area.Top+8,area.Bottom-height-8));
        if(!IsVisible)Show();
    }
}

internal sealed class PetWindow : Window
{
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X,Y; }
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll",EntryPoint="GetWindowLongPtrW")] private static extern IntPtr GetStyle(IntPtr window,int index);
    [DllImport("user32.dll",EntryPoint="SetWindowLongPtrW")] private static extern IntPtr SetStyle(IntPtr window,int index,IntPtr value);
    private readonly WebView2CompositionControl web=new();
    private readonly DispatcherTimer timer=new() { Interval=TimeSpan.FromMilliseconds(120) };
    private readonly string folder;
    private readonly Uri url;
    private readonly bool validate;
    private readonly bool debug;
    private readonly bool validateBubble;
    private readonly bool validateReply;
    private Forms.NotifyIcon? tray;
    private bool polling,passing,initialized,menuOpen;
    private int ticks,validationReadyTicks;
    private double modelScale=1;
    private double pendingScale=1;
    private readonly DispatcherTimer scaleTimer=new() { Interval=TimeSpan.FromMilliseconds(180) };
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hwnd,IntPtr after,int x,int y,int width,int height,uint flags);
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
    private bool bigHead;
    private double scaleHeight;
    private Rect desktopPlacement;
    private bool dragEnabled;
    private Rect[] dragRegions=Array.Empty<Rect>();
    private IntPtr handle;
    private ChatWindow? chat;
    private SpeechBubbleWindow? bubble;
    private System.Windows.Point headAnchor;
    private bool headAnchorVisible;
    private bool bubbleEnabled;
    private byte[] hitMask=Array.Empty<byte>();
    private int maskWidth,maskHeight;
    private Rect[] hitRegions=Array.Empty<Rect>();
    public PetWindow(string project,Uri url,bool validate,bool debug,bool validateBubble=false,bool validateReply=false)
    {
        this.url=url;this.validate=validate;this.debug=debug;this.validateBubble=validateBubble;this.validateReply=validateReply;folder=Path.Combine(project,".desktop");Directory.CreateDirectory(folder);
        dragEnabled=true; // Dragging moves the body locally and never starts an AI turn.
        Title="小小伙伴 · 3D 桌宠";Width=560;Height=780;
        WindowStyle=WindowStyle.None;AllowsTransparency=true;Background=Brushes.Transparent;
        ResizeMode=ResizeMode.NoResize;Topmost=true;ShowInTaskbar=false;
        web.DefaultBackgroundColor=System.Drawing.Color.Transparent;web.UseLayoutRounding=false;Content=web;
        MouseLeave+=async (_,_)=>{
            if(!initialized||PresentationSource.FromVisual(this) is null)return;
            try{if(GetCursorPos(out var cursor)&&new Rect(0,0,ActualWidth,ActualHeight).Contains(PointFromScreen(new System.Windows.Point(cursor.X,cursor.Y))))return;if(web.CoreWebView2 is not null)await web.CoreWebView2.ExecuteScriptAsync("window.petCloseMenu?.()");}catch(InvalidOperationException){}
        };

        var area=SystemParameters.WorkArea;Left=area.Right-Width-35;Top=area.Bottom-Height-15;
        try { var saved=JsonDocument.Parse(File.ReadAllText(Path.Combine(folder,"placement.json"))).RootElement;
            if(saved.TryGetProperty("scale",out var scale)){modelScale=Math.Clamp(scale.GetDouble(),.35,8);Width=Math.Min(560*modelScale,area.Width-20);Height=Math.Min(780*modelScale,area.Height-20);}
            Left=Math.Clamp(saved.GetProperty("left").GetDouble(),area.Left,Math.Max(area.Left,area.Right-Width));
            Top=Math.Clamp(saved.GetProperty("top").GetDouble(),area.Top,Math.Max(area.Top,area.Bottom-Height));
        } catch { }
        Loaded+=async (_,_)=>await Initialize();
        Closed+=(_,_)=> {
            initialized=false;timer.Stop();scaleTimer.Stop();bubble?.Close();chat?.Close();tray?.Dispose();web.Dispose();
            var placement=bigHead?desktopPlacement:new Rect(Left,Top,ActualWidth,ActualHeight);
            File.WriteAllText(Path.Combine(folder,"placement.json"),JsonSerializer.Serialize(new {left=placement.Left,top=placement.Top,width=placement.Width,scale=modelScale}));
        };
        PreviewKeyDown+=(_,e)=>{if(bigHead&&e.Key==System.Windows.Input.Key.Escape){SetBigHead(false);e.Handled=true;}};
        timer.Tick+=async (_,_)=>await Poll();
        LocationChanged+=(_,_)=>PositionBubble();
        SizeChanged+=(_,_)=>PositionBubble();
        pendingScale=modelScale;
        scaleTimer.Tick+=(_,_)=>{scaleTimer.Stop();if(!bigHead){ApplyScale(pendingScale);web.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new {type="scaleApplied",percent=modelScale*100,windowPercent=scaleHeight/780*100,height=scaleHeight}));}};
    }
    private void ApplyScale(double scale)
    {
        var area=SystemParameters.WorkArea;
        double center=Left+ActualWidth/2,bottom=Top+ActualHeight;
        modelScale=scale;
        var width=Math.Min(560*scale,area.Width-20);var height=Math.Min(780*scale,area.Height-20);scaleHeight=height;
        SetBounds(new Rect(Math.Clamp(center-width/2,area.Left,Math.Max(area.Left,area.Right-width)),Math.Clamp(bottom-height,area.Top,Math.Max(area.Top,area.Bottom-height)),width,height));
    }
    private void SetBounds(Rect bounds)
    {
        var dpi=VisualTreeHelper.GetDpi(this);
        SetWindowPos(handle,IntPtr.Zero,(int)Math.Round(bounds.Left*dpi.DpiScaleX),(int)Math.Round(bounds.Top*dpi.DpiScaleY),(int)Math.Round(bounds.Width*dpi.DpiScaleX),(int)Math.Round(bounds.Height*dpi.DpiScaleY),0x14);
    }
    private void SetBigHead(bool enabled)
    {
        if(bigHead==enabled)return;
        bubble?.Hide();
        scaleTimer.Stop();
        ShowInTaskbar=enabled;Topmost=!enabled;
        if(enabled){if(Math.Abs(pendingScale-modelScale)>.001){ApplyScale(pendingScale);web.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new {type="scaleApplied",percent=modelScale*100,windowPercent=scaleHeight/780*100,height=scaleHeight}));}desktopPlacement=new Rect(Left,Top,ActualWidth,ActualHeight);bigHead=true;SetPassing(false);var screen=Forms.Screen.FromHandle(handle).Bounds;SetWindowPos(handle,IntPtr.Zero,screen.Left,screen.Top,screen.Width,screen.Height,0x14);}
        else{bigHead=false;SetBounds(desktopPlacement);}
        web.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new {type="bigHead",enabled=bigHead}));
        Dispatcher.BeginInvoke(DispatcherPriority.Loaded,new Action(()=>File.WriteAllText(Path.Combine(folder,"interaction-state.json"),JsonSerializer.Serialize(new {bigHead,left=Left,top=Top,width=ActualWidth,height=ActualHeight,modelScale,nativeCaptionDrag=dragEnabled,scaleCommitDelayMs=180}))));
    }
    private void Log(string text) => File.AppendAllText(Path.Combine(folder,"host.log"),DateTime.Now.ToString("O")+" "+text+Environment.NewLine);
    private async System.Threading.Tasks.Task Initialize()
    {
        try {
            handle=new WindowInteropHelper(this).Handle;
            HwndSource.FromHwnd(handle)?.AddHook(HitTestHook);
            var options=new CoreWebView2EnvironmentOptions(validate||debug?"--remote-debugging-address=127.0.0.1 --remote-debugging-port=9227":"");
            var env=await CoreWebView2Environment.CreateAsync(null,Path.Combine(folder,"webview-profile"),options);
            await web.EnsureCoreWebView2Async(env);
            web.CoreWebView2.Settings.AreDefaultContextMenusEnabled=false;
            web.CoreWebView2.Settings.AreDevToolsEnabled=validate||debug;
            web.CoreWebView2.NavigationStarting+=(_,e)=> {
                if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out var next)||next.Host!=url.Host||next.Port!=url.Port||next.Scheme!="http")e.Cancel=true;
            };
            web.CoreWebView2.WebMessageReceived+=(_,e)=> {
                try { using var data=JsonDocument.Parse(e.WebMessageAsJson);
                    switch(data.RootElement.GetProperty("type").GetString()) {
                        case "uiRegions":
                            menuOpen=data.RootElement.GetProperty("menuOpen").GetBoolean();
                            var immediateRegions=new System.Collections.Generic.List<Rect>();
                            foreach(var region in data.RootElement.GetProperty("regions").EnumerateArray())
                                immediateRegions.Add(new Rect(region.GetProperty("x").GetDouble(),region.GetProperty("y").GetDouble(),region.GetProperty("width").GetDouble(),region.GetProperty("height").GetDouble()));
                            hitRegions=immediateRegions.ToArray();
                            var draggable=new System.Collections.Generic.List<Rect>();
                            if(data.RootElement.TryGetProperty("dragRegions",out var dr))foreach(var region in dr.EnumerateArray())draggable.Add(new Rect(region.GetProperty("x").GetDouble(),region.GetProperty("y").GetDouble(),region.GetProperty("width").GetDouble(),region.GetProperty("height").GetDouble()));
                            dragRegions=draggable.ToArray();break;
                        case "hitMask":
                            maskWidth=data.RootElement.GetProperty("width").GetInt32();
                            maskHeight=data.RootElement.GetProperty("height").GetInt32();
                            hitMask=Convert.FromBase64String(data.RootElement.GetProperty("mask").GetString()!);
                            var regions=new System.Collections.Generic.List<Rect>();
                            foreach(var region in data.RootElement.GetProperty("regions").EnumerateArray())
                                regions.Add(new Rect(region.GetProperty("x").GetDouble(),region.GetProperty("y").GetDouble(),region.GetProperty("width").GetDouble(),region.GetProperty("height").GetDouble()));
                            hitRegions=regions.ToArray();break;
                        case "drag":if(dragEnabled&&!bigHead&&GetAsyncKeyState(1)<0){SetPassing(false);try { DragMove(); } catch(InvalidOperationException) { }}break;
                        case "bigHead":SetBigHead(data.RootElement.GetProperty("enabled").GetBoolean());break;
                        case "chat":ShowChat(env);break;
                        case "bubbleConfig":bubbleEnabled=data.RootElement.GetProperty("enabled").GetBoolean();if(!bubbleEnabled)bubble?.SetText("");break;
                        case "headAnchor":
                            var x=data.RootElement.GetProperty("x").GetDouble();var y=data.RootElement.GetProperty("y").GetDouble();
                            if(double.IsFinite(x)&&double.IsFinite(y)){
                                headAnchor=new System.Windows.Point(x,y);headAnchorVisible=data.RootElement.GetProperty("visible").GetBoolean();PositionBubble();
                            }break;
                        case "alifeControl":_ = HandleAlifeControl(data.RootElement.Clone());break;
                        case "speechBubble":
                            if(!bigHead){bubbleEnabled=true;bubble??=new SpeechBubbleWindow(this);bubble.SetText(data.RootElement.GetProperty("text").GetString()??"");PositionBubble();}break;
                        case "modelScale":
                            double percent=data.RootElement.GetProperty("percent").GetDouble();
                            if(!bigHead&&double.IsFinite(percent)){pendingScale=Math.Clamp(percent/100,.35,8);scaleTimer.Stop();scaleTimer.Start();}break;
                        case "resize":
                            double factor=data.RootElement.GetProperty("factor").GetDouble();
                            if(double.IsFinite(factor)&&factor>=.8&&factor<=1.2){
                                double center=Left+Width/2,bottom=Top+Height;
                                Width=Math.Clamp(Width*factor,560,900);Height=Math.Min(Width*780/560,SystemParameters.WorkArea.Height-20);
                                var area=SystemParameters.WorkArea;
                                Left=Math.Clamp(center-Width/2,area.Left,Math.Max(area.Left,area.Right-Width));
                                Top=Math.Max(area.Top,bottom-Height);
                            }break;
                        case "close":Close();break;
                    }
                } catch(JsonException) { }
            };
            tray=new Forms.NotifyIcon { Icon=System.Drawing.SystemIcons.Application,Text="小小伙伴 · 3D 桌宠",Visible=true };
            var menu=new Forms.ContextMenuStrip();
            menu.Items.Add("动作与角色",null,async (_,_)=> {SetPassing(false);Activate();await web.CoreWebView2.ExecuteScriptAsync("window.petTogglePanel?.()");});
            menu.Items.Add("聊天窗口",null,(_,_)=>ShowChat(env));
            menu.Items.Add("复位桌面位置",null,(_,_)=> {var area=SystemParameters.WorkArea;Left=area.Right-Width-35;Top=area.Bottom-Height-15;});
            menu.Items.Add("退出桌宠",null,(_,_)=>Close());tray.ContextMenuStrip=menu;
            tray.DoubleClick+=async (_,_)=>await web.CoreWebView2.ExecuteScriptAsync("window.petTogglePanel?.()");
            web.CoreWebView2.Navigate(url.ToString());initialized=true;timer.Start();Log("Initialized transparent desktop window.");
        } catch(Exception e) {Log(e.ToString());System.Windows.MessageBox.Show(e.Message,"桌宠启动失败");Close();}
    }
    private void ShowChat(CoreWebView2Environment env)
    {
        if(chat is null){
            chat=new ChatWindow(folder,new Uri(url,"/alife-chat.html"),env,Left,Top);
            chat.Closed+=(_,_)=>chat=null;
        }
        chat.Show();chat.Activate();
    }
    private async System.Threading.Tasks.Task HandleAlifeControl(JsonElement data)
    {
        string requestId=data.GetProperty("request_id").GetString()!;
        object result;
        try {
            string action=data.GetProperty("action").GetString()!;
            var dpi=VisualTreeHelper.GetDpi(this);
            if(action=="move") {
                double dx=data.GetProperty("x").GetDouble()/dpi.DpiScaleX,dy=data.GetProperty("y").GetDouble()/dpi.DpiScaleY;
                double seconds=data.GetProperty("seconds").GetDouble();
                if(bigHead)throw new InvalidOperationException("Restore normal pet mode before moving");
                if(!double.IsFinite(dx)||!double.IsFinite(dy)||!double.IsFinite(seconds)||seconds<0||seconds>10)throw new ArgumentException("Invalid move");
                var area=SystemParameters.WorkArea;double startX=Left,startY=Top;
                double endX=Math.Clamp(Left+dx,area.Left,Math.Max(area.Left,area.Right-ActualWidth));
                double endY=Math.Clamp(Top+dy,area.Top,Math.Max(area.Top,area.Bottom-ActualHeight));
                var clock=System.Diagnostics.Stopwatch.StartNew();
                do {double t=seconds<=0?1:Math.Min(1,clock.Elapsed.TotalSeconds/seconds);double ease=t*(2-t);Left=startX+(endX-startX)*ease;Top=startY+(endY-startY)*ease;if(t>=1)break;await System.Threading.Tasks.Task.Delay(16);}while(initialized);
            } else if(action=="subtitle") {
                string text=data.GetProperty("text").GetString()??"";
                bubbleEnabled=true;bubble??=new SpeechBubbleWindow(this);bubble.SetText(text);PositionBubble();
            } else if(action=="using") {
                // Alife thinking status is exposed in the host title without inventing an action.
                Title=data.GetProperty("enabled").GetBoolean()?"Embodied3DPet · Alife 正在思考":"Embodied3DPet · Alife";
            } else if(action!="position")throw new ArgumentException("Unknown desktop action");
            var physical=PointToScreen(new System.Windows.Point(ActualWidth/2,ActualHeight/2));
            result=new {type="alifeResult",request_id=requestId,x=physical.X,y=physical.Y,drag_enabled=dragEnabled};
        } catch(Exception e) {result=new {type="alifeResult",request_id=requestId,error=e.Message};}
        web.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(result));
    }
    private void SetPassing(bool value)
    {
        if(value==passing||handle==IntPtr.Zero)return;
        long style=GetStyle(handle,-20).ToInt64();
        SetStyle(handle,-20,new IntPtr(value?style|0x20:style&~0x20));passing=value;
    }
    private void PositionBubble()
    {
        if(bubble is null||!initialized||PresentationSource.FromVisual(this) is null)return;
        if(!bubbleEnabled||bigHead||!headAnchorVisible){bubble.Hide();return;}
        var screenPoint=PointToScreen(headAnchor);var dpi=VisualTreeHelper.GetDpi(this);
        var area=Forms.Screen.FromHandle(handle).WorkingArea;
        bubble.Place(new System.Windows.Point(screenPoint.X/dpi.DpiScaleX,screenPoint.Y/dpi.DpiScaleY),
            new Rect(area.Left/dpi.DpiScaleX,area.Top/dpi.DpiScaleY,area.Width/dpi.DpiScaleX,area.Height/dpi.DpiScaleY));
    }
    private async System.Threading.Tasks.Task Poll()
    {
        if(polling||!initialized)return;polling=true;
        bubble?.Expire();
        try {
            if(menuOpen&&GetCursorPos(out var cursor)&&!new Rect(-4,-4,ActualWidth+8,ActualHeight+8).Contains(PointFromScreen(new System.Windows.Point(cursor.X,cursor.Y)))){
                menuOpen=false;await web.CoreWebView2.ExecuteScriptAsync("window.petCloseMenu?.()");
            }
            if(validate) {
                ticks++;
                var result=await web.CoreWebView2.ExecuteScriptAsync("window.petDiagnostics?.() ?? null");
                if(result!="null") {
                    using var data=JsonDocument.Parse(result);
                    if(data.RootElement.GetProperty("loaded").GetBoolean()&&data.RootElement.GetProperty("ready").GetBoolean()) {
                        // Allow buffered facial state and smoothing to reach the rendered mesh.
                        if(++validationReadyTicks<8)return;
                        if(validateReply&&bubble is null)return;
                        if(validateBubble){
                            bubble??=new SpeechBubbleWindow(this);if(!validateReply)bubble.SetText("我正在查看当前动作和后续计划，保持原计划继续执行。这个独立气泡会跟随角色头顶，支持中文换行显示。");PositionBubble();
                            if(bigHead)bubble.Hide();
                            File.WriteAllText(Path.Combine(folder,"bubble-validation.json"),JsonSerializer.Serialize(new {enabled=bubbleEnabled,visible=bubble.IsVisible,bigHead,anchorVisible=headAnchorVisible,headX=headAnchor.X,headY=headAnchor.Y,left=double.IsFinite(bubble.Left)?bubble.Left:0,top=double.IsFinite(bubble.Top)?bubble.Top:0,width=bubble.ActualWidth,height=bubble.ActualHeight,ownerLeft=Left,ownerTop=Top,eyeMicro=data.RootElement.GetProperty("eyeMicro")}));
                            if(bubble.IsVisible){var bitmapBubble=new RenderTargetBitmap((int)Math.Ceiling(bubble.ActualWidth),(int)Math.Ceiling(bubble.ActualHeight),96,96,PixelFormats.Pbgra32);bitmapBubble.Render(bubble);var bubbleEncoder=new PngBitmapEncoder();bubbleEncoder.Frames.Add(BitmapFrame.Create(bitmapBubble));using(var bubbleImage=File.Create(Path.Combine(folder,"bubble.png")))bubbleEncoder.Save(bubbleImage);}
                        }
                        File.WriteAllText(Path.Combine(folder,"validation.json"),result);
                        using(var image=File.Create(Path.Combine(folder,"webview.png")))await web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,image);
                        var bitmap=new RenderTargetBitmap((int)ActualWidth,(int)ActualHeight,96,96,PixelFormats.Pbgra32);bitmap.Render(this);
                        var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(bitmap));
                        using(var image=File.Create(Path.Combine(folder,"window.png")))encoder.Save(image);
                        Log("Validation captured.");Close();
                    }
                }
                if(ticks>1000){Log("Validation timed out.");Close();}
            }
        } catch(Exception e){Log(e.Message);}finally{polling=false;}
    }
    private IntPtr HitTestHook(IntPtr hwnd,int message,IntPtr wParam,IntPtr lParam,ref bool handled)
    {
        if(PresentationSource.FromVisual(this) is null)return IntPtr.Zero;

        if(message==0xA3){handled=true;return IntPtr.Zero;} // Do not maximize on an avatar double click.
        if(message!=0x84&&message!=0xA5&&message!=0x20A)return IntPtr.Zero;
        var value=lParam.ToInt64();
        var p=PointFromScreen(new System.Windows.Point((short)(value&0xffff),(short)((value>>16)&0xffff)));
        bool ui=false;
        foreach(var region in hitRegions)if(region.Contains(p)){ui=true;break;}
        bool avatar=false;
        if(ActualWidth>0&&ActualHeight>0&&maskWidth>0&&maskHeight>0){
            int x=(int)(p.X/ActualWidth*maskWidth),y=(int)(p.Y/ActualHeight*maskHeight);
            int index=y*maskWidth+x;
            avatar=x>=0&&x<maskWidth&&y>=0&&y<maskHeight&&index>=0&&index<hitMask.Length&&hitMask[index]!=0;
        }
        if(message==0xA5&&avatar&&!bigHead){handled=true;_ = web.CoreWebView2?.ExecuteScriptAsync($"window.petOpenMenuAt?.({p.X.ToString(System.Globalization.CultureInfo.InvariantCulture)},{p.Y.ToString(System.Globalization.CultureInfo.InvariantCulture)})");return IntPtr.Zero;}
        if(message==0x20A&&avatar&&!ui&&!bigHead){
            var delta=(short)((wParam.ToInt64()>>16)&0xffff);
            pendingScale=Math.Clamp(pendingScale*Math.Pow(1.06,delta/120.0),.35,8);
            web.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new {type="scalePreview",percent=pendingScale*100}));
            scaleTimer.Stop();scaleTimer.Start();handled=true;return IntPtr.Zero;
        }
        if(message!=0x84)return IntPtr.Zero;
        handled=true;
        if(bigHead)return new IntPtr(ui||avatar?1:-1);
        foreach(var region in dragRegions)if(region.Contains(p))return new IntPtr(2);
        return new IntPtr(ui?1:avatar?(dragEnabled?2:1):-1); // Native caption dragging starts from the original mouse-down.

    }
}

internal sealed class ChatWindow : Window
{
    private readonly WebView2CompositionControl web=new();
    private bool initialized;
    public ChatWindow(string folder,Uri url,CoreWebView2Environment env,double petLeft,double petTop)
    {
        Title="小小伙伴 · 聊天";Width=820;Height=680;MinWidth=600;MinHeight=420;
        WindowStyle=WindowStyle.None;ResizeMode=ResizeMode.CanResizeWithGrip;Topmost=true;ShowInTaskbar=false;
        Background=new SolidColorBrush(Color.FromRgb(248,245,251));Content=web;web.UseLayoutRounding=false;
        var area=SystemParameters.WorkArea;Left=Math.Clamp(petLeft-Width-12,area.Left,Math.Max(area.Left,area.Right-Width));Top=Math.Clamp(petTop,area.Top,Math.Max(area.Top,area.Bottom-Height));
        try {var state=JsonDocument.Parse(File.ReadAllText(Path.Combine(folder,"chat-placement.json"))).RootElement;
            Width=Math.Clamp(state.GetProperty("width").GetDouble(),520,1100);Height=Math.Clamp(state.GetProperty("height").GetDouble(),420,1000);
            Left=Math.Clamp(state.GetProperty("left").GetDouble(),area.Left,Math.Max(area.Left,area.Right-Width));Top=Math.Clamp(state.GetProperty("top").GetDouble(),area.Top,Math.Max(area.Top,area.Bottom-Height));
        }catch { }
        Loaded+=async (_,_)=>{
            if(initialized)return;initialized=true;
            await web.EnsureCoreWebView2Async(env);
            web.CoreWebView2.Settings.AreDefaultContextMenusEnabled=false;
            web.CoreWebView2.NavigationStarting+=(_,e)=>{if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out var next)||next.Host!=url.Host||next.Port!=url.Port||next.Scheme!="http")e.Cancel=true;};
            web.CoreWebView2.WebMessageReceived+=(_,e)=>{
                using var message=JsonDocument.Parse(e.WebMessageAsJson);
                switch(message.RootElement.GetProperty("type").GetString()){
                    case "dragChat":try{DragMove();}catch(InvalidOperationException){}break;
                    case "closeChat":Save();Hide();break;
                }
            };
            web.CoreWebView2.Navigate(url.ToString());
        };
        void Save()=>File.WriteAllText(Path.Combine(folder,"chat-placement.json"),JsonSerializer.Serialize(new{left=Left,top=Top,width=Width,height=Height}));
        Closed+=(_,_)=>{Save();web.Dispose();};
    }
}
