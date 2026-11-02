// Caption Remover for Windows: launcher, installer, first-run setup and uninstaller in one exe.
//
// First run (from the unzipped download): installs itself to %LOCALAPPDATA%\Programs\Caption Remover,
// adds Start menu + desktop shortcuts and an entry in Settings > Apps, then relaunches from there.
// Then: downloads its own Python, ffmpeg and AI models into %LOCALAPPDATA%\Caption Remover (with a
// progress window), and starts the app. Every later launch starts the app straight away.
// In-app updates put new code in %LOCALAPPDATA%\Caption Remover\app; this launcher runs whichever
// is newer: that, or the code installed next to the exe.
package main

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"crypto/sha1"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"
)

var version = "dev" // set at build time: -ldflags "-X main.version=1.2.0"

const (
	appName = "Caption Remover"
	pyURL   = "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.12.7%2B20241016-x86_64-pc-windows-msvc-install_only.tar.gz"
	ffBase  = "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0"
	lamaURL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
	crafURL = "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip"
	cudaIdx = "https://download.pytorch.org/whl/cu124"
	regKey  = `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TruStudioCaptionRemover`

	createNoWindow  = 0x08000000
	detachedProcess = 0x00000008
)

var (
	user32      = syscall.NewLazyDLL("user32.dll")
	pMessageBox = user32.NewProc("MessageBoxW")
	local       = os.Getenv("LOCALAPPDATA")
	installDir  = filepath.Join(local, "Programs", appName)
	sup         = filepath.Join(local, appName)
	logPath     = filepath.Join(sup, "log.txt")
	logFile     *os.File
)

// ------------------------------------------------------------------ helpers

func utf16p(s string) *uint16 { p, _ := syscall.UTF16PtrFromString(s); return p }

const (
	mbOK          = 0x0
	mbOKCancel    = 0x1
	mbYesNo       = 0x4
	mbIconInfo    = 0x40
	mbIconWarn    = 0x30
	mbIconError   = 0x10
	idOK          = 1
	idYes         = 6
	mbSetForegrnd = 0x10000
)

func msgBox(text string, flags uintptr) int {
	r, _, _ := pMessageBox.Call(0, uintptr(unsafe.Pointer(utf16p(text))), uintptr(unsafe.Pointer(utf16p(appName))), flags|mbSetForegrnd)
	return int(r)
}

func logf(format string, a ...any) {
	if logFile != nil {
		fmt.Fprintf(logFile, time.Now().Format("2006-01-02 15:04:05 ")+format+"\n", a...)
	}
}

func hidden(cmd *exec.Cmd) *exec.Cmd {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
	return cmd
}

func run(name string, args ...string) error {
	cmd := hidden(exec.Command(name, args...))
	cmd.Stdout, cmd.Stderr = logFile, logFile
	logf("$ %s %s", name, strings.Join(args, " "))
	return cmd.Run()
}

func powershell(script string) error {
	u := utf16.Encode([]rune(script))
	b := make([]byte, len(u)*2)
	for i, c := range u {
		b[2*i], b[2*i+1] = byte(c), byte(c>>8)
	}
	return run("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", base64.StdEncoding.EncodeToString(b))
}

func psq(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" } // PowerShell single-quoted string

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

func samePath(a, b string) bool {
	a, _ = filepath.Abs(a)
	b, _ = filepath.Abs(b)
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

func copyDir(src, dst string) error {
	return filepath.Walk(src, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		t := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(t, 0o755)
		}
		return copyFile(p, t)
	})
}

func readVersion(dir string) string {
	b, err := os.ReadFile(filepath.Join(dir, "app.py"))
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, "VERSION = ") {
			p := strings.Split(line, `"`)
			if len(p) >= 2 {
				return p[1]
			}
		}
	}
	return ""
}

func newer(a, b string) bool { // version a > b
	if a == "" || a == b {
		return false
	}
	pa, pb := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < 3; i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			return x > y
		}
	}
	return false
}

// ------------------------------------------------------------------ progress window

type progress struct{ path string }

