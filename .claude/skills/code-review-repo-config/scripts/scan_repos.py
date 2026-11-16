#!/usr/bin/env python3
"""Scan a folder of git repositories and collect the facts needed to configure the Code Review Bot.

    python3 scan_repos.py /opt/projects [--repos mbp,core-module] [--out scan.json]

Prints a markdown summary; --out also writes the full JSON. Standard library only, read-only:
it runs `git ls-files`, `git rev-parse` and `git remote get-url`, and reads tracked files.

What it finds per repo: platform/workspace from the git remote, branch, language, build
system, kind (ui / service / library), size, ignore-path candidates, Maven artifacts and
dependencies, npm package and dependencies, HTTP endpoints it exposes, URL-like config keys,
API base env vars and API path prefixes (UIs), message-queue topics.

Across repos: build edges (Maven/npm, exact) and runtime edge CANDIDATES (UI path prefixes
matching a backend's endpoints, config URLs naming another repo). Candidates are hints —
confirm them in the code before using them.

Secrets: config values are only reported for URL-like keys, never for keys that look like
passwords, secrets, tokens or keys, and only the scheme+host+first path segment is kept.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

SOURCE_LANG = {".java": "java", ".kt": "java", ".ts": "js", ".tsx": "js", ".js": "js", ".jsx": "js",
               ".vue": "js", ".php": "php", ".cs": "dotnet", ".py": "python"}
TEXT_EXT = set(SOURCE_LANG) | {".properties", ".yml", ".yaml", ".xml", ".json", ".env", ".conf"}
MAX_FILE_BYTES = 1_000_000
UI_FRAMEWORKS = ("react", "vue", "@angular/core", "svelte", "next", "nuxt", "vite", "react-scripts")
SECRET_KEY = re.compile(r"pass|secret|token|apikey|api[-_.]?key|credential|private|signing", re.I)
URL_KEY = re.compile(r"(url|uri|host|endpoint|domain|base|address|server)", re.I)
URL_VALUE = re.compile(r"(https?://[^\s\"'<>,;]+)")
IGNORE_CANDIDATES = [
    ("**/target/**", re.compile(r"(^|/)target/")),
    ("dist/**", re.compile(r"(^|/)dist/")),
    ("build/**", re.compile(r"(^|/)build/")),
    ("vendor/**", re.compile(r"(^|/)vendor/")),
    ("**/*.min.js", re.compile(r"\.min\.js$")),
    ("package-lock.json", re.compile(r"(^|/)package-lock\.json$")),
    ("yarn.lock", re.compile(r"(^|/)yarn\.lock$")),
    ("composer.lock", re.compile(r"(^|/)composer\.lock$")),
    ("**/migrations/**", re.compile(r"(^|/)migrations?/")),
    ("**/generated/**", re.compile(r"(^|/)generated/")),
    ("**/*.Designer.cs", re.compile(r"\.Designer\.cs$")),
    (".idea/**", re.compile(r"(^|/)\.idea/")),
    ("**/*.properties", re.compile(r"\.properties$")),
    ("**/*.sql", re.compile(r"\.sql$")),
    ("**/*.csv", re.compile(r"\.csv$")),
    ("**/*.md", re.compile(r"\.md$")),
]


def git(path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(path), *args],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def parse_remote(url: str) -> dict:
    m = re.search(r"(bitbucket\.org|github\.com|gitlab\.[\w.]+)[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        return {"platform": None, "repo_full_name": None}
    host = m.group(1)
    platform = "bitbucket" if "bitbucket" in host else "github" if "github" in host else "gitlab"
    return {"platform": platform, "repo_full_name": f"{m.group(2)}/{m.group(3)}"}


def read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def short_url(url: str) -> str:
    """scheme://host/first-segment — enough to tell which service a URL points at."""
    m = re.match(r"(https?://[^/\s\"']+)(/[^/?#\s\"']*)?", url)
    return (m.group(1) + (m.group(2) or "")) if m else url


