using System;
using System.IO;
using System.Drawing;
using System.Windows.Forms;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class CaptionButton : Button {
    readonly string action;
    public bool Maximized;
    public CaptionButton(string kind) {
        action=kind;bool close=kind=="close";Text="";AccessibleName=close?"关闭窗口":kind=="maximize"?"最大化窗口":"最小化窗口";
        Width=40;Height=32;Margin=new Padding(2,2,2,0);
        FlatStyle=FlatStyle.Flat;FlatAppearance.BorderSize=0;
        FlatAppearance.MouseOverBackColor=close?Color.FromArgb(196,43,55):Color.FromArgb(35,43,47);
        FlatAppearance.MouseDownBackColor=close?Color.FromArgb(156,30,40):Color.FromArgb(49,60,65);
        ForeColor=Color.FromArgb(222,233,238);BackColor=Color.FromArgb(1,2,3);
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        float scale=DeviceDpi/96f,cx=ClientSize.Width/2f,cy=ClientSize.Height/2f,r=5*scale;
        e.Graphics.SmoothingMode=System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
        using(var pen=new Pen(ForeColor,1.2f*scale)) {
            if(action=="close"){e.Graphics.DrawLine(pen,cx-r,cy-r,cx+r,cy+r);e.Graphics.DrawLine(pen,cx+r,cy-r,cx-r,cy+r);}
            else if(action=="maximize"){
                if(Maximized){
                    float d=2*scale;
                    e.Graphics.DrawLines(pen,new[]{new PointF(cx-r+d,cy-r+d),new PointF(cx-r+d,cy-r),new PointF(cx+r,cy-r),new PointF(cx+r,cy+r-d),new PointF(cx+r-d,cy+r-d)});
                    e.Graphics.DrawRectangle(pen,cx-r,cy-r+d,2*r-d,2*r-d);
                }else e.Graphics.DrawRectangle(pen,cx-r,cy-r,2*r,2*r);
            }
            else e.Graphics.DrawLine(pen,cx-r,cy,cx+r,cy);
        }
    }
}

// Separate native surfaces prevent DWM accent blur from spilling across panel gaps.
internal sealed class DesktopBlurPanel : Form {
    public bool BlurApplied;
    public DesktopBlurPanel(){FormBorderStyle=FormBorderStyle.None;ShowInTaskbar=false;StartPosition=FormStartPosition.Manual;BackColor=Color.Black;AutoScaleMode=AutoScaleMode.None;}
    protected override bool ShowWithoutActivation{get{return true;}}
    protected override CreateParams CreateParams{get{var p=base.CreateParams;p.ClassStyle&=~0x20000;p.ExStyle|=0x08000000|0x80|0x20;return p;}}
    protected override void OnHandleCreated(EventArgs e){
        base.OnHandleCreated(e);
        int corner=1;DwmSetWindowAttribute(Handle,33,ref corner,4);
        int policy=1;DwmSetWindowAttribute(Handle,2,ref policy,4);
        RefreshBlur();
    }
    public void RefreshBlur(){
        var accent=new AccentPolicy{State=3};var memory=Marshal.AllocHGlobal(Marshal.SizeOf(accent));
        try{Marshal.StructureToPtr(accent,memory,false);var data=new CompositionData{Attribute=19,Data=memory,Size=new IntPtr(Marshal.SizeOf(accent))};BlurApplied=SetWindowCompositionAttribute(Handle,ref data);}
        catch(EntryPointNotFoundException){BlurApplied=false;}
        finally{Marshal.FreeHGlobal(memory);}
    }
    protected override void OnSizeChanged(EventArgs e){
        base.OnSizeChanged(e);
        var previous=Region;Region=new Region(new Rectangle(0,0,Width,Height));if(previous!=null)previous.Dispose();
    }
    // Leave the surface to DWM. A normal WinForms background paint would cover
    // the accent with opaque black after its first frame.
    protected override void OnPaintBackground(PaintEventArgs e){}
    public void Place(IntPtr after,Rectangle r){
        SetWindowPos(Handle,after,r.X,r.Y,r.Width,r.Height,0x10|0x40);
        RefreshBlur();
    }
    protected override void WndProc(ref Message m){if(m.Msg==0x84){m.Result=new IntPtr(-1);return;}base.WndProc(ref m);}
    [StructLayout(LayoutKind.Sequential)] struct AccentPolicy{public int State,Flags,Color,Animation;}
    [StructLayout(LayoutKind.Sequential)] struct CompositionData{public int Attribute;public IntPtr Data,Size;}
    [DllImport("user32.dll")] [return:MarshalAs(UnmanagedType.Bool)] static extern bool SetWindowCompositionAttribute(IntPtr window,ref CompositionData data);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int w,int h,uint flags);
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr window,int attribute,ref int value,int size);
}
internal sealed class DesktopBlurLayer : IDisposable {
    readonly System.Collections.Generic.List<DesktopBlurPanel> panels=new System.Collections.Generic.List<DesktopBlurPanel>();
    int[][] regions=new int[0][];Point offset;
    public bool IsDisposed{get;private set;}
    public bool Disposing{get{return IsDisposed;}}
    public bool Visible{get;private set;}
    public Rectangle Bounds{get;private set;}
    public IntPtr Handle{get{return panels.Count==0?IntPtr.Zero:panels[panels.Count-1].Handle;}}
    public bool BlurApplied{get{return panels.Count>0 && panels.TrueForAll(p=>p.BlurApplied);}}
    public void SetPanels(int[][] value,Point origin){regions=value;offset=origin;}
    public void Show(){if(!IsDisposed)Visible=true;}
    public void Hide(){
        Visible=false;
        if(IsDisposed)return;
        // Accent surfaces can retain a frozen DWM backdrop after the host has
        // switched to an opaque maximized surface. Retire their HWNDs when the
        // effect is suspended; restoration must create fresh compositor surfaces.
        foreach(var panel in panels)panel.Dispose();
        panels.Clear();
    }
    public void PlaceBehind(Form host){
        if(IsDisposed || !Visible)return;
        Bounds=host.RectangleToScreen(host.ClientRectangle);
        while(panels.Count<regions.Length)panels.Add(new DesktopBlurPanel());
        while(panels.Count>regions.Length){var last=panels[panels.Count-1];panels.RemoveAt(panels.Count-1);last.Dispose();}
        IntPtr after=host.Handle;
        for(int i=0;i<regions.Length;i++){
            var r=regions[i];var panel=panels[i];
            if(r.Length<5 || r[2]<4 || r[3]<4){panel.Hide();continue;}
            var bounds=new Rectangle(Bounds.X+r[0]+offset.X+2,Bounds.Y+r[1]+offset.Y+2,r[2]-4,r[3]-4);
            if(!panel.Visible){panel.Bounds=bounds;panel.Show();}
            panel.Place(after,bounds);after=panel.Handle;
        }
    }
    public void Dispose(){if(IsDisposed)return;IsDisposed=true;Visible=false;foreach(var panel in panels)panel.Dispose();panels.Clear();}
}