func startProgress() *progress {
	p := &progress{path: filepath.Join(sup, "setup-status.txt")}
	p.set(0, "Preparing…")
	ico := filepath.Join(installDir, "AppIcon.ico")
	script := `
Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$st = ` + psq(p.path) + `
$f = New-Object Windows.Forms.Form
$f.Text = 'Caption Remover setup'; $f.ClientSize = New-Object Drawing.Size(460,128)
$f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'; $f.MaximizeBox = $false; $f.TopMost = $true
$f.BackColor = [Drawing.Color]::FromArgb(18,18,21); $f.ForeColor = [Drawing.Color]::FromArgb(235,235,240)
try { $f.Icon = New-Object Drawing.Icon(` + psq(ico) + `) } catch {}
$h = New-Object Windows.Forms.Label; $h.Location = New-Object Drawing.Point(20,16); $h.Size = New-Object Drawing.Size(420,22)
$h.Font = New-Object Drawing.Font('Segoe UI Semibold',11); $h.Text = 'Setting up Caption Remover'
$l = New-Object Windows.Forms.Label; $l.Location = New-Object Drawing.Point(20,42); $l.Size = New-Object Drawing.Size(420,36)
$l.Font = New-Object Drawing.Font('Segoe UI',9); $l.ForeColor = [Drawing.Color]::FromArgb(163,163,173)
$p = New-Object Windows.Forms.ProgressBar; $p.Location = New-Object Drawing.Point(20,86); $p.Size = New-Object Drawing.Size(420,18); $p.Maximum = 1000
$f.Controls.AddRange(@($h,$l,$p))
$t = New-Object Windows.Forms.Timer; $t.Interval = 300
$t.Add_Tick({
  try { $s = [IO.File]::ReadAllText($st) } catch { return }
  if ($s.StartsWith('DONE')) { $t.Stop(); $f.Close(); return }
  $parts = $s.Split('|',2); $v = [int]$parts[0]
  if ($v -lt 0) { $p.Style = 'Marquee' } else { $p.Style = 'Continuous'; $p.Value = [Math]::Min(1000,$v) }
  if ($parts.Length -gt 1) { $l.Text = $parts[1] }
})
$f.Add_Shown({ $t.Start() }); [void]$f.ShowDialog()
`
	u := utf16.Encode([]rune(script))
	b := make([]byte, len(u)*2)
	for i, c := range u {
		b[2*i], b[2*i+1] = byte(c), byte(c>>8)
	}
	cmd := exec.Command("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
		"-EncodedCommand", base64.StdEncoding.EncodeToString(b))
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	_ = cmd.Start()
	return p
}

// set: permille 0..1000, or -1 for "busy" (no percentage)
func (p *progress) set(permille int, text string) {
	tmp := p.path + ".tmp"
	_ = os.WriteFile(tmp, []byte(fmt.Sprintf("%d|%s", permille, text)), 0o644)
	_ = os.Rename(tmp, p.path)
}
func (p *progress) done() { _ = os.WriteFile(p.path, []byte("DONE"), 0o644) }

// ------------------------------------------------------------------ downloads

func download(url, dst string, pr *progress, label string, lo, hi int) error {
	logf("download %s", url)
	resp, err := http.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("HTTP %d for %s", resp.StatusCode, url)
	}
	_ = os.MkdirAll(filepath.Dir(dst), 0o755)
	tmp := dst + ".part"
	f, err := os.Create(tmp)
	if err != nil {
		return err
	}
	total := resp.ContentLength
	var got int64
	buf := make([]byte, 256*1024)
	last := time.Now()
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := f.Write(buf[:n]); werr != nil {
				f.Close()
				return werr
			}
			got += int64(n)
			if pr != nil && time.Since(last) > 250*time.Millisecond {
				last = time.Now()
				if total > 0 {
					pr.set(lo+int(float64(hi-lo)*float64(got)/float64(total)),
						fmt.Sprintf("%s  (%d of %d MB)", label, got>>20, total>>20))
				} else {
					pr.set(lo, fmt.Sprintf("%s  (%d MB)", label, got>>20))
				}
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			f.Close()
			return rerr
		}
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, dst)
}