def strip_xml_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def scan_maven(repo: Path, files: list[str]) -> dict:
    artifacts, groups, deps, packaging, shaded = [], set(), [], set(), False
    for rel in files:
        if not rel.endswith("pom.xml"):
            continue
        text = strip_xml_comments(read(repo / rel))
        head = re.sub(r"<parent>.*?</parent>", "", text, flags=re.S)
        head = re.split(r"<(?:dependencies|dependencyManagement|build|profiles)>", head)[0]
        a = re.search(r"<artifactId>\s*([^<$]+?)\s*</artifactId>", head)
        g = re.search(r"<groupId>\s*([^<$]+?)\s*</groupId>", head) or \
            re.search(r"<parent>.*?<groupId>\s*([^<]+?)\s*</groupId>", text, flags=re.S)
        if a:
            artifacts.append(a.group(1))
        if g:
            groups.add(g.group(1))
        p = re.search(r"<packaging>\s*(\w+)\s*</packaging>", head)
        packaging.add(p.group(1) if p else "jar")
        shaded = shaded or "maven-shade-plugin" in text or "maven-assembly-plugin" in text
        for block in re.findall(r"<(dependency|parent)>(.*?)</\1>", text, flags=re.S):
            dg = re.search(r"<groupId>\s*(.*?)\s*</groupId>", block[1])
            da = re.search(r"<artifactId>\s*(.*?)\s*</artifactId>", block[1])
            if dg and da:
                deps.append((dg.group(1), da.group(1)))
    return {"artifacts": sorted(set(artifacts)), "group_ids": sorted(groups), "packaging": sorted(packaging),
            "shaded_jar": shaded, "deps": sorted(set(deps))}


def scan_npm(repo: Path, files: list[str]) -> dict:
    pkg = repo / "package.json"
    try:
        data = json.loads(read(pkg) or "{}")
    except json.JSONDecodeError:
        data = {}
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    framework = [f for f in UI_FRAMEWORKS if f in deps]
    local = {k: v for k, v in deps.items() if isinstance(v, str) and v.startswith(("file:", "link:", "git+", "git@"))}
    return {"name": data.get("name"), "framework": framework, "deps": sorted(deps), "local_deps": local}


def scan_runtime(repo: Path, files: list[str], is_ui: bool) -> dict:
    url_keys: dict[str, str] = {}
    code_urls: dict[str, str] = {}  # short URL → first file it appears in
    endpoints, context_paths, app_names, mounts = Counter(), set(), set(), set()
    env_vars, api_paths, topics = Counter(), Counter(), set()
    http_clients, proxies = Counter(), []
    for rel in files:
        suffix = Path(rel).suffix
        if suffix not in TEXT_EXT or "/test/" in rel or "node_modules/" in rel:
            continue
        text = read(repo / rel)
        if not text:
            continue
        if suffix in (".properties", ".yml", ".yaml", ".env", ".conf") or Path(rel).name.startswith(".env"):
            for line in text.splitlines():
                m = re.match(r"\s*([\w.\-]+)\s*[=:]\s*(.+)", line)
                if not m:
                    continue
                key, value = m.group(1), m.group(2).strip()
                if key in ("server.servlet.context-path", "server.context-path", "context-path"):
                    context_paths.add(value.strip("\"'"))
                    mounts.add(value.strip("\"'").rstrip("/"))
                if key in ("spring.application.name", "name") and suffix != ".env":
                    # hyatt-service-demo → hyatt-service: config keys name the service, not its environment
                    app_names.add(re.sub(r"([-_](test|demo|prod|dev|uat|stage|staging|qa|local))+$", "",
                                         value.strip("\"'")))
                if SECRET_KEY.search(key) or not URL_KEY.search(key):
                    continue
                u = URL_VALUE.search(value)
                if u:
                    url_keys.setdefault(key, short_url(u.group(1)))
        if suffix in (".java", ".kt", ".cs", ".php", ".py", ".xml"):
            for p in re.findall(r"@(?:RequestMapping|Path|RestController|Controller)\(\s*(?:value\s*=\s*)?\"(/[^\"]*)\"", text):
                endpoints["/" + p.strip("/").split("/")[0]] += 1
            for p in re.findall(r"@(?:Get|Post|Put|Delete|Patch)Mapping\(\s*(?:value\s*=\s*)?\"(/[^\"]*)\"", text):
                endpoints["/" + p.strip("/").split("/")[0]] += 1
            for p in re.findall(r"address=\"(/[^\"]+)\"", text):  # CXF servers
                mounts.add(p.rstrip("/"))
            for p in re.findall(r"<url-pattern>\s*(/[^<*]*)\*?\s*</url-pattern>", text):
                if p.strip("/"):
                    mounts.add("/" + p.strip("/").split("/")[0])
            if suffix != ".xml":
                for u in URL_VALUE.findall(text):
                    if not re.search(r"(localhost|127\.0\.0\.1|schemas?\.|w3\.org|apache\.org|springframework|xmlns|"
                                     r"example\.|java\.sun\.com|oracle\.com|jcp\.org)", u):
                        code_urls.setdefault(short_url(u), rel)
            for name, rx in (("RestTemplate", r"\bRestTemplate\b"), ("WebClient", r"\bWebClient\b"),
                             ("Feign", r"@FeignClient"), ("OkHttp", r"\bOkHttpClient\b"),
                             ("ApacheHttpClient", r"\bHttpClients?\b"), ("HttpURLConnection", r"HttpURLConnection")):
                if re.search(rx, text):
                    http_clients[name] += 1
            for t in re.findall(r"@(?:KafkaListener|RabbitListener)\([^)]*?(?:topics|queues)\s*=\s*\{?\s*\"([^\"]+)\"", text):
                topics.add(t)
            if re.search(r"class \w*Proxy\w*", text) and suffix in (".java", ".kt"):
                proxies.append(rel)
        if is_ui and suffix in (".ts", ".tsx", ".js", ".jsx", ".vue"):
            for v in re.findall(r"(?:import\.meta\.env|process\.env)\.((?:VITE_|REACT_APP_|NEXT_PUBLIC_)?\w*(?:API|URL|HOST)\w*)", text):
                env_vars[v] += 1
            for p in re.findall(r"[`'\"](/?(?:api|json|services|rest|v\d)/[\w\-/]*)", text):
                segs = p.strip("/").split("/")
                api_paths["/" + "/".join(segs[:2])] += 1
    return {
        "url_config_keys": dict(sorted(url_keys.items())[:500]),
        "code_urls": dict(sorted(code_urls.items())[:500]),
        "mounts": sorted(m for m in mounts if m)[:50],
        "endpoint_prefixes": [p for p, _ in endpoints.most_common(40)],
        "context_paths": sorted(context_paths),
        "app_names": sorted(a for a in app_names if "@" not in a)[:40],
        "http_clients": dict(http_clients),
        "proxy_classes": proxies[:10],
        "topics": sorted(topics)[:40],
        "ui_api_env_vars": [v for v, _ in env_vars.most_common(15)],
        "ui_api_path_prefixes": [p for p, _ in api_paths.most_common(25)],
    }