internal sealed class DesktopShell : Form {
    readonly StudioService service = new StudioService();
    readonly WebView2 web = new WebView2();
    readonly DesktopBlurLayer blurLayer = new DesktopBlurLayer();
    int[][] panelRegions = new int[0][];
    bool lightTheme;
    readonly CancellationTokenSource startup = new CancellationTokenSource();
    readonly TableLayoutPanel loading = new TableLayoutPanel();
    readonly Label message = new Label();
    readonly ToolTip captionTips = new ToolTip();
    readonly CaptionButton loadingMaximize = new CaptionButton("maximize");
    readonly bool smoke;
    readonly uint activateMessage;
    bool starting = true, allowClose, smokeStarted, smokeDownloadComplete, documentLoaded, transparentHost, glassMode, maximizedState;
    public int TestResult = 1;
    public DesktopShell(bool test, uint messageId) {
        smoke=test;activateMessage=messageId;
        service.LogName="desktop-server.log";service.InstanceFile="desktop-instance.json";
        Text="DLSS Studio";FormBorderStyle=FormBorderStyle.None;
        StartPosition=FormStartPosition.CenterScreen;Size=new Size(1320,820);MinimumSize=new Size(980,700);
        BackColor=Color.FromArgb(1,2,3);Padding=new Padding(4);
        Font=new Font("Microsoft YaHei UI",10);AutoScaleMode=AutoScaleMode.Dpi;
        web.Dock=DockStyle.Fill;web.DefaultBackgroundColor=Color.Transparent;Controls.Add(web);
        loading.Dock=DockStyle.Fill;loading.BackColor=BackColor;loading.Margin=Padding.Empty;loading.Padding=Padding.Empty;
        loading.ColumnCount=1;loading.RowCount=2;loading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        loading.RowStyles.Add(new RowStyle(SizeType.Absolute,40));loading.RowStyles.Add(new RowStyle(SizeType.Percent,100));Controls.Add(loading);loading.BringToFront();
        message.Text="DLSS Studio\n\n正在准备工作台…";message.ForeColor=Color.White;
        message.Dock=DockStyle.Fill;message.TextAlign=ContentAlignment.MiddleCenter;
        message.Font=new Font(Font.FontFamily,19);message.Margin=Padding.Empty;loading.Controls.Add(message,0,1);
        var caption=new FlowLayoutPanel{Dock=DockStyle.Fill,Margin=Padding.Empty,Height=40,FlowDirection=FlowDirection.RightToLeft,BackColor=BackColor};
        var close=new CaptionButton("close");
        var minimize=new CaptionButton("minimize");
        captionTips.SetToolTip(close,"关闭");captionTips.SetToolTip(minimize,"最小化");captionTips.SetToolTip(loadingMaximize,"最大化");
        close.Click+=(s,e)=>Close();minimize.Click+=(s,e)=>WindowState=FormWindowState.Minimized;
        loadingMaximize.Click+=(s,e)=>ToggleMaximize();
        caption.Controls.Add(close);caption.Controls.Add(loadingMaximize);caption.Controls.Add(minimize);loading.Controls.Add(caption,0,0);
        caption.MouseDown+=(s,e)=>{if(e.Button==MouseButtons.Left)DragWindow();};
        Resize+=(s,e)=>UpdateWindowState();
        Shown+=async(s,e)=>{
            if(smoke){
                using(var snapshot=new Bitmap(loading.Width,loading.Height)){
                    loading.DrawToBitmap(snapshot,new Rectangle(Point.Empty,loading.Size));
                    snapshot.Save(Path.Combine(service.Root,"desktop-preparing.png"),System.Drawing.Imaging.ImageFormat.Png);
                }
            }
            await Initialize();
        };
        FormClosing+=OnClosing;
        FormClosed+=(s,e)=>{documentLoaded=false;blurLayer.Dispose();captionTips.Dispose();web.Dispose();service.Dispose();startup.Dispose();};
        if(smoke)ShowInTaskbar=false;
    }
    async Task Initialize() {
        try {
            bool missingRuntime=false;
            try { CoreWebView2Environment.GetAvailableBrowserVersionString(); }
            catch(WebView2RuntimeNotFoundException) { missingRuntime=true; }
            if(missingRuntime) {
                if(smoke)throw new InvalidOperationException("WebView2 Runtime not installed");
                var choice=MessageBox.Show(this,"独立窗口需要 Microsoft WebView2。现在安装此组件？首次安装需要联网。","安装浏览器组件",MessageBoxButtons.YesNo,MessageBoxIcon.Information);
                if(choice!=DialogResult.Yes){allowClose=true;Close();return;}
                message.Text="正在安装 WebView2…\n请保持网络连接";
                var installer=Path.Combine(service.Root,"components","MicrosoftEdgeWebview2Setup.exe");
                using(var proc=Process.Start(new ProcessStartInfo(installer,"/silent /install"){UseShellExecute=false,CreateNoWindow=true})) {
                    await Task.Run(()=>proc.WaitForExit());
                    if(proc.ExitCode!=0)throw new IOException("WebView2 安装未完成，请检查网络后重试。");
                }
            }
            startup.Token.ThrowIfCancellationRequested();
            await service.Start(startup.Token);
            startup.Token.ThrowIfCancellationRequested();
            var environment=await CoreWebView2Environment.CreateAsync(null,Path.Combine(service.Root,smoke?"desktop-smoke-profile":"desktop-profile"));
            await web.EnsureCoreWebView2Async(environment);
            startup.Token.ThrowIfCancellationRequested();
            var core=web.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled=false;
            core.Settings.AreDevToolsEnabled=false;
            core.Settings.IsStatusBarEnabled=false;
            core.Settings.AreBrowserAcceleratorKeysEnabled=true;
            core.Settings.IsZoomControlEnabled=true;
            core.Settings.AreHostObjectsAllowed=false;
            core.Settings.IsPasswordAutosaveEnabled=false;
            core.Settings.IsGeneralAutofillEnabled=false;
            await core.AddScriptToExecuteOnDocumentCreatedAsync("window.__DLSS_DESKTOP__=true;");
            if(smoke)await core.Profile.ClearBrowsingDataAsync(CoreWebView2BrowsingDataKinds.LocalStorage);
            if(smoke)await core.AddScriptToExecuteOnDocumentCreatedAsync("window.__appErrors=[];window.addEventListener('error',e=>{if(e.message)window.__appErrors.push(e.message)});window.addEventListener('unhandledrejection',e=>window.__appErrors.push(String(e.reason)));");
            core.NavigationStarting+=(s,e)=>{if(!OwnOrigin(e.Uri))e.Cancel=true;};
            core.NewWindowRequested+=(s,e)=>{e.Handled=true;};
            core.PermissionRequested+=(s,e)=>{e.State=CoreWebView2PermissionState.Deny;};
            if(smoke)core.DownloadStarting+=(s,e)=>{
                e.ResultFilePath=Path.Combine(service.Root,"desktop-downloaded-result.png");e.Handled=true;
                e.DownloadOperation.StateChanged+=(sender,args)=>{if(e.DownloadOperation.State==CoreWebView2DownloadState.Completed)smokeDownloadComplete=true;};
            };
            core.WebMessageReceived+=(s,e)=>{
                if(!OwnOrigin(e.Source))return;
                string action;try{action=e.TryGetWebMessageAsString();}catch{return;}
                if(action=="minimize")WindowState=FormWindowState.Minimized;
                else if(action=="open-browser") {
                    try { Process.Start(new ProcessStartInfo(service.Url){UseShellExecute=true}); }
                    catch(Exception ex){MessageBox.Show(this,ex.Message,"无法打开浏览器");}
                }
                else if(action=="close")Close();
                else if(action=="drag")DragWindow();
                else if(action=="maximize")ToggleMaximize();
                else if(action=="window-state")NotifyWindowState();
                else if(action.StartsWith("blur-regions:")){
                    try{panelRegions=new JavaScriptSerializer().Deserialize<int[][]>(action.Substring(13));blurLayer.SetPanels(panelRegions,web.Location);UpdateBlurLayer();}catch(ArgumentException){}
                }
                else if(action=="theme:light" || action=="theme:dark"){
                    lightTheme=action=="theme:light";if(documentLoaded&&maximizedState)ApplyGlassMode();
                }
                else if(action=="glass:on" || action=="glass:off"){
                    bool enabled=action=="glass:on";
                    if(glassMode!=enabled){glassMode=enabled;if(documentLoaded)ApplyGlassMode();}
                }
            };
            core.NavigationCompleted+=async(s,e)=>{
                // File downloads can finish a navigation with a cancellation status.
                // Keep an already loaded workbench visible in that case.
                if(!e.IsSuccess){if(!documentLoaded){message.Text="工作台加载失败\n请关闭窗口后重试";loading.Show();}return;}
                documentLoaded=true;
                ApplyGlassMode();
                NotifyWindowState();
                loading.Hide();
                if(smoke && !smokeStarted){smokeStarted=true;await SmokeTest();}
            };
            core.ProcessFailed+=(s,e)=>{if(!IsDisposed){message.Text="浏览器组件已退出\n请重新打开 DLSS Studio";loading.Show();}};
            core.Navigate(service.Url);
        } catch(OperationCanceledException) { allowClose=true;Close(); }
        catch(Exception ex) {
            service.Stop();
            try{Directory.CreateDirectory(service.LogDir);File.AppendAllText(Path.Combine(service.LogDir,"desktop.log"),DateTime.Now+" "+ex+Environment.NewLine);}catch{}
            if(smoke){File.WriteAllText(Path.Combine(service.Root,"desktop-smoke-result.txt"),ex.ToString());allowClose=true;Close();}
            else message.Text="无法打开工作台\n\n"+ex.Message+"\n\n详情见 logs/desktop.log";
        } finally { starting=false; }
    }
    bool OwnOrigin(string value){Uri uri;return Uri.TryCreate(value,UriKind.Absolute,out uri)&&uri.Scheme=="http"&&uri.Host=="127.0.0.1"&&uri.Port==service.Port;}
    void EnableDesktopTransparency(){
        // Keep per-pixel WebView alpha, including shadows, instead of removing a key color.
        var region=CreateRectRgn(0,0,-1,-1);
        try{
            var blur=new DwmBlurBehind{Flags=3,Enable=true,Region=region};
            transparentHost=DwmEnableBlurBehindWindow(Handle,ref blur)==0;
        }finally{DeleteObject(region);}
        if(transparentHost){BackColor=Color.Black;Invalidate();}
    }
    void UpdateWindowState(){
        // Minimizing preserves whether the window will return maximized.
        if(WindowState==FormWindowState.Minimized)return;
        bool maximized=WindowState==FormWindowState.Maximized;
        loadingMaximize.Maximized=maximized;loadingMaximize.AccessibleName=maximized?"还原窗口":"最大化窗口";
        captionTips.SetToolTip(loadingMaximize,maximized?"还原":"最大化");loadingMaximize.Invalidate();
        if(maximizedState==maximized)return;
        maximizedState=maximized;
        if(documentLoaded){ApplyGlassMode();NotifyWindowState();}
    }
    void NotifyWindowState(){
        if(!IsDisposed && web.CoreWebView2!=null)web.CoreWebView2.PostWebMessageAsJson(new JavaScriptSerializer().Serialize(new {type="window-state",maximized=maximizedState}));
    }
    void ApplyGlassMode(){
        if(maximizedState){
            blurLayer.Hide();
            var blur=new DwmBlurBehind{Flags=1,Enable=false};DwmEnableBlurBehindWindow(Handle,ref blur);transparentHost=false;
            web.DefaultBackgroundColor=lightTheme?Color.White:Color.Black;BackColor=web.DefaultBackgroundColor;
        }else{web.DefaultBackgroundColor=Color.Transparent;EnableDesktopTransparency();}
        UpdateBlurLayer();
    }
    void UpdateBlurLayer(){
        if(IsDisposed || Disposing || blurLayer.IsDisposed || blurLayer.Disposing)return;
        if(!documentLoaded || !glassMode || maximizedState || WindowState==FormWindowState.Minimized || !Visible || panelRegions.Length==0){blurLayer.Hide();return;}
        if(!blurLayer.Visible)blurLayer.Show();
        blurLayer.PlaceBehind(this);
    }
    protected override CreateParams CreateParams{
        get{var style=base.CreateParams;style.ExStyle&=~0x20;return style;}
    }
    void DragWindow(){ReleaseCapture();SendMessage(Handle,0xA1,new IntPtr(2),IntPtr.Zero);}
    void ToggleMaximize(){MaximizedBounds=Screen.FromHandle(Handle).WorkingArea;WindowState=WindowState==FormWindowState.Maximized?FormWindowState.Normal:FormWindowState.Maximized;}
    void OnClosing(object sender,FormClosingEventArgs e){
        if(allowClose)return;
        if(starting){e.Cancel=true;startup.Cancel();return;}
        if(service.Busy() && MessageBox.Show(this,"关闭窗口会中断正在处理的任务，确定关闭？","关闭 DLSS Studio",MessageBoxButtons.YesNo,MessageBoxIcon.Question)!=DialogResult.Yes)e.Cancel=true;
    }
    protected override void WndProc(ref Message m){
        if((uint)m.Msg==activateMessage){if(WindowState==FormWindowState.Minimized)WindowState=maximizedState?FormWindowState.Maximized:FormWindowState.Normal;Show();Activate();SetForegroundWindow(Handle);return;}
        base.WndProc(ref m);
        if((m.Msg==0x47 || m.Msg==0x06) && documentLoaded)UpdateBlurLayer();
        if(m.Msg==0x31E && documentLoaded)ApplyGlassMode();
        if(m.Msg==0x84){
            var p=PointToClient(new Point(unchecked((short)m.LParam.ToInt64()),unchecked((short)(m.LParam.ToInt64()>>16))));
            int grip=4;bool left=p.X<grip,right=p.X>=ClientSize.Width-grip,top=p.Y<grip,bottom=p.Y>=ClientSize.Height-grip;
            int hit=WindowState==FormWindowState.Normal?(top?(left?13:right?14:12):bottom?(left?16:right?17:15):left?10:right?11:0):0;
            // Transparent pixels still belong to this window; never return HTTRANSPARENT.
            if(ClientRectangle.Contains(p))m.Result=new IntPtr(hit!=0?hit:1);
        }
    }
    async Task<bool> Check(string expression,int seconds){
        for(int i=0;i<seconds*4;i++){if(await web.CoreWebView2.ExecuteScriptAsync("Boolean("+expression+")")=="true")return true;await Task.Delay(250);}return false;
    }
    async Task SmokeTest(){
        try {
            var core=web.CoreWebView2;
            if(!await Check("document.querySelector('.desktop-window-controls') && !document.querySelector('#empty-state').hidden",20))throw new Exception("Native controls or upload area missing");
            await core.ExecuteScriptAsync("if(document.documentElement.dataset.glass==='on')document.querySelector('#glass-toggle').click()");
            await Task.Delay(150);
            if(!transparentHost || !await Check("getComputedStyle(document.documentElement).backgroundColor==='rgba(0, 0, 0, 0)' && getComputedStyle(document.body).backgroundColor==='rgba(0, 0, 0, 0)'",3))throw new Exception("Desktop background is not transparent");
            var normalSize=Size;Size=MinimumSize;await Task.Delay(250);
            if(!await Check("document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight && document.querySelector('.window-close').getBoundingClientRect().right<=innerWidth && document.querySelector('.inspector').scrollHeight<=document.querySelector('.inspector').clientHeight && document.querySelector('.open-browser')",3))throw new Exception("Compact window overflow");
            if(!await Check("document.querySelector('#glass-toggle').getBoundingClientRect().right<document.querySelector('.theme-switch').getBoundingClientRect().left && document.querySelector('.mode-tabs').getBoundingClientRect().right+6<document.querySelector('.desktop-actions').getBoundingClientRect().left && document.querySelector('.engine-status').getBoundingClientRect().right+6<document.querySelector('.mode-tabs').getBoundingClientRect().left",3))throw new Exception("Compact appearance controls overlap navigation");
            await core.ExecuteScriptAsync("document.querySelector('[data-mode=video]').click();document.querySelector('#video-settings details:last-child summary').click()");
            if(!await Check("document.querySelector('#settings-dialog').open && document.querySelector('#settings-dialog').scrollHeight<=document.querySelector('#settings-dialog').clientHeight && document.querySelector('#settings-form').elements.codec",3))throw new Exception("Video settings dialog overflow or form association lost");
            await core.ExecuteScriptAsync("document.querySelector('#settings-dialog .done').click()");
            await Task.Delay(100);
            if(!await Check("document.querySelector('.inspector').scrollHeight<=document.querySelector('.inspector').clientHeight",3))throw new Exception("Video inspector overflow");
            await core.ExecuteScriptAsync("document.querySelector('[data-mode=image]').click();document.querySelector('.advanced summary').click()");
            if(!await Check("document.querySelector('#settings-dialog').open && document.querySelector('#settings-form').elements.local_structure",3))throw new Exception("Advanced settings dialog failed");
            await core.ExecuteScriptAsync("document.querySelector('#settings-dialog .done').click()");
            await Task.Delay(100);
            Size=normalSize;await Task.Delay(250);
            await core.ExecuteScriptAsync("document.querySelector('[data-theme=light][type=button]').click()");
            if(!await Check("document.documentElement.dataset.theme==='light'",5))throw new Exception("Light theme failed");
            using(var file=File.Create(Path.Combine(service.Root,"desktop-light.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
            await core.ExecuteScriptAsync("document.querySelector('[data-theme=dark][type=button]').click()");
            if(!await Check("document.documentElement.dataset.theme==='dark'",5))throw new Exception("Dark theme failed");
            using(var file=File.Create(Path.Combine(service.Root,"desktop-dark.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
            TopMost=true;await VerifyDesktopTransparency();
            await VerifyGlassMode();
            await VerifyMaximizedMode();TopMost=false;
            if(!await Check("document.documentElement.scrollWidth<=innerWidth",2))throw new Exception("Horizontal overflow");
            // Exercise the actual upload input and processing button inside the embedded browser.
            await core.ExecuteScriptAsync("(async()=>{const b=await(await fetch('/assets/sample-lake.png')).blob();const d=new DataTransfer();d.items.add(new File([b],'desktop-test.png',{type:'image/png'}));const i=document.querySelector('#file-input');i.files=d.files;i.dispatchEvent(new Event('change',{bubbles:true}));})()");
            if(!await Check("!document.querySelector('#start-button').disabled",20))throw new Exception("Upload failed");
            await core.ExecuteScriptAsync("document.querySelector('#start-button').click()");
            if(!await Check("document.querySelector('#status-text').textContent==='处理完成'",100))throw new Exception("Embedded rendering failed");
            await core.ExecuteScriptAsync("document.querySelector('[data-view=compare]').click();document.querySelector('#close-notice').click()");
            if(!await Check("!document.querySelector('#compare-slider').hidden",5))throw new Exception("Compare mode failed");
            using(var file=File.Create(Path.Combine(service.Root,"desktop-result.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
            await core.ExecuteScriptAsync("document.querySelector('#download').click()");
            for(int i=0;i<80&&!smokeDownloadComplete;i++)await Task.Delay(250);
            if(!smokeDownloadComplete || new FileInfo(Path.Combine(service.Root,"desktop-downloaded-result.png")).Length<1024)throw new Exception("Download failed");
            if(loading.Visible)throw new Exception("Download hid the workbench");
            await VerifyVideoWorkflow();
            await VerifyWorkbenchWorkflow();
            await core.ExecuteScriptAsync("document.querySelector('.window-minimize').click()");await Task.Delay(300);
            if(WindowState!=FormWindowState.Minimized)throw new Exception("Minimize command failed");
            WindowState=FormWindowState.Normal;
            if(OwnOrigin("https://example.com/")||OwnOrigin("file:///C:/"))throw new Exception("Origin validation failed");
            File.WriteAllText(Path.Combine(service.Root,"desktop-smoke-result.txt"),"PASS: panel-clipped native background blur, clear normal-window gaps and unchanged borders; maximized black/white opaque canvas with glass disabled and restored preference; input hit testing; transparent desktop composition; borderless WebView2; compact image/video layout without overflow; settings dialogs and form fields; dark/light themes; real upload and render; compare; download; minimize; close requested; service_port="+service.Port);
            TestResult=0;
            await core.ExecuteScriptAsync("setTimeout(()=>document.querySelector('.window-close').click(),100)");
        } catch(Exception ex){File.WriteAllText(Path.Combine(service.Root,"desktop-smoke-result.txt"),ex.ToString());allowClose=true;Close();}
    }
    async Task VerifyVideoWorkflow(){
        var fixture=Environment.GetEnvironmentVariable("DLSS_STUDIO_TEST_VIDEO");
        if(String.IsNullOrEmpty(fixture))return;
        var core=web.CoreWebView2;
        var script=Path.GetFullPath(Path.Combine(Path.GetDirectoryName(fixture),"../video-workflow.js"));
        await core.ExecuteScriptAsync("window.__videoFixture="+new JavaScriptSerializer().Serialize(Convert.ToBase64String(File.ReadAllBytes(fixture)))+";");
        await core.ExecuteScriptAsync(File.ReadAllText(script));
        foreach(var phase in new[]{"frame","compare"}){
            if(!await Check("window.__videoTest && (window.__videoTest.phase==='"+phase+"' || window.__videoTest.error)",240))throw new Exception("Video workflow timed out: "+phase);
            var error=await core.ExecuteScriptAsync("window.__videoTest.error");
            if(error!="null")throw new Exception("Video workflow: "+error);
            using(var file=File.Create(Path.Combine(service.Root,"desktop-video-"+phase+".png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
            if(phase=="compare"){
                var previous=Size;Size=MinimumSize;await Task.Delay(250);
                if(!await Check("document.documentElement.scrollWidth<=innerWidth && document.querySelector('#video-stage').scrollWidth<=document.querySelector('#video-stage').clientWidth && ['#trim-start','#trim-end'].every(s=>{const r=document.querySelector(s).getBoundingClientRect(),c=document.querySelector('#canvas').getBoundingClientRect();return r.left>=c.left&&r.right<=c.right}) && !document.querySelector('#frame-test-button')",3))throw new Exception("Compact video transport overflow");
                using(var file=File.Create(Path.Combine(service.Root,"desktop-video-compact.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
                Size=previous;
            }
            await core.ExecuteScriptAsync("window.__videoTest.resume=true");
        }
        if(!await Check("window.__videoTest.phase==='done'",5))throw new Exception("Video workflow did not finish");
        File.WriteAllText(Path.Combine(service.Root,"desktop-video-diagnostics.json"),await core.ExecuteScriptAsync("window.__videoTest.diagnostics"));
    }
    async Task VerifyWorkbenchWorkflow(){
        var script=Environment.GetEnvironmentVariable("DLSS_STUDIO_TEST_WORKBENCH");
        if(String.IsNullOrEmpty(script))return;
        var core=web.CoreWebView2;
        await core.ExecuteScriptAsync(File.ReadAllText(script));
        if(!await Check("window.__workbenchTest && (window.__workbenchTest.done || window.__workbenchTest.error)",150))throw new Exception("Workbench regression timed out");
        var error=await core.ExecuteScriptAsync("window.__workbenchTest.error");
        if(error!="null")throw new Exception("Workbench regression: "+error);
        File.WriteAllText(Path.Combine(service.Root,"desktop-workbench-diagnostics.json"),await core.ExecuteScriptAsync("window.__workbenchTest"));
        using(var file=File.Create(Path.Combine(service.Root,"desktop-history.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
        await core.ExecuteScriptAsync("document.querySelector('#history-dialog').close();saveSession();sessionStorage.setItem('smoke-result-id',state.output.id)");
        core.Reload();
        if(!await Check("typeof sessionReady!=='undefined' && sessionReady && typeof state!=='undefined' && state.output && state.output.id===sessionStorage.getItem('smoke-result-id') && !state.busy && !document.querySelector('#download').classList.contains('disabled')",30))throw new Exception("Reload did not restore downloadable result");
        var normal=Size;Size=MinimumSize;await Task.Delay(250);
        if(!await Check("document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight && document.querySelector('.preview-tools').getBoundingClientRect().right<=document.querySelector('.canvas-panel').getBoundingClientRect().right",3))throw new Exception("Workbench controls overflow compact window");
        using(var file=File.Create(Path.Combine(service.Root,"desktop-workbench-compact.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
        Size=normal;
        web.ZoomFactor=2;await Task.Delay(300);
        if(!await Check("document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight && document.querySelector('.brand-group').getBoundingClientRect().right<=document.querySelector('.desktop-actions').getBoundingClientRect().left",3))throw new Exception("200% text scale overflow");
        await core.ExecuteScriptAsync("document.querySelector('.mobile-settings').click()");
        if(!await Check("document.querySelector('.inspector').classList.contains('mobile-open') && document.querySelector('.inspector').scrollHeight<=document.querySelector('.inspector').clientHeight",3))throw new Exception("200% settings drawer overflow");
        using(var file=File.Create(Path.Combine(service.Root,"desktop-workbench-zoom200.png")))await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png,file);
        await core.ExecuteScriptAsync("document.querySelector('.inspector-close').click()");web.ZoomFactor=1;
        if(!await Check("window.__appErrors.length===0",3))throw new Exception("Browser script errors: "+await core.ExecuteScriptAsync("window.__appErrors"));
    }
    async Task VerifyDesktopTransparency(){
        // A solid test window behind the app proves actual desktop composition, not just CSS alpha.
        using(var backdrop=new Form{FormBorderStyle=FormBorderStyle.None,ShowInTaskbar=false,TopMost=true,StartPosition=FormStartPosition.Manual,Bounds=Bounds,BackColor=Color.FromArgb(52,84,110)}){
            backdrop.Show();BringToFront();Activate();SetWindowPos(backdrop.Handle,blurLayer.Visible?blurLayer.Handle:Handle,0,0,0,0,0x13);await Task.Delay(350);
            using(var snapshot=new Bitmap(Width,Height)){
                using(var graphics=Graphics.FromImage(snapshot))graphics.CopyFromScreen(Location,Point.Empty,Size);
                snapshot.Save(Path.Combine(service.Root,"desktop-transparent-native.png"),System.Drawing.Imaging.ImageFormat.Png);
                var pixel=snapshot.GetPixel(1,Height/2);
                if(Math.Abs(pixel.R-52)>2 || Math.Abs(pixel.G-84)>2 || Math.Abs(pixel.B-110)>2)throw new Exception("Desktop transparency did not reveal the test backdrop: "+pixel);
            }
        }
    }
    Bitmap CaptureNative(string filename){
        var snapshot=new Bitmap(Width,Height);
        using(var graphics=Graphics.FromImage(snapshot))graphics.CopyFromScreen(Location,Point.Empty,Size);
        snapshot.Save(Path.Combine(service.Root,filename),System.Drawing.Imaging.ImageFormat.Png);
        return snapshot;
    }
    void VerifyInputOwnership(){
        foreach(var point in new[]{new Point(1,Height/2),new Point(8,8),new Point(100,210),new Point(Width-60,40)}){
            var screen=PointToScreen(point);var target=WindowFromPoint(screen);
            if(GetAncestor(target,2)!=Handle)throw new Exception("Transparent region passes input to another window: "+point);
            var packed=new IntPtr(unchecked((screen.Y<<16)|(screen.X&0xffff)));
            if(SendMessage(target,0x84,IntPtr.Zero,packed).ToInt64()==-1)throw new Exception("Transparent region returns HTTRANSPARENT: "+point);
        }
    }
    readonly System.Collections.Generic.Dictionary<string,Color[]> borderSamples = new System.Collections.Generic.Dictionary<string,Color[]>();
    async Task VerifyPanelComposition(string name,bool translucent){
        var core=web.CoreWebView2;
        if(!transparentHost || web.DefaultBackgroundColor.A!=0 || !await Check("getComputedStyle(document.documentElement).backgroundColor==='rgba(0, 0, 0, 0)' && getComputedStyle(document.body).backgroundColor==='rgba(0, 0, 0, 0)' && !document.querySelector('#glass-toggle').disabled",3))throw new Exception("Opaque desktop host: "+name);
        var json=await core.ExecuteScriptAsync("(()=>{const r=s=>document.querySelector(s).getBoundingClientRect(),c=r('.canvas-panel'),i=r('.inspector'),h=r('.topbar'),o=r('.output-bar');return [[(c.right+i.left)/2,c.top+c.height/2],[c.left+50,(h.bottom+c.top)/2],[c.left+50,(c.bottom+o.top)/2],[c.left+30,c.bottom-30]].map(([x,y])=>[Math.round(x*devicePixelRatio),Math.round(y*devicePixelRatio)])})()");
        var coords=new JavaScriptSerializer().Deserialize<int[][]>(json);
        var points=Array.ConvertAll(coords,p=>new Point(p[0]+web.Left,p[1]+web.Top));
        var borderJson=await core.ExecuteScriptAsync("[...document.querySelectorAll('.glass')].map(e=>{const r=e.getBoundingClientRect();return [Math.round((r.left+r.width*.4)*devicePixelRatio),Math.round((r.top+(document.documentElement.dataset.theme==='dark'?1:0))*devicePixelRatio)]})");
        var borderPoints=new JavaScriptSerializer().Deserialize<int[][]>(borderJson);
        using(var backdrop=new Form{FormBorderStyle=FormBorderStyle.None,ShowInTaskbar=false,TopMost=true,StartPosition=FormStartPosition.Manual,Bounds=Bounds}){
            bool inverted=false,solid=false;
            backdrop.Paint+=(s,e)=>{
                if(solid)e.Graphics.Clear(inverted?Color.Black:Color.White);
                else for(int x=0;x<backdrop.Width;x+=8)e.Graphics.FillRectangle(((x/8)%2==0)^inverted?Brushes.White:Brushes.Black,x,0,8,backdrop.Height);
            };
            backdrop.Show();BringToFront();Activate();SetWindowPos(backdrop.Handle,blurLayer.Visible?blurLayer.Handle:Handle,0,0,0,0,0x13);await Task.Delay(350);
            using(var first=CaptureNative("desktop-"+name+".png")){
                string borderKey=name.Replace("glass-on","glass").Replace("glass-off","glass");
                var colors=Array.ConvertAll(borderPoints,p=>first.GetPixel(p[0]+web.Left,p[1]+web.Top));
                Color[] previous;
                if(borderSamples.TryGetValue(borderKey,out previous)){
                    for(int i=0;i<colors.Length;i++)if(Math.Abs(previous[i].R-colors[i].R)+Math.Abs(previous[i].G-colors[i].G)+Math.Abs(previous[i].B-colors[i].B)>6)throw new Exception("Glass changed border pixels: "+name+" panel="+i+" "+previous[i]+" -> "+colors[i]);
                    File.AppendAllText(Path.Combine(service.Root,"desktop-glass-diagnostics.txt"),borderKey+": unchanged border pixels PASS"+Environment.NewLine);
                }else borderSamples.Add(borderKey,colors);
                for(int i=0;i<3;i++){
                    var pixel=first.GetPixel(points[i].X,points[i].Y);int expected=(points[i].X/8)%2==0?255:0;
                    if(Math.Abs(pixel.R-expected)>3 || Math.Abs(pixel.G-expected)>3 || Math.Abs(pixel.B-expected)>3)throw new Exception("Gap is tinted or blurred: "+name+" "+points[i]+" "+pixel+" expected "+expected);
                }
                inverted=true;backdrop.Invalidate();await Task.Delay(250);
                using(var second=CaptureNative("desktop-"+name+"-inverse.png")){
                    for(int i=0;i<3;i++){
                        var pixel=second.GetPixel(points[i].X,points[i].Y);int expected=(points[i].X/8)%2==0?0:255;
                        if(Math.Abs(pixel.R-expected)>3 || Math.Abs(pixel.G-expected)>3 || Math.Abs(pixel.B-expected)>3)throw new Exception("Gap did not preserve desktop pixels: "+name);
                    }
                    var p=points[3];var one=first.GetPixel(p.X,p.Y);var two=second.GetPixel(p.X,p.Y);
                    int difference=Math.Abs(one.R-two.R)+Math.Abs(one.G-two.G)+Math.Abs(one.B-two.B);
                    if(translucent){
                        double total=0,squared=0;int count=0;
                        for(int x=p.X;x<p.X+160;x++){var pixel=first.GetPixel(x,p.Y);double value=(pixel.R+pixel.G+pixel.B)/3.0;total+=value;squared+=value*value;count++;}
                        double mean=total/count,variation=Math.Sqrt(squared/count-mean*mean);
                        // DWM/HDR tone mapping changes absolute screenshot luminance.
                        // Sharp stripe suppression and the live black/white delta below
                        // test blur/translucency without assuming one display's gamma.
                        if(!blurLayer.Visible || !blurLayer.BlurApplied || variation>5 || mean<5 || mean>250)throw new Exception("Desktop Gaussian blur failed: "+name+" variation="+variation+" mean="+mean);
                        File.AppendAllText(Path.Combine(service.Root,"desktop-glass-diagnostics.txt"),name+": native background blur PASS; variation="+variation+Environment.NewLine);
                    }else if(difference>6 || blurLayer.Visible)throw new Exception("Glass off did not disable transparency and blur: "+name);
                    File.AppendAllText(Path.Combine(service.Root,"desktop-glass-diagnostics.txt"),name+": clear gaps PASS; panel delta="+difference+Environment.NewLine);
                }
            }
            if(translucent){
                // Keep the real window idle, then change only the background.
                // No direct blur refresh is allowed here: this covers the shipped path.
                await Task.Delay(2000);
                using(var settled=CaptureNative("desktop-"+name+"-settled.png")){
                    double total=0,squared=0;int count=0;var sample=points[3];
                    for(int x=sample.X;x<sample.X+160;x++){var c=settled.GetPixel(x,sample.Y);double v=(c.R+c.G+c.B)/3.0;total+=v;squared+=v*v;count++;}
                    double mean=total/count,variation=Math.Sqrt(squared/count-mean*mean);
                    if(variation>5 || mean<5 || mean>250)throw new Exception("Glass disappeared or lost blur after settling: "+name);
                }
                solid=true;inverted=false;backdrop.Invalidate();await Task.Delay(400);
                Color white,black;
                using(var shot=CaptureNative("desktop-"+name+"-white.png"))white=shot.GetPixel(points[3].X,points[3].Y);
                inverted=true;backdrop.Invalidate();await Task.Delay(400);
                using(var shot=CaptureNative("desktop-"+name+"-black.png"))black=shot.GetPixel(points[3].X,points[3].Y);
                int delta=Math.Abs(white.R-black.R)+Math.Abs(white.G-black.G)+Math.Abs(white.B-black.B);
                if(delta<200 || delta>360)throw new Exception("Blur background is stale or opaque: "+name+" delta="+delta);
                File.AppendAllText(Path.Combine(service.Root,"desktop-glass-diagnostics.txt"),name+": stable blur after 2 seconds; live desktop fill delta="+delta+" PASS"+Environment.NewLine);
            }
            VerifyInputOwnership();
        }
    }
    async Task VerifyGlassMode(){
        var core=web.CoreWebView2;
        File.WriteAllText(Path.Combine(service.Root,"desktop-glass-diagnostics.txt"),"");
        await VerifyPanelComposition("glass-off-dark",false);
        await core.ExecuteScriptAsync("document.querySelector('#glass-toggle').click();document.activeElement.blur()");
        if(!await Check("document.querySelector('#glass-toggle').getAttribute('aria-pressed')==='true' && getComputedStyle(document.querySelector('.canvas-panel')).backgroundColor==='rgba(0, 0, 0, 0.6)'",3) || !glassMode)throw new Exception("Panel transparency activation failed");
        core.Reload();
        if(!await Check("document.querySelector('#glass-toggle')?.getAttribute('aria-pressed')==='true' && document.querySelector('.desktop-window-controls') && localStorage.getItem('dlss-glass')==='on'",5))throw new Exception("Glass state did not persist");
        if(!await Check("[...document.querySelectorAll('.glass')].every(e=>getComputedStyle(e).opacity==='1' && getComputedStyle(e,'::before').opacity==='1')",3))throw new Exception("Glass faded panel contents or borders");
        await VerifyPanelComposition("glass-on-dark",true);
        await core.ExecuteScriptAsync("document.querySelector('.advanced summary').click()");
        if(!await Check("document.querySelector('#settings-dialog').open && getComputedStyle(document.querySelector('#settings-dialog')).backgroundColor==='rgba(0, 0, 0, 0.6)' && getComputedStyle(document.querySelector('#settings-dialog'),'::backdrop').backgroundColor==='rgba(0, 0, 0, 0)'",3))throw new Exception("Settings dialog transparency failed");
        await core.ExecuteScriptAsync("document.querySelector('#settings-dialog .done').click();document.querySelector('[data-theme=light][type=button]').click();document.activeElement.blur()");
        await VerifyPanelComposition("glass-on-light",true);
        await core.ExecuteScriptAsync("document.querySelector('#glass-toggle').click();document.activeElement.blur()");
        await VerifyPanelComposition("glass-off-light",false);
        await core.ExecuteScriptAsync("document.querySelector('[data-theme=dark][type=button]').click()");
    }
    async Task VerifyMaximizedMode(){
        var core=web.CoreWebView2;var originalBounds=Bounds;
        await core.ExecuteScriptAsync("document.querySelector('#glass-toggle').click();document.querySelector('.window-maximize').click()");
        if(!await Check("document.documentElement.classList.contains('desktop-maximized') && document.querySelector('.window-maximize').getAttribute('aria-label')==='还原窗口' && document.querySelector('.window-maximize img').src.includes('restore.svg') && document.querySelector('#glass-toggle').disabled && document.documentElement.dataset.glass==='off' && document.querySelector('#glass-toggle').getAttribute('aria-pressed')==='false' && localStorage.getItem('dlss-glass')==='on'",5))throw new Exception("Maximize did not force glass off while retaining preference");
        if(WindowState!=FormWindowState.Maximized || Bounds!=Screen.FromHandle(Handle).WorkingArea)throw new Exception("Maximized bounds do not match the work area");
        foreach(var theme in new[]{"dark","light"}){
            await core.ExecuteScriptAsync("document.querySelector('[data-theme="+theme+"][type=button]').click();document.activeElement.blur()");
            string fill=theme=="light"?"rgb(255, 255, 255)":"rgb(0, 0, 0)";
            if(!await Check("getComputedStyle(document.body).backgroundColor==='"+fill+"' && [...document.querySelectorAll('.glass')].every(e=>getComputedStyle(e).opacity==='1' && getComputedStyle(e).backdropFilter==='none')",3) || transparentHost || web.DefaultBackgroundColor.A!=255 || blurLayer.Visible || glassMode)throw new Exception("Maximized canvas is not opaque: "+theme);
            using(var backdrop=new Form{FormBorderStyle=FormBorderStyle.None,ShowInTaskbar=false,TopMost=true,StartPosition=FormStartPosition.Manual,Bounds=Bounds,BackColor=Color.Magenta}){
                backdrop.Show();BringToFront();Activate();await Task.Delay(300);
                using(var shot=CaptureNative("desktop-maximized-opaque-"+theme+".png")){
                    var pixel=shot.GetPixel(1,Height/2);var expected=theme=="light"?Color.White:Color.Black;
                    if(pixel.R!=expected.R || pixel.G!=expected.G || pixel.B!=expected.B)throw new Exception("Maximized background leaked desktop: "+theme+" "+pixel);
                }
                VerifyInputOwnership();
            }
        }
        core.Reload();
        if(!await Check("document.querySelector('#glass-toggle')?.disabled && document.documentElement.dataset.glass==='off' && localStorage.getItem('dlss-glass')==='on'",5))throw new Exception("Reload lost maximized glass override");
        await core.ExecuteScriptAsync("document.querySelector('.window-minimize').click()");await Task.Delay(200);
        if(WindowState!=FormWindowState.Minimized || !maximizedState || blurLayer.Visible)throw new Exception("Minimize lost maximized state or left blur visible");
        WindowState=FormWindowState.Maximized;await Task.Delay(200);
        await core.ExecuteScriptAsync("document.querySelector('[data-theme=dark][type=button]').click();document.querySelector('.window-maximize').click()");
        if(!await Check("!document.documentElement.classList.contains('desktop-maximized') && !document.querySelector('#glass-toggle').disabled && document.querySelector('#glass-toggle').getAttribute('aria-pressed')==='true'",5) || Bounds!=originalBounds || !transparentHost || !glassMode || !blurLayer.Visible)throw new Exception("Restore did not recover glass and window bounds");
        await VerifyPanelComposition("restored-glass-on-dark",true);
        Location=new Point(Location.X+12,Location.Y+12);await Task.Delay(150);
        if(blurLayer.Bounds!=RectangleToScreen(ClientRectangle))throw new Exception("Blur layer did not follow window movement");
        Bounds=originalBounds;
        await core.ExecuteScriptAsync("document.querySelector('#glass-toggle').click();document.querySelector('.topbar').dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0,detail:2}))");
        if(!await Check("document.documentElement.classList.contains('desktop-maximized') && document.querySelector('#glass-toggle').disabled",3) || glassMode || transparentHost || blurLayer.Visible)throw new Exception("Title bar maximize failed with glass off");
        await core.ExecuteScriptAsync("document.querySelector('.window-maximize').click()");
        if(!await Check("!document.documentElement.classList.contains('desktop-maximized') && document.querySelector('#glass-toggle').getAttribute('aria-pressed')==='false'",3) || glassMode || !transparentHost || blurLayer.Visible)throw new Exception("Restore enabled glass unexpectedly");
        File.AppendAllText(Path.Combine(service.Root,"desktop-glass-diagnostics.txt"),"maximize: forced glass off; black/white opaque canvas; reload, minimize, restore and movement PASS"+Environment.NewLine);
    }
    [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int w,int h,uint flags);
    [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr window,uint flags);
    [StructLayout(LayoutKind.Sequential)] struct DwmBlurBehind{public uint Flags;[MarshalAs(UnmanagedType.Bool)] public bool Enable;public IntPtr Region;[MarshalAs(UnmanagedType.Bool)] public bool TransitionOnMaximized;}
    [DllImport("dwmapi.dll")] static extern int DwmEnableBlurBehindWindow(IntPtr window,ref DwmBlurBehind blur);
    [DllImport("gdi32.dll")] static extern IntPtr CreateRectRgn(int left,int top,int right,int bottom);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr value);
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h,int m,IntPtr w,IntPtr l);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
}

internal static class DesktopProgram {
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern uint RegisterWindowMessage(string message);
    [DllImport("user32.dll")] static extern bool PostMessage(IntPtr window,uint message,IntPtr w,IntPtr l);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [STAThread] static int Main(string[] args){
        SetProcessDPIAware();Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
        if(args.Length>0&&args[0]=="--smoke-test")return ServiceDiagnostics.Run(args);
        bool test=args.Length>0&&args[0]=="--desktop-smoke-test";
        using(var service=new StudioService()){
            string identity=service.Identity;uint activate=RegisterWindowMessage("DLSSStudioDesktop_"+identity);
            bool created;using(var mutex=new Mutex(true,"Local\\DLSSStudioDesktop_"+identity,out created)){
                if(!created){PostMessage(new IntPtr(0xffff),activate,IntPtr.Zero,IntPtr.Zero);return 0;}
                try{using(var form=new DesktopShell(test,activate)){Application.Run(form);return test?form.TestResult:0;}}
                finally{mutex.ReleaseMutex();}
            }
        }
    }
}