func untarGz(src, dst string) error {
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		if strings.Contains(h.Name, "..") {
			continue
		}
		t := filepath.Join(dst, filepath.FromSlash(h.Name))
		switch h.Typeflag {
		case tar.TypeDir:
			_ = os.MkdirAll(t, 0o755)
		case tar.TypeReg:
			_ = os.MkdirAll(filepath.Dir(t), 0o755)
			o, err := os.Create(t)
			if err != nil {
				return err
			}
			if _, err := io.Copy(o, tr); err != nil {
				o.Close()
				return err
			}
			o.Close()
		}
	}
}

func unzipFile(src, dst string) error {
	z, err := zip.OpenReader(src)
	if err != nil {
		return err
	}
	defer z.Close()
	for _, f := range z.File {
		if strings.Contains(f.Name, "..") || f.FileInfo().IsDir() {
			continue
		}
		t := filepath.Join(dst, filepath.FromSlash(f.Name))
		_ = os.MkdirAll(filepath.Dir(t), 0o755)
		r, err := f.Open()
		if err != nil {
			return err
		}
		o, err := os.Create(t)
		if err != nil {
			r.Close()
			return err
		}
		_, err = io.Copy(o, r)
		r.Close()
		o.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// ------------------------------------------------------------------ install / uninstall

func installSelf(exe, exeDir string) {
	if !exists(filepath.Join(exeDir, "app", "app.py")) {
		msgBox("Please unzip the download first.\n\nRight-click the zip file, choose \"Extract All\", then open Caption Remover.exe from the extracted folder.", mbOK|mbIconInfo)
		os.Exit(0)
	}
	already := exists(filepath.Join(installDir, appName+".exe"))
	msg := "Install Caption Remover on this PC?\n\nIt will be added to your Start menu and desktop.\nThe first launch downloads its AI components (about 1.5 to 3 GB), so keep the PC online."
	if already {
		msg = fmt.Sprintf("Update the installed Caption Remover to version %s?", version)
	}
	if msgBox(msg, mbOKCancel|mbIconInfo) != idOK {
		os.Exit(0)
	}
	_ = os.MkdirAll(installDir, 0o755)
	_ = os.RemoveAll(filepath.Join(installDir, "app"))
	steps := []error{
		copyFile(exe, filepath.Join(installDir, appName+".exe")),
		copyDir(filepath.Join(exeDir, "app"), filepath.Join(installDir, "app")),
		copyFile(filepath.Join(exeDir, "AppIcon.ico"), filepath.Join(installDir, "AppIcon.ico")),
	}
	for _, e := range steps {
		if e != nil {
			logf("install failed: %v", e)
			msgBox("Couldn't install Caption Remover:\n"+e.Error()+"\n\nIf it's already running, close it and try again.", mbOK|mbIconError)
			os.Exit(1)
		}
	}
	target := filepath.Join(installDir, appName+".exe")
	ico := filepath.Join(installDir, "AppIcon.ico")
	_ = powershell(`
$ws = New-Object -ComObject WScript.Shell
foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
  $s = $ws.CreateShortcut((Join-Path $d 'Caption Remover.lnk'))
  $s.TargetPath = ` + psq(target) + `; $s.WorkingDirectory = ` + psq(installDir) + `
  $s.IconLocation = ` + psq(ico+",0") + `; $s.Description = 'Remove burned-in captions from video'; $s.Save()
}`)
	reg := func(name, typ, val string) { _ = run("reg.exe", "add", regKey, "/v", name, "/t", typ, "/d", val, "/f") }
	reg("DisplayName", "REG_SZ", appName)
	reg("DisplayVersion", "REG_SZ", version)
	reg("Publisher", "REG_SZ", "The Tru Studio")
	reg("DisplayIcon", "REG_SZ", ico)
	reg("InstallLocation", "REG_SZ", installDir)
	reg("UninstallString", "REG_SZ", `"`+target+`" --uninstall`)
	reg("URLInfoAbout", "REG_SZ", "https://www.thetrustudio.com/")
	reg("NoModify", "REG_DWORD", "1")
	reg("NoRepair", "REG_DWORD", "1")
	cmd := exec.Command(target)
	cmd.Dir = installDir
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: detachedProcess}
	_ = cmd.Start()
	os.Exit(0)
}

func uninstall() {
	if msgBox("Remove Caption Remover and its downloaded components from this PC?\n\nYour cleaned videos are kept.", mbYesNo|mbIconWarn) != idYes {
		return
	}
	_ = powershell(`
foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
  Remove-Item -LiteralPath (Join-Path $d 'Caption Remover.lnk') -ErrorAction SilentlyContinue }`)
	_ = run("reg.exe", "delete", regKey, "/f")
	if logFile != nil {
		logFile.Close()
		logFile = nil
	}
	_ = os.RemoveAll(sup)
	// the running exe can't delete itself: remove the install folder a moment after we exit
	cmd := exec.Command("cmd.exe", "/c", "ping 127.0.0.1 -n 3 > nul & rmdir /s /q \""+installDir+"\"")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow | detachedProcess}
	_ = cmd.Start()
	msgBox("Caption Remover has been removed.", mbOK|mbIconInfo)
}