def scan_repo(repo: Path) -> dict:
    files = git(repo, "ls-files").splitlines()
    if not files:
        files = [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts][:50000]
    langs, loc = Counter(), 0
    for rel in files:
        lang = SOURCE_LANG.get(Path(rel).suffix)
        if lang and "node_modules/" not in rel:
            langs[lang] += 1
            text = read(repo / rel)
            loc += text.count("\n")
    build = [b for b in ("pom.xml", "build.gradle", "build.gradle.kts", "package.json", "composer.json",
                         "pyproject.toml", "requirements.txt") if (repo / b).exists()]
    if any(f.endswith((".csproj", ".sln")) for f in files):
        build.append("dotnet")
    info = {
        "slug": repo.name,
        **parse_remote(git(repo, "remote", "get-url", "origin")),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "shallow": git(repo, "rev-parse", "--is-shallow-repository") == "true",
        "language": langs.most_common(1)[0][0] if langs else None,
        "languages": dict(langs.most_common(4)),
        "build": build,
        "source_files": sum(langs.values()),
        "source_lines": loc,
        "has_claude_md": (repo / "CLAUDE.md").exists(),
        "ignore_path_candidates": [glob for glob, rx in IGNORE_CANDIDATES if any(rx.search(f) for f in files)],
    }
    info["maven"] = scan_maven(repo, files) if "pom.xml" in build else None
    info["npm"] = scan_npm(repo, files) if "package.json" in build else None
    is_ui = bool(info["npm"] and info["npm"]["framework"]) and "pom.xml" not in build
    info["runtime"] = scan_runtime(repo, files, is_ui)
    if is_ui:
        info["kind"] = "ui"
    elif info["maven"] and ("war" in info["maven"]["packaging"] or info["runtime"]["endpoint_prefixes"]):
        info["kind"] = "service"
    elif info["maven"]:
        info["kind"] = "library"
    else:
        info["kind"] = "service" if info["runtime"]["endpoint_prefixes"] else "other"
    return info


