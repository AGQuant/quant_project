#!/usr/bin/env python3
"""cc#1984 item 3 — regenerate the route inventory in API_REFERENCE.md FROM CODE.

WHY THIS EXISTS. The census (cc#1979) measured API_REFERENCE.md at 27% accurate: 179 of 665 live
paths documented, 485 undocumented, and 4 documented routes that no longer exist. A doc maintained
by hand against a codebase this size drifts the moment someone forgets. So the inventory is
generated, and only the inventory.

WHAT IT DOES NOT TOUCH. Everything in API_REFERENCE.md outside the two sentinel lines is written by
people and is left exactly as it is -- the conventions block, the "three TC systems" warning, every
per-router note. This script rewrites ONLY the block between:

    <!-- BEGIN GENERATED ROUTES -->
    <!-- END GENERATED ROUTES -->

If the sentinels are absent the block is appended once, and every later run edits it in place.

METHOD. A pure `ast` walk -- no imports, nothing executed, so it runs anywhere with no env and no
database. Per module it resolves `X = APIRouter(prefix="...")` to a prefix, then reads every
`@X.get/post/put/delete/patch(...)` and `@app.<method>(...)` decorator and joins the two. main.py is
read a second time for `app.include_router(<name>)` so a router that exists but is NOT mounted --
qsr_endpoints.py, dead by design per session_log 33844 -- is reported as UNMOUNTED rather than
silently listed as live.

Run:  python3 tools/gen_api_reference.py            # rewrite the block in place
      python3 tools/gen_api_reference.py --check    # exit 1 if the block is out of date
"""
import ast
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "API_REFERENCE.md")
BEGIN = "<!-- BEGIN GENERATED ROUTES -->"
END = "<!-- END GENERATED ROUTES -->"
METHODS = ("get", "post", "put", "delete", "patch", "head", "options")
SKIP_DIRS = {".git", "node_modules", "__pycache__", "reports", "design_refs", "previews", "tools"}


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def scan_module(path):
    """-> (routers {var: prefix}, routes [(method, path, handler, var)])"""
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return {}, []
    routers, routes = {}, []
    for node in ast.walk(tree):
        # X = APIRouter(prefix="/api/v8", ...)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "APIRouter":
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix":
                        prefix = _const_str(kw.value) or ""
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        routers[t.id] = prefix
        # @thing.method("/path")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                if dec.func.attr not in METHODS:
                    continue
                owner = getattr(dec.func.value, "id", None)
                if owner is None:
                    continue
                p = _const_str(dec.args[0]) if dec.args else None
                if p is None:
                    continue
                routes.append((dec.func.attr.upper(), p, node.name, owner))
    return routers, routes


def _module_mounts(path):
    """(aliases {local name -> module}, mounts [module names this file include_router's])."""
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return {}, []
    alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                alias[a.asname or a.name] = node.module
        elif isinstance(node, ast.Import):
            for a in node.names:
                alias[a.asname or a.name.split(".")[0]] = a.name
    mounts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "include_router" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name):
                mounts.append(alias.get(a.value.id, a.value.id))
            elif isinstance(a, ast.Name):
                m = alias.get(a.id)
                if m:
                    mounts.append(m)
    return alias, mounts


def mounted_modules(root):
    """Every module whose router is REACHABLE from main.py, following nested mounts transitively.

    main.py mounts a router in two shapes and a router can also be mounted inside ANOTHER router,
    so reachability is a graph problem, not a single scan of main.py. Both of the following are
    live and both were missed by earlier versions of this function:

        from v8_endpoints import router as v8_router   ->  app.include_router(v8_router)
        import earnings_calendar_diag                  ->  app.include_router(earnings_calendar_diag.router)
        news_endpoints.py:  router.include_router(knowledge_router)      # nested, one level down
        scanner_endpoints.py: router.include_router(tc_lite_router)      # nested

    The first version read only `from ... import` in main.py and called earnings_calendar_diag
    unmounted. The second read both import shapes but still only main.py, and called
    knowledge_endpoints and tc_lite_scanner unmounted -- two live routers reported as dead code.
    A generator that does that is worse than no generator, so reachability is now a proper
    breadth-first walk from main.py over every module's own include_router calls.
    """
    graph = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                _, mounts = _module_mounts(os.path.join(dirpath, fn))
                graph[rel[:-3].replace(os.sep, ".")] = mounts

    reached, queue = set(), list(graph.get("main", []))
    while queue:
        m = queue.pop()
        if m in reached:
            continue
        reached.add(m)
        queue.extend(graph.get(m, []))
    return reached