// ------------------------------------------------------------------ setup

func hasNvidia() bool {
	return hidden(exec.Command("nvidia-smi", "-L")).Run() == nil
}

func depsStamp(src string, gpu bool) string {
	h := sha1.New()
	b, _ := os.ReadFile(filepath.Join(src, "requirements.txt"))
	h.Write(b)
	fmt.Fprintf(h, "%s|%s|%s|%s|gpu=%v", pyURL, ffBase, lamaURL, crafURL, gpu)
	return "deps-" + hex.EncodeToString(h.Sum(nil))[:10]
}

func setup(src string, gpu bool, py string) error {
	pr := startProgress()
	defer pr.done()
	if !exists(py) {
		tgz := filepath.Join(sup, "python.tar.gz")
		if err := download(pyURL, tgz, pr, "Downloading Python…", 0, 120); err != nil {
			return fmt.Errorf("Python download: %w", err)
		}
		pr.set(125, "Unpacking Python…")
		_ = os.RemoveAll(filepath.Join(sup, "python"))
		if err := untarGz(tgz, sup); err != nil {
			return fmt.Errorf("Python unpack: %w", err)
		}
		_ = os.Remove(tgz)
	}
	bin := filepath.Join(sup, "bin")
	for i, n := range []string{"ffmpeg", "ffprobe"} {
		dst := filepath.Join(bin, n+".exe")
		if !exists(dst) {
			if err := download(ffBase+"/"+n+"-win32-x64", dst, pr, "Downloading video tools ("+n+")…", 130+i*60, 190+i*60); err != nil {
				return fmt.Errorf("%s download: %w", n, err)
			}
		}
	}
	models := filepath.Join(sup, "models")
	if !exists(filepath.Join(models, "big-lama.pt")) {
		if err := download(lamaURL, filepath.Join(models, "big-lama.pt"), pr, "Downloading AI fill model…", 250, 400); err != nil {
			return fmt.Errorf("fill model download: %w", err)
		}
	}
	if !exists(filepath.Join(models, "craft_mlt_25k.pth")) {
		z := filepath.Join(models, "craft.zip")
		if err := download(crafURL, z, pr, "Downloading text detector…", 400, 460); err != nil {
			return fmt.Errorf("text detector download: %w", err)
		}
		if err := unzipFile(z, models); err != nil {
			return fmt.Errorf("text detector unpack: %w", err)
		}
		_ = os.Remove(z)
	}
	pr.set(-1, "Installing the AI engine. This is the long step (5 to 15 minutes)…")
	_ = run(py, "-m", "pip", "install", "--upgrade", "pip", "-q", "--no-warn-script-location")
	if gpu {
		pr.set(-1, "Installing the AI engine for your NVIDIA graphics card (about 2.5 GB)…")
		if err := run(py, "-m", "pip", "install", "torch", "torchvision", "--index-url", cudaIdx, "--no-warn-script-location"); err != nil {
			logf("CUDA torch install failed, falling back to CPU: %v", err)
		}
	}
	if err := run(py, "-m", "pip", "install", "-r", filepath.Join(src, "requirements.txt"), "--no-warn-script-location"); err != nil {
		return fmt.Errorf("AI engine install: %w", err)
	}
	pr.set(1000, "Done. Opening Caption Remover…")
	time.Sleep(600 * time.Millisecond)
	return nil
}