def build_edges(repos: dict) -> list[dict]:
    edges = []
    # Maven: internal = same "org.company" prefix as a groupId the scanned repos publish under
    internal_prefixes = {".".join(g.split(".")[:2]) for r in repos.values() if r["maven"] for g in r["maven"]["group_ids"]}
    owner = {}
    for name, r in repos.items():
        for a in (r["maven"] or {}).get("artifacts", []):
            owner.setdefault(a, name)

    def artifact_owner(a: str):
        if a in owner:
            return owner[a]
        # Only a version/classifier suffix: Foo-1.0-SNAPSHOT-without-dependencies is Foo, mbp-pos-crypto is not mbp
        for known in sorted(owner, key=len, reverse=True):
            if a.startswith(known + "-") and re.match(r"-(\d|SNAPSHOT|without-|with-|jar-with|shaded|all$|uber|fat)",
                                                      a[len(known):]):
                return owner[known]
        return None

    for name, r in repos.items():
        used = defaultdict(set)
        for g, a in (r["maven"] or {}).get("deps", []):
            if ".".join(g.split(".")[:2]) not in internal_prefixes:
                continue
            o = artifact_owner(a)
            if o and o != name:
                used[o].add(a)
        for o, arts in used.items():
            edges.append({"from": name, "to": o, "type": "build", "evidence": sorted(arts)[:5]})
    # npm: a dependency that is another scanned package
    npm_owner = {r["npm"]["name"]: n for n, r in repos.items() if r["npm"] and r["npm"]["name"]}
    for name, r in repos.items():
        for dep in (r["npm"] or {}).get("deps", []):
            if dep in npm_owner and npm_owner[dep] != name:
                edges.append({"from": name, "to": npm_owner[dep], "type": "build", "evidence": [dep]})
    # Runtime candidates. A backend is "reachable" by its mount points (CXF address, servlet
    # url-pattern, context path), endpoint prefixes, repo name and Spring app names.
    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9]", "", x.lower())

    prefix_owners = defaultdict(set)
    for other, o in repos.items():
        if o["kind"] == "ui":
            continue
        for q in set(o["runtime"]["mounts"]) | set(o["runtime"]["endpoint_prefixes"]):
            if q.strip("/") and "{" not in q:
                prefix_owners[q.rstrip("/")].add(other)
    # For URLs found in config/code only mount points count (CXF address, url-pattern, context
    # path), matched in full: "/api" of some external API must not match a backend's "/api/v4"
    mount_owners = defaultdict(set)
    for other, o in repos.items():
        if o["kind"] != "ui":
            for q in o["runtime"]["mounts"]:
                if len(q.strip("/")) > 2:
                    mount_owners[q.rstrip("/")].add(other)
    distinctive = {q: owners for q, owners in mount_owners.items() if len(owners) == 1}

    # Company domains: a registrable domain seen in URLs of 3+ scanned repos. Path-based
    # matching is limited to them, so third-party APIs that happen to use /xml or /api don't match.
    def domain(url: str) -> str:
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
        return ".".join(host.split(".")[-2:])

    domain_repos = defaultdict(set)
    for n, r in repos.items():
        for url in [*r["runtime"]["url_config_keys"].values(), *r["runtime"]["code_urls"]]:
            domain_repos[domain(url)].add(n)
    company_domains = {d for d, rs in domain_repos.items() if len(rs) >= 3}
    name_rx = {other: re.compile(r"(?<![a-z0-9])(" + "|".join(
        re.escape(n) for n in {other.lower(), *(a.lower() for a in o["runtime"]["app_names"])} if len(n) >= 3)
        + r")(?![a-z0-9])") for other, o in repos.items()}
    name_norm = {other: {norm(other), *(norm(a) for a in o["runtime"]["app_names"])} for other, o in repos.items()}

    def targets_of(text: str, path: str, me: str) -> set:
        hit = set()
        first = path.strip("/").split("/")[0] if path else ""
        for other in repos:
            if other == me:
                continue
            # bp.webmessages.base.url → web-messages, bp.hyatt.service.host → hyatt-service: compare without separators
            long_names = {n for n in name_norm[other] if len(n) >= 6}
            key_part = norm(text.split("=")[0]) if "=" in text else ""
            if name_rx[other].search(text.lower()) or (len(norm(first)) > 3 and norm(first) in name_norm[other]) \
                    or any(n in key_part for n in long_names):
                hit.add(other)
        for q, owners in distinctive.items():
            if path and ("/" + path.strip("/") + "/").startswith(q + "/"):
                hit |= owners - {me}
        return hit

    for name, r in repos.items():
        rt = r["runtime"]
        if r["kind"] == "ui":
            for p in rt["ui_api_path_prefixes"]:
                for q, owners in prefix_owners.items():
                    if p == q or p.startswith(q + "/") or q.startswith(p + "/"):
                        for other in owners:
                            edges.append({"from": name, "to": other, "type": "http-candidate", "evidence": [p]})
        refs = [(f"{k}={v}", v) for k, v in rt["url_config_keys"].items()]
        refs += [(f"{v} (in {f})", v) for v, f in rt["code_urls"].items()]
        for label, url in refs:
            path = re.sub(r"^https?://[^/]+", "", url) if domain(url) in company_domains else ""
            for other in targets_of(label.split(" (in ")[0], path, name):
                edges.append({"from": name, "to": other, "type": "http-candidate", "evidence": [label]})
    merged = {}
    for e in edges:
        k = (e["from"], e["to"], e["type"])
        if k in merged:
            merged[k]["evidence"] = sorted(set(merged[k]["evidence"]) | set(e["evidence"]))[:6]
        else:
            merged[k] = e
    return sorted(merged.values(), key=lambda e: (e["from"], e["type"], e["to"]))


