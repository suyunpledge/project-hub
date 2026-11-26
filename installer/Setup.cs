// Project Hub installer / uninstaller. Built by installer/build.py with the C# compiler that ships
// with Windows (C# 5, .NET Framework 4.x) — no third-party installer tooling needed.
//
//   ProjectHub-Setup.exe                 graphical install
//   ProjectHub-Setup.exe /S [/D=<dir>]   silent install (CI / testing)
//   Uninstall.exe /uninstall [/S]        remove (copied into the install folder)
using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Project Hub Setup")]
[assembly: AssemblyProduct("Project Hub")]
[assembly: AssemblyCompany("suyunpledge")]
[assembly: AssemblyVersion(Setup.Version + ".0")]
[assembly: AssemblyFileVersion(Setup.Version + ".0")]

static class Setup
{
    public const string Version = "1.1.1";
    public const string AppName = "Project Hub";
    const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\ProjectHub";

    public static string DefaultDir
    {
        get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "ProjectHub"); }
    }
    public static string DataDir
    {
        get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ProjectHub"); }
    }

    [STAThread]
    static int Main(string[] args)
    {
        bool silent = false, uninstall = false, shortcuts = true, path = true, register = true;
        string dir = null;
        foreach (string a in args)
        {
            if (a.Equals("/S", StringComparison.OrdinalIgnoreCase)) silent = true;
            else if (a.Equals("/uninstall", StringComparison.OrdinalIgnoreCase)) uninstall = true;
            else if (a.StartsWith("/D=", StringComparison.OrdinalIgnoreCase)) dir = a.Substring(3).Trim('"');
            // test switches: leave the machine's shortcuts / PATH / Apps list untouched
            else if (a.Equals("/NOSHORTCUTS", StringComparison.OrdinalIgnoreCase)) shortcuts = false;
            else if (a.Equals("/NOPATH", StringComparison.OrdinalIgnoreCase)) path = false;
            else if (a.Equals("/NOREG", StringComparison.OrdinalIgnoreCase)) register = false;
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            if (uninstall) return Uninstall(silent, dir);
            if (silent)
            {
                Install(dir ?? DefaultDir, shortcuts, shortcuts, path, null, register);
                return 0;
            }
            Application.Run(new SetupForm());
            return 0;
        }
        catch (Exception e)
        {
            if (silent) { Console.Error.WriteLine(e.Message); return 1; }
            MessageBox.Show(e.Message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    // ------------------------------------------------------------------ install

    public static void Install(string dir, bool desktop, bool startMenu, bool addPath, Action<int, string> progress, bool register = true)
    {
        if (progress == null) progress = delegate { };
        dir = Path.GetFullPath(dir);
        Directory.CreateDirectory(dir);
        progress(3, "检查是否有正在运行的 Project Hub…");
        EnsureNotRunning(dir);

        // remove the previous program files (data lives elsewhere and is kept)
        foreach (string sub in new[] { "app", "runtime" })
        {
            string p = Path.Combine(dir, sub);
            if (Directory.Exists(p)) Directory.Delete(p, true);
        }

        progress(8, "解压程序文件…");
        using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (ZipArchive zip = new ZipArchive(s, ZipArchiveMode.Read))
        {
            int n = 0, total = zip.Entries.Count;
            foreach (ZipArchiveEntry e in zip.Entries)
            {
                string target = Path.GetFullPath(Path.Combine(dir, e.FullName));
                if (!target.StartsWith(dir, StringComparison.OrdinalIgnoreCase)) continue; // zip-slip guard
                if (e.FullName.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                e.ExtractToFile(target, true);
                n++;
                if (n % 20 == 0 || n == total) progress(8 + 72 * n / Math.Max(1, total), "解压程序文件… " + n + "/" + total);
            }
        }
        File.WriteAllText(Path.Combine(dir, "app", ".installed"), Version);
        Directory.CreateDirectory(Path.Combine(DataDir, "data"));

        progress(84, "安装卸载程序…");
        string self = Assembly.GetExecutingAssembly().Location;
        string uninstaller = Path.Combine(dir, "Uninstall.exe");
        if (!string.Equals(Path.GetFullPath(self), uninstaller, StringComparison.OrdinalIgnoreCase))
            File.Copy(self, uninstaller, true);

        progress(88, "创建快捷方式…");
        string exe = Path.Combine(dir, "ProjectHub.exe");
        string icon = Path.Combine(dir, "app", "hub", "static", "icon.ico");
        if (desktop) Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), AppName + ".lnk"), exe, dir, icon);
        if (startMenu) Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), AppName + ".lnk"), exe, dir, icon);
        if (addPath) { progress(92, "添加 hub 命令到 PATH…"); AddToPath(dir); }

        progress(96, "登记到「应用和功能」…");
        if (register)
        using (RegistryKey k = Registry.CurrentUser.CreateSubKey(UninstallKey))
        {
            k.SetValue("DisplayName", AppName);
            k.SetValue("DisplayVersion", Version);
            k.SetValue("Publisher", "suyunpledge");
            k.SetValue("DisplayIcon", icon);
            k.SetValue("InstallLocation", dir);
            k.SetValue("URLInfoAbout", "https://github.com/suyunpledge/project-hub");
            k.SetValue("UninstallString", "\"" + uninstaller + "\" /uninstall");
            k.SetValue("QuietUninstallString", "\"" + uninstaller + "\" /uninstall /S");
            k.SetValue("NoModify", 1, RegistryValueKind.DWord);
            k.SetValue("NoRepair", 1, RegistryValueKind.DWord);
            k.SetValue("EstimatedSize", (int)(DirSize(dir) / 1024), RegistryValueKind.DWord);
            k.SetValue("AddedToPath", addPath ? 1 : 0, RegistryValueKind.DWord);
        }
        progress(100, "安装完成");
    }

    static void EnsureNotRunning(string dir)
    {
        string runtime = Path.Combine(dir, "runtime");
        if (!Directory.Exists(runtime)) return;
        for (int attempt = 0; ; attempt++)
        {
            string probe = runtime + ".probe";
            try
            {
                Directory.Move(runtime, probe);   // fails while any file inside is in use
                Directory.Move(probe, runtime);
                return;
            }
            catch (IOException)
            {
                if (attempt >= 1)
                    throw new InvalidOperationException("Project Hub 正在运行。请关闭它的窗口（或结束 Agent 使用的 hub 命令）后重新运行安装程序。");
                Thread.Sleep(1500);
            }
            catch (UnauthorizedAccessException)
            {
                if (attempt >= 1)
                    throw new InvalidOperationException("Project Hub 正在运行。请关闭它的窗口后重新运行安装程序。");
                Thread.Sleep(1500);
            }
        }
    }

    static void Shortcut(string lnk, string target, string workdir, string icon)
    {
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(t);
        try
        {
            object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnk });
            Type st = sc.GetType();
            st.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { target });
            st.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { workdir });
            st.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc, new object[] { icon + ",0" });
            st.InvokeMember("Description", BindingFlags.SetProperty, null, sc, new object[] { "本机项目仓库" });
            st.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
            Marshal.FinalReleaseComObject(sc);
        }
        finally { Marshal.FinalReleaseComObject(shell); }
    }

    static bool PointsInto(string lnk, string dir)
    {
        try
        {
            Type t = Type.GetTypeFromProgID("WScript.Shell");
            object shell = Activator.CreateInstance(t);
            try
            {
                object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { lnk });
                string target = (string)sc.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, sc, null);
                Marshal.FinalReleaseComObject(sc);
                return target.StartsWith(dir.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
            }
            finally { Marshal.FinalReleaseComObject(shell); }
        }
        catch { return false; }
    }

    static void AddToPath(string dir)
    {
        using (RegistryKey env = Registry.CurrentUser.OpenSubKey("Environment", true))
        {
            string path = (string)env.GetValue("Path", "", RegistryValueOptions.DoNotExpandEnvironmentNames);
            foreach (string part in path.Split(';'))
                if (part.TrimEnd('\\').Equals(dir.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) return;
            string kind = env.GetValueKind("Path") == RegistryValueKind.ExpandString ? "x" : "s";
            string updated = string.IsNullOrEmpty(path) ? dir : path.TrimEnd(';') + ";" + dir;
            env.SetValue("Path", updated, kind == "x" ? RegistryValueKind.ExpandString : RegistryValueKind.String);
        }
        BroadcastEnvChange();
    }

    static void RemoveFromPath(string dir)
    {
        using (RegistryKey env = Registry.CurrentUser.OpenSubKey("Environment", true))
        {
            if (env == null) return;
            string path = (string)env.GetValue("Path", "", RegistryValueOptions.DoNotExpandEnvironmentNames);
            StringBuilder sb = new StringBuilder();
            foreach (string part in path.Split(';'))
            {
                if (part.Length == 0 || part.TrimEnd('\\').Equals(dir.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase)) continue;
                if (sb.Length > 0) sb.Append(';');
                sb.Append(part);
            }
            RegistryValueKind kind = env.GetValueKind("Path");
            env.SetValue("Path", sb.ToString(), kind == RegistryValueKind.ExpandString ? kind : RegistryValueKind.String);
        }
        BroadcastEnvChange();
    }

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);

    static void BroadcastEnvChange()
    {
        UIntPtr r;
        SendMessageTimeout(new IntPtr(0xffff), 0x001A, UIntPtr.Zero, "Environment", 0x0002, 3000, out r);
    }

    static long DirSize(string dir)
    {
        long size = 0;
        foreach (string f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories))
            try { size += new FileInfo(f).Length; } catch (IOException) { }
        return size;
    }

    // ------------------------------------------------------------------ uninstall

    static int Uninstall(bool silent, string dir)
    {
        string self = Assembly.GetExecutingAssembly().Location;
        if (dir == null)
        {
            dir = Path.GetDirectoryName(self);
            // the uninstaller lives inside the folder it deletes: re-run from a temp copy
            string temp = Path.Combine(Path.GetTempPath(), "ProjectHub-Uninstall-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".exe");
            File.Copy(self, temp, true);
            Process.Start(new ProcessStartInfo(temp, "/uninstall /D=\"" + dir + "\"" + (silent ? " /S" : "")) { UseShellExecute = false });
            return 0;
        }
        bool removeData = false;
        if (!silent)
        {
            if (MessageBox.Show("确定要卸载 Project Hub 吗？", AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return 1;
            removeData = MessageBox.Show("同时删除已登记的项目列表和设置吗？\n（只删除 Project Hub 自己的记录，你的项目文件不会受影响。选“否”可在重装后继续使用。）",
                AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes;
        }
        try { EnsureNotRunning(dir); }
        catch (InvalidOperationException e)
        {
            if (!silent) MessageBox.Show(e.Message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
        foreach (string lnk in new[] {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), AppName + ".lnk"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), AppName + ".lnk") })
            if (File.Exists(lnk) && PointsInto(lnk, dir)) File.Delete(lnk);  // never touch someone else's shortcut
        RemoveFromPath(dir);
        Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false);
        for (int i = 0; i < 5 && Directory.Exists(dir); i++)
        {
            try { Directory.Delete(dir, true); }
            catch (IOException) { Thread.Sleep(700); }
            catch (UnauthorizedAccessException) { Thread.Sleep(700); }
        }
        if (removeData && Directory.Exists(DataDir))
            try { Directory.Delete(DataDir, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        if (!silent) MessageBox.Show("Project Hub 已卸载。", AppName, MessageBoxButtons.OK, MessageBoxIcon.Information);
        return 0;
    }
}

// ====================================================================== UI

class SetupForm : Form
{
    static readonly Color Bg = Color.FromArgb(15, 17, 26);
    static readonly Color Card = Color.FromArgb(24, 28, 42);
    static readonly Color Line = Color.FromArgb(44, 50, 72);
    static readonly Color Fg = Color.FromArgb(227, 231, 242);
    static readonly Color Muted = Color.FromArgb(133, 144, 171);
    static readonly Color Accent = Color.FromArgb(109, 140, 255);
    static readonly Color Accent2 = Color.FromArgb(160, 110, 250);

    readonly Panel welcome = new Panel(), working = new Panel(), done = new Panel();
    readonly TextBox dirBox = new TextBox();
    readonly CheckBox desktop = new CheckBox(), startMenu = new CheckBox(), addPath = new CheckBox(), launch = new CheckBox();
    readonly Label status = new Label(), gitNote = new Label();
    readonly ProgressBarLite bar = new ProgressBarLite();
    readonly Image logo;
    string installedDir;

    public SetupForm()
    {
        Text = "安装 " + Setup.AppName;
        Font = new Font("Microsoft YaHei UI", 9.5f);
        ClientSize = new Size(720, 440);
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Bg;
        ForeColor = Fg;
        try { Icon = Icon.ExtractAssociatedIcon(Assembly.GetExecutingAssembly().Location); } catch { }
        using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("logo.png"))
            logo = s != null ? Image.FromStream(s) : null;

        Panel side = new Panel { Dock = DockStyle.Left, Width = 230 };
        side.Paint += PaintSide;
        Controls.Add(side);

        foreach (Panel p in new[] { welcome, working, done })
        {
            p.Location = new Point(230, 0);
            p.Size = new Size(490, 440);
            p.BackColor = Bg;
            Controls.Add(p);
        }
        BuildWelcome();
        BuildWorking();
        BuildDone();
        Show(welcome);
        Shown += delegate { dirBox.SelectionStart = 0; dirBox.SelectionLength = 0; ActiveControl = null; };
    }

    void PaintSide(object sender, PaintEventArgs e)
    {
        Control c = (Control)sender;
        Graphics g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        using (LinearGradientBrush b = new LinearGradientBrush(c.ClientRectangle, Color.FromArgb(30, 32, 78), Color.FromArgb(58, 24, 82), 70f))
            g.FillRectangle(b, c.ClientRectangle);
        using (GraphicsPath glow = new GraphicsPath())
        {
            glow.AddEllipse(-80, 230, 300, 300);
            using (PathGradientBrush pb = new PathGradientBrush(glow))
            {
                pb.CenterColor = Color.FromArgb(90, 109, 140, 255);
                pb.SurroundColors = new[] { Color.FromArgb(0, 109, 140, 255) };
                g.FillPath(pb, glow);
            }
        }
        if (logo != null) g.DrawImage(logo, 32, 46, 72, 72);
        using (Font big = new Font("Microsoft YaHei UI", 17f, FontStyle.Bold))
            g.DrawString("Project Hub", big, Brushes.White, 28, 136);
        using (Brush m = new SolidBrush(Color.FromArgb(200, 205, 225)))
        {
            g.DrawString("本机项目仓库", Font, m, 31, 172);
            g.DrawString("版本 " + Setup.Version, Font, m, 31, 400);
        }
        string[] points = { "分组管理本机项目", "跨分支提交与推送", "运行与实时检查", "一键发布到 GitHub", "Agent 通过 MCP 调用" };
        using (Brush dot = new SolidBrush(Accent))
        using (Brush t = new SolidBrush(Color.FromArgb(225, 228, 240)))
            for (int i = 0; i < points.Length; i++)
            {
                g.FillEllipse(dot, 33, 225 + i * 30, 7, 7);
                g.DrawString(points[i], Font, t, 47, 218 + i * 30);
            }
    }

    Label Heading(Panel p, string text, int y)
    {
        Label l = new Label { Text = text, Font = new Font("Microsoft YaHei UI", 15f, FontStyle.Bold), ForeColor = Fg, AutoSize = true, Location = new Point(34, y) };
        p.Controls.Add(l);
        return l;
    }

    Label Note(Panel p, string text, int y, int h)
    {
        Label l = new Label { Text = text, ForeColor = Muted, Location = new Point(36, y), Size = new Size(420, h) };
        p.Controls.Add(l);
        return l;
    }

    CheckBox Check(Panel p, CheckBox c, string text, int y)
    {
        c.Text = text; c.Checked = true; c.ForeColor = Fg; c.AutoSize = true; c.Location = new Point(36, y);
        c.FlatStyle = FlatStyle.Standard;  // System style ignores ForeColor (unreadable on dark)
        c.UseVisualStyleBackColor = false;
        c.BackColor = Bg;
        p.Controls.Add(c);
        return c;
    }

    Button MakeButton(string text, bool primary)
    {
        Button b = new Button { Text = text, Size = new Size(118, 36), FlatStyle = FlatStyle.Flat, Cursor = Cursors.Hand };
        b.FlatAppearance.BorderSize = primary ? 0 : 1;
        b.FlatAppearance.BorderColor = Line;
        b.BackColor = primary ? Accent : Card;
        b.ForeColor = primary ? Color.White : Fg;
        b.Font = new Font("Microsoft YaHei UI", 9.5f, primary ? FontStyle.Bold : FontStyle.Regular);
        return b;
    }

    void BuildWelcome()
    {
        Heading(welcome, "欢迎安装 Project Hub", 40);
        Note(welcome, "把散落在电脑里的项目登记到一处：按分组管理、浏览源码与分支、提交与推送、运行并实时检查错误。", 82, 44);

        Label l = new Label { Text = "安装位置", ForeColor = Muted, AutoSize = true, Location = new Point(36, 140) };
        welcome.Controls.Add(l);
        dirBox.Text = Setup.DefaultDir;
        dirBox.Location = new Point(36, 162);
        dirBox.Size = new Size(316, 26);
        dirBox.BackColor = Card; dirBox.ForeColor = Fg; dirBox.BorderStyle = BorderStyle.FixedSingle;
        welcome.Controls.Add(dirBox);
        Button browse = MakeButton("浏览…", false);
        browse.Size = new Size(86, 27);
        browse.Location = new Point(360, 161);
        browse.Click += delegate
        {
            using (FolderBrowserDialog d = new FolderBrowserDialog { Description = "选择安装位置", SelectedPath = dirBox.Text })
                if (d.ShowDialog(this) == DialogResult.OK) dirBox.Text = Path.Combine(d.SelectedPath, "ProjectHub");
        };
        welcome.Controls.Add(browse);

        Check(welcome, desktop, "创建桌面快捷方式", 204);
        Check(welcome, startMenu, "添加到开始菜单", 232);
        Check(welcome, addPath, "添加 hub 命令（供 Agent 在终端调用）", 260);

        gitNote.Location = new Point(36, 296);
        gitNote.Size = new Size(420, 40);
        bool git = HasGit();
        gitNote.ForeColor = git ? Color.FromArgb(110, 220, 170) : Color.FromArgb(251, 191, 36);
        gitNote.Text = git ? "✓ 已检测到 Git。" : "⚠ 没有检测到 Git。Project Hub 的分支、提交和推送功能需要 Git，\n   可在 git-scm.com 下载安装（安装后无需重装本程序）。";
        welcome.Controls.Add(gitNote);

        Note(welcome, "安装到当前用户，无需管理员权限。已登记的项目保存在 %LOCALAPPDATA%\\ProjectHub，重装或升级不会丢失。", 342, 40);

        Button install = MakeButton("安装", true);
        install.Location = new Point(338, 386);
        install.Click += delegate { StartInstall(); };
        welcome.Controls.Add(install);
        Button cancel = MakeButton("取消", false);
        cancel.Location = new Point(212, 386);
        cancel.Click += delegate { Close(); };
        welcome.Controls.Add(cancel);
        AcceptButton = install;
    }

    void BuildWorking()
    {
        Heading(working, "正在安装…", 40);
        bar.Location = new Point(36, 120);
        bar.Size = new Size(420, 10);
        working.Controls.Add(bar);
        status.ForeColor = Muted;
        status.Location = new Point(36, 142);
        status.Size = new Size(420, 40);
        working.Controls.Add(status);
    }

    void BuildDone()
    {
        Heading(done, "安装完成", 40);
        Note(done, "Project Hub 已安装。可以从桌面或开始菜单打开。\n\nAgent 可在终端使用 hub 命令（新开的终端生效），或在 Project Hub 的「GitHub」设置里复制 MCP 配置，让 Claude Code / Codex 直接调用。", 84, 110);
        Check(done, launch, "现在打开 Project Hub", 210);
        Button finish = MakeButton("完成", true);
        finish.Location = new Point(338, 386);
        finish.Click += delegate
        {
            if (launch.Checked && installedDir != null)
                try { Process.Start(new ProcessStartInfo(Path.Combine(installedDir, "ProjectHub.exe")) { WorkingDirectory = installedDir }); } catch { }
            Close();
        };
        done.Controls.Add(finish);
    }

    void Show(Panel p)
    {
        welcome.Visible = p == welcome;
        working.Visible = p == working;
        done.Visible = p == done;
    }

    static bool HasGit()
    {
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo("git", "--version") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true };
            using (Process p = Process.Start(psi)) { p.WaitForExit(5000); return p.ExitCode == 0; }
        }
        catch { return false; }
    }

    void StartInstall()
    {
        string dir = dirBox.Text.Trim();
        if (dir.Length == 0 || !Path.IsPathRooted(dir))
        {
            MessageBox.Show(this, "请选择一个完整的安装路径。", Setup.AppName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        Show(working);
        bool d = desktop.Checked, s = startMenu.Checked, p = addPath.Checked;
        Thread t = new Thread(delegate ()
        {
            try
            {
                Setup.Install(dir, d, s, p, delegate (int pct, string msg)
                {
                    BeginInvoke((Action)delegate { bar.Value = pct; status.Text = msg; });
                });
                installedDir = Path.GetFullPath(dir);
                BeginInvoke((Action)delegate { Show(done); AcceptButton = null; });
            }
            catch (Exception e)
            {
                BeginInvoke((Action)delegate
                {
                    MessageBox.Show(this, "安装没有完成：\n" + e.Message, Setup.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                    Show(welcome);
                });
            }
        });
        t.IsBackground = true;
        t.Start();
    }
}

class ProgressBarLite : Control
{
    int value;
    public int Value { get { return value; } set { this.value = Math.Max(0, Math.Min(100, value)); Invalidate(); } }
    public ProgressBarLite() { SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint, true); }
    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        using (SolidBrush track = new SolidBrush(Color.FromArgb(35, 42, 62))) g.FillRectangle(track, ClientRectangle);
        int w = Width * value / 100;
        if (w > 0)
            using (LinearGradientBrush b = new LinearGradientBrush(new Rectangle(0, 0, Math.Max(1, Width), Height),
                Color.FromArgb(109, 140, 255), Color.FromArgb(192, 132, 252), 0f))
                g.FillRectangle(b, 0, 0, w, Height);
    }
}