// ------------------------------------------------------------------ main

func main() {
	_ = os.MkdirAll(sup, 0o755)
	logFile, _ = os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	defer func() {
		if logFile != nil {
			logFile.Close()
		}
	}()
	exe, _ := os.Executable()
	exeDir := filepath.Dir(exe)
	logf("=== launch v%s from %s", version, exeDir)

	if len(os.Args) > 1 && os.Args[1] == "--uninstall" {
		uninstall()
		return
	}
	if local == "" {
		msgBox("Can't find your user folder (LOCALAPPDATA).", mbOK|mbIconError)
		return
	}
	if !samePath(exeDir, installDir) {
		installSelf(exe, exeDir) // exits
	}

	bundled := filepath.Join(installDir, "app")
	src := bundled
	if upd := filepath.Join(sup, "app"); exists(filepath.Join(upd, "app.py")) && newer(readVersion(upd), readVersion(bundled)) {
		src = upd
	}
	py := filepath.Join(sup, "python", "python.exe")
	pyw := filepath.Join(sup, "python", "pythonw.exe")
	stampFile := filepath.Join(sup, ".ready")
	cur, _ := os.ReadFile(stampFile)
	gpu := strings.Contains(string(cur), "gpu=1")
	if !exists(py) {
		gpu = hasNvidia()
	}
	want := depsStamp(src, gpu)
	have := ""
	if f := strings.Fields(string(cur)); len(f) > 0 {
		have = f[0]
	}
	if have != want {
		if err := setup(src, gpu, py); err != nil {
			logf("setup failed: %v", err)
			msgBox("Setup couldn't finish: "+err.Error()+"\n\nCheck your internet connection and open Caption Remover again. It continues where it stopped.\n\nLog: "+logPath, mbOK|mbIconError)
			return
		}
		g := "0"
		if gpu {
			g = "1"
		}
		_ = os.WriteFile(stampFile, []byte(want+" gpu="+g), 0o644)
	}

	runApp(src, pyw, exe)
}

func runApp(src, pyw, exe string) {
	env := append(os.Environ(),
		"CR_SUPPORT="+sup,
		"CR_BUNDLE="+exe,
		"CR_FFMPEG="+filepath.Join(sup, "bin", "ffmpeg.exe"),
		"CR_FFPROBE="+filepath.Join(sup, "bin", "ffprobe.exe"),
		"CR_MODELS="+filepath.Join(sup, "models"),
		"CR_ICON_ICO="+filepath.Join(installDir, "AppIcon.ico"),
		"PYTHONIOENCODING=utf-8",
	)
	start := time.Now()
	cmd := exec.Command(pyw, "app.py")
	cmd.Dir, cmd.Env = src, env
	cmd.Stdout, cmd.Stderr = logFile, logFile
	err := cmd.Run()
	logf("app exited after %s: %v", time.Since(start).Round(time.Second), err)
	updDir := filepath.Join(sup, "app")
	if err != nil && samePath(src, updDir) && time.Since(start) < 20*time.Second {
		// a downloaded update crashed straight away: set it aside, run the installed version
		logf("update v%s failed to start; falling back", readVersion(src))
		_ = os.RemoveAll(filepath.Join(sup, "app.broken"))
		_ = os.Rename(updDir, filepath.Join(sup, "app.broken"))
		runApp(filepath.Join(installDir, "app"), pyw, exe)
		return
	}
	if err != nil && time.Since(start) < 20*time.Second {
		msgBox("Caption Remover couldn't start.\n\nLog: "+logPath, mbOK|mbIconError)
	}
}