def summary(repos: dict, edges: list[dict]) -> str:
    out = ["# Repository scan", "",
           "| repo | remote | branch | kind | lang | build | src files | lines | CLAUDE.md |",
           "|---|---|---|---|---|---|---|---|---|"]
    for n, r in sorted(repos.items()):
        out.append(f"| {n} | {r['repo_full_name'] or '?'} | {r['branch']}{' (shallow)' if r['shallow'] else ''} | "
                   f"{r['kind']} | {r['language']} | {','.join(r['build'])} | {r['source_files']} | "
                   f"{r['source_lines']} | {'yes' if r['has_claude_md'] else 'no'} |")
    out += ["", "## Build edges (exact: from depends on to)", ""]
    out += [f"- {e['from']} → {e['to']} ({', '.join(e['evidence'])})" for e in edges if e["type"] == "build"] or ["- none"]
    out += ["", "## Runtime edge candidates (verify in code before use)", ""]
    out += [f"- {e['from']} → {e['to']}: {'; '.join(e['evidence'])}" for e in edges if e["type"] != "build"] or ["- none"]
    out += ["", "## Per-repo runtime hints", ""]
    for n, r in sorted(repos.items()):
        rt = r["runtime"]
        bits = []
        if rt["http_clients"]:
            bits.append("http clients: " + ", ".join(f"{k}×{v}" for k, v in rt["http_clients"].items()))
        if rt["proxy_classes"]:
            bits.append("proxy classes: " + ", ".join(Path(p).stem for p in rt["proxy_classes"][:5]))
        if rt["context_paths"]:
            bits.append("context path: " + ", ".join(rt["context_paths"]))
        if rt["endpoint_prefixes"]:
            bits.append("endpoints: " + " ".join(rt["endpoint_prefixes"][:12]))
        if rt["ui_api_env_vars"]:
            bits.append("api env vars: " + ", ".join(rt["ui_api_env_vars"]))
        if rt["ui_api_path_prefixes"]:
            bits.append("api paths: " + " ".join(rt["ui_api_path_prefixes"][:12]))
        if rt["topics"]:
            bits.append("topics/queues: " + ", ".join(rt["topics"][:8]))
        if rt["url_config_keys"]:
            bits.append(f"url config keys: {len(rt['url_config_keys'])} (see JSON)")
        out.append(f"- **{n}**: " + ("; ".join(bits) if bits else "no runtime hints"))
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="folder that contains one git repository per sub-folder")
    ap.add_argument("--repos", help="comma-separated folder names to scan (default: all)")
    ap.add_argument("--out", help="write the full JSON here")
    args = ap.parse_args()
    root = Path(args.root)
    wanted = set(args.repos.split(",")) if args.repos else None
    repos = {}
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / ".git").exists() and (wanted is None or d.name in wanted):
            print(f"scanning {d.name}…", file=sys.stderr)
            repos[d.name] = scan_repo(d)
    if wanted:
        missing = sorted(wanted - set(repos))
        if missing:
            print(f"not found in {root}: {', '.join(missing)}", file=sys.stderr)
    edges = build_edges(repos)
    depended_on = {e["to"] for e in edges if e["type"] == "build"}
    for n, r in repos.items():
        if r["kind"] == "service" and n in depended_on and r["maven"] and "war" not in r["maven"]["packaging"] \
                and not r["runtime"]["mounts"]:
            r["kind"] = "library"
    if args.out:
        Path(args.out).write_text(json.dumps({"root": str(root), "repos": repos, "edges": edges}, indent=1))
    print(summary(repos, edges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
