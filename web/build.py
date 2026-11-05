"""Build the iPad web app from the Mac app's studio page.
   python3 web/build.py <version> [out_dir]   ->  out_dir (default web/dist) ready to host on GitHub Pages."""
import os, re, shutil, sys
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")


def build(version, out):
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "vendor"))
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    html = html.replace('src="/vendor/', 'src="vendor/')
    html = html.replace("<title>Caption Remover</title>", "<title>Tru Studio Music</title>")
    html = html.replace('<meta name="viewport" content="width=device-width, initial-scale=1">',
                        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">')
    head = f'''<link rel="manifest" href="manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Tru Studio">
<meta name="theme-color" content="#0a0a0c">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="icon" href="icon-192.png">
<link rel="stylesheet" href="web.css?v={version}">
<script>window.WEBAPP_VERSION = "{version}";</script>
<script src="analysis.js?v={version}"></script>
<script src="local.js?v={version}"></script>
'''
    html = html.replace("<style>", head + "<style>", 1)
    # brand
    html = re.sub(r'(<div class="brand"><i>.*?</i>)Caption Remover(</div>)', r"\1Tru Studio Music\2", html, count=1, flags=re.S)
    html = html.replace("<body>", '<body>\n<div id="rotateHint"><b>Turn your iPad sideways</b>The studio needs the full width for two decks.</div>', 1)
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(html)
    for f in ("analysis.js", "local.js", "web.css", "manifest.webmanifest"):
        shutil.copy(os.path.join(WEB, f), out)
    open(os.path.join(out, "sw.js"), "w").write(open(os.path.join(WEB, "sw.js")).read().replace("__VERSION__", version))
    shutil.copy(os.path.join(ROOT, "static", "vendor", "SignalsmithStretch.js"), os.path.join(out, "vendor"))
    icon = Image.open(os.path.join(ROOT, "packaging", "icon", "icon.png")).convert("RGBA")
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        bg = Image.new("RGBA", (size, size), (10, 10, 12, 255))
        im = icon.resize((size, size), Image.LANCZOS)
        bg.alpha_composite(im)
        bg.convert("RGB").save(os.path.join(out, name))
    return out


if __name__ == "__main__":
    v = sys.argv[1] if len(sys.argv) > 1 else "dev"
    print(build(v, sys.argv[2] if len(sys.argv) > 2 else os.path.join(WEB, "dist")))
