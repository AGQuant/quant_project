import re

NAV_TEMPLATE = '''<nav class="model-nav" id="scorr-nav">
  <a class="mnav-item{Home}" href="/"><span>&#8962;</span>Home</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{V8}" href="/dashboard"><span>&#9889;</span>V8</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{GVM}" href="/cio2?model=gvm"><span>&#9702;</span>GVM</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{Sector}" href="/sector"><span>&#8855;</span>Sector</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{Check}" href="/check"><span>&#10003;</span>Check</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{News}" href="/news"><span>&#128478;</span>News</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{Scanners}" href="/scanners"><span>&#8862;</span>Scanners</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{Max}" href="/cio"><span>&#9673;</span>Max</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{FPC}" href="/fpc"><span>&#9706;</span>FPC</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{QB}" href="/quant-basket"><span>&#9707;</span>QB</a>
  <div class="mnav-sep"></div>
  <a class="mnav-item{V10}" href="/v10"><span>&#9651;</span>V10</a>
</nav>'''

SLOTS = ["Home","V8","GVM","Sector","Check","News","Scanners","Max","FPC","QB","V10"]

def build_nav(active):
    d = {s: (" active" if s == active else "") for s in SLOTS}
    return NAV_TEMPLATE.format(**d)

TARGETS = {
    "scorr_home.html":     "Home",
    "v8_dashboard.html":   "V8",
    "scorr_sector.html":   "Sector",
    "scorr_check.html":    "Check",
    "scorr_news.html":     "News",
    "scorr_scanners.html": "Scanners",
    "scorr_cockpit.html":  "Max",
    "fpc_v11.html":        "FPC",
    "quant_basket.html":   "QB",
}

nav_re = re.compile(r'<nav class="model-nav".*?</nav>', re.DOTALL)
guard_re = re.compile(r"window\.self\s*!==\s*window\.top")

for fn, active in TARGETS.items():
    with open(fn, encoding="utf-8") as f:
        src = f.read()
    n = len(nav_re.findall(src))
    if n != 1:
        print(f"{fn}: SKIP - found {n} model-nav blocks (expected 1)")
        continue
    new = nav_re.sub(lambda m: build_nav(active), src, count=1)
    changed = new != src
    with open(fn, "w", encoding="utf-8") as f:
        f.write(new)
    items = new.count('class="mnav-item')
    guard = "yes" if guard_re.search(new) else "NO"
    print(f"{fn}: active={active} changed={changed} nav_items={items} embed_guard={guard}")
