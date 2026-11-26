// ProjectHub.exe — starts the desktop app with the bundled Python runtime (no console window).
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Project Hub")]
[assembly: AssemblyProduct("Project Hub")]

static class Launcher
{
    [STAThread]
    static int Main(string[] args)
    {
        string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string pyw = Path.Combine(root, "runtime", "pythonw.exe");
        string script = Path.Combine(root, "app", "hub.py");
        if (!File.Exists(pyw) || !File.Exists(script))
        {
            MessageBox.Show("Project Hub 的程序文件不完整，请重新运行安装程序。", "Project Hub", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        StringBuilder a = new StringBuilder("\"" + script + "\"");
        if (args.Length == 0) a.Append(" desktop");
        foreach (string x in args) a.Append(" \"").Append(x.Replace("\"", "\\\"")).Append('"');
        ProcessStartInfo psi = new ProcessStartInfo(pyw, a.ToString()) { WorkingDirectory = root, UseShellExecute = false };
        Process.Start(psi);
        return 0;
    }
}