def collect():
    mounted = mounted_modules(ROOT)

    rows = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            mod = rel[:-3].replace(os.sep, ".")
            routers, routes = scan_module(path)
            if not routes:
                continue
            for method, p, handler, owner in routes:
                if owner == "app":
                    full, live = p, (rel == "main.py")
                elif owner in routers:
                    full = (routers[owner] or "") + p
                    live = (rel == "main.py") or (mod in mounted)
                else:
                    continue
                rows.append({"method": method, "path": full, "file": rel,
                             "handler": handler, "mounted": live})
    # a path defined more than once: only the FIRST registration answers (Starlette first-match-wins)
    seen = {}
    for r in sorted(rows, key=lambda r: (r["path"], r["method"], r["file"])):
        seen.setdefault((r["method"], r["path"]), []).append(r)
    for k, v in seen.items():
        if len(v) > 1:
            for r in v:
                r["shadowed"] = True
    return rows, seen


def render(rows, seen):
    live = [r for r in rows if r["mounted"]]
    unmounted = [r for r in rows if not r["mounted"]]
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    stamp = datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=5, minutes=30)))

    out = [BEGIN, "",
           "## Generated route inventory", "",
           "**Generated %s IST by `tools/gen_api_reference.py`.** Do not edit this block by hand —"
           " it is rewritten from the code on every run, and a hand edit will be lost. Everything"
           " outside the two sentinel comments is written by people and is never touched."
           % stamp.strftime("%d-%b-%Y %H:%M"), "",
           "| | count |", "|---|---|",
           "| live paths (router mounted in `main.py`, or declared directly on `app`) | **%d** |" % len(live),
           "| distinct method+path pairs | **%d** |" % len({(r["method"], r["path"]) for r in live}),
           "| defined but NOT mounted (present in code, unreachable) | **%d** |" % len(unmounted),
           "| paths defined more than once (first registration wins) | **%d** |" % len(dupes), ""]

    if dupes:
        out += ["### Duplicate definitions", "",
                "Starlette matches in registration order and returns on the first full match, so"
                " only the first of each pair answers. The rest is dead code.", "",
                "| Method | Path | Defined in |", "|---|---|---|"]
        for (m, p), v in sorted(dupes.items()):
            out.append("| %s | `%s` | %s |" % (m, p, " · ".join("`%s:%s`" % (r["file"], r["handler"]) for r in v)))
        out.append("")

    if unmounted:
        out += ["### Defined but not mounted", "",
                "These exist in code and answer nothing, because no `include_router` in `main.py`"
                " reaches them. `qsr_endpoints.py` is unmounted **on purpose** (session_log 33844) —"
                " recorded here so it is not rediscovered as a bug.", "",
                "| Method | Path | File | Handler |", "|---|---|---|---|"]
        for r in sorted(unmounted, key=lambda r: (r["file"], r["path"], r["method"])):
            out.append("| %s | `%s` | `%s` | `%s` |" % (r["method"], r["path"], r["file"], r["handler"]))
        out.append("")

    out += ["### Every live path, by file", ""]
    byfile = {}
    for r in live:
        byfile.setdefault(r["file"], []).append(r)
    for f in sorted(byfile):
        rs = sorted(byfile[f], key=lambda r: (r["path"], r["method"]))
        out += ["#### `%s` — %d route%s" % (f, len(rs), "" if len(rs) == 1 else "s"), "",
                "| Method | Path | Handler |", "|---|---|---|"]
        for r in rs:
            flag = " ⚠ shadowed" if r.get("shadowed") else ""
            out.append("| %s | `%s` | `%s`%s |" % (r["method"], r["path"], r["handler"], flag))
        out.append("")
    out.append(END)
    return "\n".join(out)


def main():
    rows, seen = collect()
    block = render(rows, seen)
    doc = open(DOC, encoding="utf-8").read() if os.path.exists(DOC) else "# Scorr API Reference\n"
    if BEGIN in doc and END in doc:
        head, rest = doc.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        new = head + block + tail
    else:
        new = doc.rstrip() + "\n\n---\n\n" + block + "\n"
    if "--check" in sys.argv:
        # Compare with the timestamp line REMOVED. It changes on every run by design, so a naive
        # diff would report OUT OF DATE immediately after a successful generate -- which it did on
        # the first version of this script, making --check useless. What --check must answer is
        # "have the ROUTES changed", not "has the clock moved".
        def _routes_only(t):
            return "\n".join(l for l in t.splitlines() if not l.startswith("**Generated "))
        if _routes_only(new) != _routes_only(doc):
            print("API_REFERENCE.md generated block is OUT OF DATE — run tools/gen_api_reference.py")
            return 1
        print("API_REFERENCE.md generated block is current (routes unchanged).")
        return 0
    open(DOC, "w", encoding="utf-8").write(new)
    live = [r for r in rows if r["mounted"]]
    print("wrote %s" % os.path.relpath(DOC, ROOT))
    print("  live routes           : %d" % len(live))
    print("  distinct method+path  : %d" % len({(r["method"], r["path"]) for r in live}))
    print("  unmounted             : %d" % len([r for r in rows if not r["mounted"]]))
    print("  duplicate definitions : %d" % len({k for k, v in seen.items() if len(v) > 1}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
