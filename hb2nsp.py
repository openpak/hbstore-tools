#!/usr/bin/env python3
"""Turn hb-appstore (ForTheUsers) packages into OpenPak store NSPs, for testing.

    hb2nsp.py build <package>...        download from the 4TU CDN, wrap, write out/<tid>.nsp + .json
    hb2nsp.py upload <URL> <tid>...     send out/<tid>.* to nx-baas's store admin API (key: NX_INTERNAL_KEY)
    hb2nsp.py since <dd/mm/yyyy> <URL>  build, upload and publish every package updated since then
    hb2nsp.py add <package>... <URL>    build, upload, publish and enrich those packages now
    hb2nsp.py openpak <tag> <URL>       publish OpenPak itself (openpak.nro from that GitHub release) as 01fe000000000000
    hb2nsp.py openpak-update <tag> [URL]  OpenPak <tag> as the update 01fe000000000800; without URL, build,
                                        check and print the upload instead of sending it
    hb2nsp.py openpak-nsp <tag>         OpenPak <tag> as an installable base NSP, attached to its GitHub release
    hb2nsp.py enrich <URL>              category, full description, changelog and screenshots for every store title

The NSP is no-rights-ID (key-area crypto). Its program is ./loader (nx-hbloader that copies
romfs:/sd onto the SD card on first launch, like an hb-appstore install, and runs the NRO
from its own path); its control is the NRO's own NACP and icon. The package's atmosphere/ and
bootloader/ files are never shipped: no sysmodule or boot flag gets installed behind the
player's back. Installing one needs the FS NCA-header patch and the Loader ACID gate
(scratch/hbstore/pkg3).

An update is a full-content patch: CNMT type Patch under app|0x800, the same loader, the new
romfs and control, no delta. Its program and control NCAs carry the application id (FS
checks the NCA's program id against the running application's), only the meta NCA carries
the patch id. FS uses a patch romfs that has no indirect (BKTR) table on its own, so the
loader reads the new package straight from romfs:/. Store versions are
(major*10000 + minor*100 + micro) << 16; a base is always 0. Needs hacBrewPack's HBP_PATCH.
"""
import datetime, functools, hashlib, io, json, os, re, shutil, struct, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CDN = "https://switch.cdn.fortheusers.org"
KEYS = os.path.expanduser("~/.switch/prod.keys")
HBP = os.path.join(HERE, "hacBrewPack/hacbrewpack")
HBP_KEYS = os.path.expanduser("~/.switch/hbp.keys")  # prod.keys minus lines hacbrewpack rejects
LOADER = os.path.join(HERE, "loader")
# Working data (built packages, the uploaded-stamp ledger, scratch builds) lives outside the
# repository: HBSTORE_DATA, by default the workspace scratch folder it always used.
DATA = os.environ.get("HBSTORE_DATA", os.path.expanduser("~/REPOS/Openpak/scratch/hbstore"))
OUT = os.path.join(DATA, "out")
WORK = os.path.join(DATA, "work")
UA = {"User-Agent": "openpak-hb2nsp"}
# Each 4TU package's upstream: {"<4TU name>": "<owner/repo>"} for repos that publish GitHub
# releases. `sync` takes updates from there first (see `sources`), so a release reaches the
# store the day it is published, not when 4TU repackages it.
SOURCES_PATH = os.path.join(HERE, "sources.json")
# Never adopted by `discover`: firmware, bootloaders and loaders, not apps.
NOT_APPS = {"atmosphere-nx/atmosphere", "ctcaer/hekate", "switchbrew/nx-hbmenu", "switchbrew/nx-hbloader"}
# Not apps: Smash plugins (skyline), and hbmenu, which would overwrite the player's /hbmenu.nro.
EXCLUDE = {"hdr-launcher", "UltimateTrainingModpack", "hbmenu"}
SKIP_PREFIXES = ("atmosphere/", "bootloader/")
# Store-side notes shown at the top of a title's description (kept through every enrich).
NOTES = {
    "ftpd": "Known issue: on firmware 22.5.0 ftpd shows an error when it quits (an ftpd bug; it "
            "happens from the Homebrew Menu too). It works normally while running; close it from "
            "HOME, or use ftpsrv.",
}
LANGS = ["AmericanEnglish", "BritishEnglish", "Japanese", "French", "German", "LatinAmericanSpanish",
         "Spanish", "Italian", "Dutch", "CanadianFrench", "Portuguese", "Russian", "Korean",
         "TraditionalChinese", "SimplifiedChinese", "BrazilianPortuguese"]


@functools.cache
def gh_token():
    """GitHub API token (GITHUB_TOKEN, else gh's): 5000 requests an hour instead of 60."""
    try:
        return os.environ.get("GITHUB_TOKEN") or subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def get(url, to=None):
    h = dict(UA, Authorization="Bearer " + gh_token()) if url.startswith("https://api.github.com/") and gh_token() else UA
    for wait in (30, 90, 240, 0):  # GitHub's secondary rate limit: back off, then give up
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=600)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429) or not wait:
                raise
            time.sleep(wait)
    with r:
        if to is None:
            return r.read()
        with open(to, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)


def category(pkg):
    """The shop sidebar's category for a 4TU package; anything unnamed is misc."""
    return {"game": "game", "emu": "emulator", "tool": "tool", "advanced": "advanced",
            "theme": "theme"}.get(pkg.get("category"), "misc")


def day(v):
    """4TU dates are dd/mm/yyyy; the store wants unix seconds (0 when unknown)."""
    try:
        return int(datetime.datetime.strptime(v, "%d/%m/%Y").replace(tzinfo=datetime.timezone.utc).timestamp())
    except (TypeError, ValueError):
        return 0


def text(v):
    """4TU text carries literal backslash-n sequences; the shop wants line breaks."""
    return (v or "").replace("\\n", "\n").strip()


def enrich(base, repo):
    """For every title in the store that came from 4TU: category, full description,
    changelog, and the package's screenshot(s) hosted by the store."""
    import requests
    h = {"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}
    listed = {t["title_id"] for t in requests.get(f"{base}/internal/store/titles", headers=h).json()["titles"]}
    for p in repo:
        tid = title_id(p["name"])
        if tid not in listed:
            continue
        about = text(p.get("details")) or text(p.get("description"))
        if p["name"] in NOTES:
            about = NOTES[p["name"]] + "\n\n" + about
        r = requests.patch(f"{base}/internal/store/titles/{tid}", headers=h, json={
            "category": category(p), "changelog": text(p.get("changelog")),
            "released_at": day(p.get("appCreated")), "revised_at": day(p.get("updated")),
            "localized": {"en-US": {"name": p.get("title") or p["name"], "description": about}}})
        shots = 0
        for i, f in enumerate(["screen.png"] + [f"screen{n}.png" for n in range(1, int(p.get("screens") or 0) + 1)][:8]):
            try:
                img = get(f"{CDN}/packages/{p['name']}/{f}")
            except Exception:
                continue
            if len(img) > 2 << 20:
                tmp = os.path.join(WORK, "shot")
                os.makedirs(WORK, exist_ok=True)
                open(tmp, "wb").write(img)
                subprocess.run(["magick", tmp + "[0]", "-resize", "1280x720>", "-quality", "85", tmp + ".jpg"], check=True)
                img = open(tmp + ".jpg", "rb").read()
            ct = "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"
            if requests.put(f"{base}/internal/store/titles/{tid}/screenshots/{i}", data=img, headers={**h, "Content-Type": ct}).status_code == 200:
                shots += 1
        print(tid, p["name"], category(p), r.status_code, f"{shots} screenshot(s)", flush=True)


OPENPAK_TID = "01fe000000000000"  # reserved: OpenPak itself (hash ids never land on it in practice)


def store_version(tag):
    """The content meta version of an update to <tag>: (major*10000 + minor*100 + micro) << 16."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not m:
        raise SystemExit(f"{tag}: not a vMAJOR.MINOR.MICRO tag")
    major, minor, micro = map(int, m.groups())
    if minor > 99 or micro > 99 or major * 10000 + minor * 100 + micro >= 1 << 16:
        raise SystemExit(f"{tag}: does not fit the store version")
    return (major * 10000 + minor * 100 + micro) << 16


def openpak_build(tag, version=0):
    """OpenPak's package for the GitHub release <tag>: openpak.nro installed (hidden from hbmenu) at
    /switch/.openpak/openpak.nro (self-update rewrites that same file, and the loader only
    recopies when the store's package changes). The base, or with a version, its update.
    Returns the release and the package id (out/<id>.*)."""
    rel = json.loads(get(f"https://api.github.com/repos/openpak/switch-patcher/releases/tags/{tag}"))
    url = next(a["browser_download_url"] for a in rel["assets"] if a["name"] == "openpak.nro")
    nro = get(url)
    os.makedirs(WORK, exist_ok=True)
    zpath = os.path.join(WORK, "openpak-pkg.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("switch/.openpak/openpak.nro", nro)
    pkg = {"name": "OpenPak", "title": "OpenPak", "author": "OpenPak", "version": tag.lstrip("v"),
           "binary": "/switch/.openpak/openpak.nro", "category": "tool",
           "url": "https://github.com/openpak/switch-patcher", "license": "OpenPak",
           "description": "Connects this console to OpenPak: online play, the eShop, news and system support."}
    real_get, real_tid = get, title_id
    def local_get(u, to=None):
        if u.endswith("/zips/OpenPak.zip"):
            shutil.copy(zpath, to)
            return None
        return real_get(u, to)
    globals()["get"], globals()["title_id"] = local_get, (lambda n: OPENPAK_TID)
    try:
        tid = build("OpenPak", [pkg], version)
    finally:
        globals()["get"], globals()["title_id"] = real_get, real_tid
        os.remove(zpath)
    meta = json.load(open(os.path.join(OUT, f"{tid}.json")))
    meta["title"]["distribution"] = {"source": "https://github.com/openpak/switch-patcher/releases/tag/" + tag,
                                     "permission": "OpenPak's own software"}
    json.dump(meta, open(os.path.join(OUT, f"{tid}.json"), "w"), indent=1)
    return rel, tid


# ---------- titles from a GitHub release (not on 4TU) ----------
# Recorded in $HBSTORE_DATA/out/github.json ({"owner/repo": {"tag", "category"}}) so `sync`
# ships each new release as an update.

def gh_api(path):
    return json.loads(get("https://api.github.com/" + path))


def release_zip(rel, to, binary=None, hint="", base_zip=None, only=False):
    """Write the package zip for a GitHub release to <to> and return the NRO's path in it
    (None when the release has nothing usable). A .nro asset lands at <binary> (default
    switch/<hint>/<asset>) on top of base_zip's other files (4TU's package: configs, data);
    a .zip asset is taken whole when it holds <binary>, or without one, a single (or
    <hint>-named) switch/**.nro; with only, just <binary> out of it (one app from a bundle)."""
    low = hint.lower()
    nros = [a for a in rel["assets"] if a["name"].lower().endswith(".nro")]
    want = os.path.basename(binary or "").lower()
    a = next((a for a in nros if a["name"].lower() in (want, low + ".nro")), nros[0] if len(nros) == 1 else None)
    if a:
        binary = binary or f"switch/{hint}/{a['name']}"
        files = {}
        if base_zip:
            with zipfile.ZipFile(base_zip) as z:
                files = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
        files[binary] = get(a["browser_download_url"])
        with zipfile.ZipFile(to, "w", zipfile.ZIP_DEFLATED) as z:
            for n, d in files.items():
                z.writestr(n, d)
        return binary
    for a in rel["assets"]:
        if not a["name"].lower().endswith(".zip") or a["size"] > 300 << 20:
            continue
        data = get(a["browser_download_url"])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        if binary is None:
            found = [n for n in names if n.startswith("switch/") and n.lower().endswith(".nro")]
            pick = [n for n in found if os.path.basename(n).lower() == low + ".nro"] or found
            if len(pick) != 1:
                continue
            binary = pick[0]
        if binary in names:
            if only:
                with zipfile.ZipFile(to, "w") as z:
                    z.writestr(binary, zipfile.ZipFile(io.BytesIO(data)).read(binary))
            else:
                open(to, "wb").write(data)
            return binary
    return None


def github_build(slug, tag, version=0, category="tool", binary=None, only=False):
    """Package release <tag> of github.com/<slug> (its .nro asset, or the NRO in its zip); the
    title id is derived from "gh:<slug>" so it never collides with a 4TU package.
    Returns (release, repo, id, the NRO's path in the package)."""
    repo_info, rel = gh_api("repos/" + slug), gh_api(f"repos/{slug}/releases/tags/{tag}")
    owner, name = slug.split("/")
    os.makedirs(WORK, exist_ok=True)
    zpath = os.path.join(WORK, "github-pkg.zip")
    try:
        binary = release_zip(rel, zpath, binary, name, only=only)
        if binary is None:
            raise SystemExit(f"{slug} {tag}: no .nro asset, nor a zip with one under switch/")
        nacp = nro_assets(zipfile.ZipFile(zpath).read(binary), {"name": "", "title": name, "author": owner,
                                                                  "icon_url": repo_info["owner"]["avatar_url"]})[1]
        title = next((e for e in (nacp[i * 0x300:i * 0x300 + 0x200].split(b"\0")[0] for i in range(16)) if e), b"").decode(errors="replace")
        key = "gh:" + slug.lower() + ("/" + os.path.basename(binary).lower() if only else "")
        pkg = {"name": key, "title": title or name, "author": owner,
               "version": tag.lstrip("v"), "binary": "/" + binary, "category": category,
               "url": repo_info["html_url"], "license": (repo_info.get("license") or {}).get("spdx_id") or "none published",
               "description": repo_info.get("description") or "", "icon_url": repo_info["owner"]["avatar_url"]}
        tid = build(pkg["name"], [pkg], version, zpath)
    finally:
        if os.path.exists(zpath):
            os.remove(zpath)
    meta = json.load(open(os.path.join(OUT, f"{tid}.json")))
    meta["title"]["distribution"] = {"source": rel["html_url"],
                                     "permission": f"{pkg['license']}, from {slug} {tag}, OpenPak testing only"}
    json.dump(meta, open(os.path.join(OUT, f"{tid}.json"), "w"), indent=1)
    return rel, repo_info, tid, binary


def github_meta(base, tid, rel, repo_info, category):
    """Store text for a GitHub title: description, changelog (release notes), dates."""
    import requests
    stamp = lambda v: int(datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    requests.patch(f"{base}/internal/store/titles/{tid[:-3]}000", headers={"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]},
                   json={"changelog": (rel.get("body") or "").strip(), "category": category,
                         "released_at": stamp(repo_info["created_at"]), "revised_at": stamp(rel["published_at"])})


def github_add(slug, tag, base, category="tool", binary=None, only=False):
    rel, repo_info, tid, binary = github_build(slug, tag, 0, {"emulator": "emu"}.get(category, category), binary, only)
    upload(base, tid)
    publish(base, tid)
    github_meta(base, tid, rel, repo_info, category)
    os.remove(os.path.join(OUT, f"{tid}.nsp"))
    path = os.path.join(OUT, "github.json")
    known = json.load(open(path)) if os.path.exists(path) else {}
    known[slug + ("#" + binary if only else "")] = {"tag": tag, "category": category, "binary": binary, "only": only}
    json.dump(known, open(path, "w"), indent=1)
    print(f"{tid}: {slug} {tag} published", flush=True)


def github_sync(base):
    """Ship every recorded GitHub title's newer latest release as an update."""
    import requests
    path = os.path.join(OUT, "github.json")
    known = json.load(open(path)) if os.path.exists(path) else {}
    h = {"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}
    for entry, rec in known.items():
        if rec.get("pin"):  # e.g. Daybreak: tied to the Atmosphère the console runs
            continue
        slug = entry.split("#")[0]
        try:
            latest = json.loads(get(f"https://api.github.com/repos/{slug}/releases/latest"))["tag_name"]
            if latest == rec["tag"]:
                continue
            tid = title_id("gh:" + slug.lower() + ("/" + os.path.basename(rec["binary"]).lower() if rec.get("only") else ""))
            current = requests.get(f"{base}/internal/store/titles/{tid}", headers=h).json().get("version", 0)
            version = ((current >> 16) + 1) << 16
            if os.environ.get("HBSTORE_DRY"):
                print(f"would update: {slug} {rec['tag']} -> {latest} ({version})")
                continue
            rel, repo_info, pid, _ = github_build(slug, latest, version, {"emulator": "emu"}.get(rec["category"], rec["category"]), rec.get("binary"), rec.get("only", False))
            upload(base, pid)
            github_meta(base, pid, rel, repo_info, rec["category"])
            os.remove(os.path.join(OUT, f"{pid}.nsp"))
            rec["tag"] = latest
            json.dump(known, open(path, "w"), indent=1)
            print(f"update: {slug} -> {latest} ({version})", flush=True)
        except (SystemExit, Exception) as e:
            print(f"  FAILED {slug}: {str(e)[:200]}", flush=True)


# ---------- upstream: 4TU packages updated straight from their GitHub releases ----------

def sources(repo):
    """Write sources.json: each 4TU package whose url is a GitHub repo (renames followed) with a
    latest release carrying a .nro or .zip asset. Whether that asset really holds the package's
    NRO is only known at build time; a miss falls back to 4TU."""
    out = {}
    for p in repo:
        m = re.match(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", p.get("url") or "")
        if not m or p["name"] in EXCLUDE or not p.get("binary", "").lower().endswith(".nro"):
            continue
        try:
            slug = gh_api(f"repos/{m[1]}/{m[2].removesuffix('.git')}")["full_name"]
            rel = gh_api(f"repos/{slug}/releases/latest")
        except Exception:
            continue
        if any(a["name"].lower().endswith((".nro", ".zip")) for a in rel["assets"]):
            out[p["name"]] = slug
    json.dump(out, open(SOURCES_PATH, "w"), indent=1, sort_keys=True)
    print(f"sources: {len(out)} of {len(repo)} packages have GitHub releases", flush=True)


def upstream_update(base, p, repo, t, slug, seen, broken):
    """One 4TU package against its upstream's latest release: "same" when there is nothing new,
    (patch id, release) once a newer release has shipped as an update, None to fall back to 4TU
    (no release, nothing usable in it). seen[name] is the last upstream tag the store has; the
    first look counts a release no newer than 4TU's package (+2 days) as already in the store."""
    name = p["name"]
    rel = gh_api(f"repos/{slug}/releases/latest")
    tag = rel["tag_name"]
    seen_as = f"{tag}@{rel['published_at']}"  # a rolling tag ("release", "nightly") moves its date
    if seen.get(name) in (tag, seen_as):
        seen[name] = seen_as  # an entry from before dates were kept: from now on it has one
        return "same"
    published = int(datetime.datetime.fromisoformat(rel["published_at"].replace("Z", "+00:00")).timestamp())
    if name not in seen and published <= day(p.get("updated")) + 2 * 86400:
        seen[name] = seen_as
        return "same"
    if broken.get(name) == "gh:" + tag:
        return None
    version = ((t.get("version", 0) >> 16) + 1) << 16
    if os.environ.get("HBSTORE_DRY"):
        print(f"would update from {slug}: {name} {seen.get(name) or p.get('version')} -> {tag} ({version})")
        return "same"
    print(f"update: {name} -> {version} ({slug} {tag})", flush=True)
    os.makedirs(WORK, exist_ok=True)
    zpath, base_zip = os.path.join(WORK, "upstream-pkg.zip"), os.path.join(WORK, "upstream-4tu.zip")
    try:
        try:
            get(f"{CDN}/zips/{name}.zip", base_zip)  # 4TU's other files (configs, data), when 4TU still answers
        except Exception:
            base_zip = None
        if release_zip(rel, zpath, p["binary"].lstrip("/"), base_zip=base_zip) is None:
            print(f"  {slug} {tag}: no usable asset, 4TU instead", flush=True)
            broken[name] = "gh:" + tag
            return None
        pid = build(name, repo, version, zpath)
    finally:
        for f in (zpath, base_zip):
            if f and os.path.exists(f):
                os.remove(f)
    meta = json.load(open(os.path.join(OUT, f"{pid}.json")))
    meta["title"]["distribution"] = {"source": rel["html_url"],
                                     "permission": f"{p.get('license') or 'unknown'}, from {slug} {tag}, OpenPak testing only"}
    json.dump(meta, open(os.path.join(OUT, f"{pid}.json"), "w"), indent=1)
    upload(base, pid)
    os.remove(os.path.join(OUT, f"{pid}.nsp"))
    seen[name] = seen_as
    return pid, rel


# ---------- discover: homebrew on GitHub that neither 4TU nor the store has ----------
DISCOVER_QUERIES = ["topic:switch-homebrew", "topic:nintendo-switch-homebrew", "topic:libnx", "topic:nro",
                    "topic:homebrew-switch", "topic:nintendo-switch", "topic:switch-homebrew-app",
                    "nro in:readme", "libnx in:readme", "switch homebrew in:description"]


def guess_category(r):
    words = " ".join([r["full_name"], r.get("description") or ""] + r.get("topics", [])).lower()
    if "emulat" in words or "libretro" in words:
        return "emulator"
    if re.search(r"\bgames?\b|\bport\b|puzzle|platformer|shooter", words):
        return "game"
    if "theme" in words:
        return "theme"
    return "tool"


def discover(repo, since="2022-01-01"):
    """GitHub repos (topic and readme searches) whose latest release, published on or after
    <since>, carries a Switch NRO, minus what 4TU, sources.json or github.json already
    cover and NROs 4TU already ships under another repo. Written to out/discover.json for
    review; `adopt` publishes what is left in it."""
    known = {v.lower() for v in json.load(open(SOURCES_PATH)).values()} if os.path.exists(SOURCES_PATH) else set()
    gh_path = os.path.join(OUT, "github.json")
    known |= {k.lower() for k in (json.load(open(gh_path)) if os.path.exists(gh_path) else {})}
    for p in repo:
        m = re.match(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)", p.get("url") or "")
        if m:
            known.add(m[1].lower().removesuffix(".git"))
    nro_names = {os.path.basename(p.get("binary", "")).lower() for p in repo}
    found = {}
    for q in DISCOVER_QUERIES:
        for page in range(1, 11):
            res = gh_api(f"search/repositories?q={urllib.parse.quote(q + ' fork:false')}&per_page=100&page={page}")
            for r in res["items"]:
                found.setdefault(r["full_name"], r)
            time.sleep(2.5)  # search: 30 requests a minute
            if len(res["items"]) < 100:
                break
    print(f"discover: {len(found)} repos searched", flush=True)
    picked = []
    os.makedirs(WORK, exist_ok=True)
    # Every repo looked at, with its pick (or null), so an interrupted run resumes.
    checked_path = os.path.join(OUT, "discover-checked.json")
    checked = json.load(open(checked_path)) if os.path.exists(checked_path) else {}
    for slug, r in sorted(found.items()):
        if slug.lower() in known or slug.lower() in NOT_APPS:
            continue
        if slug in checked:
            if checked[slug]:
                picked.append(checked[slug])
            continue
        checked[slug] = None
        if len(checked) % 25 == 0:
            json.dump(checked, open(checked_path, "w"))
        try:
            rel = gh_api(f"repos/{slug}/releases/latest")
        except Exception:
            continue
        if rel["published_at"][:10] < since:
            continue
        if not any(a["name"].lower().endswith((".nro", ".zip")) for a in rel["assets"]):
            continue
        tmp = os.path.join(WORK, "discover.zip")
        try:
            binary = release_zip(rel, tmp, None, slug.split("/")[1])
        except Exception:
            binary = None
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        if not binary or os.path.basename(binary).lower() in nro_names:
            continue
        picked.append({"slug": slug, "tag": rel["tag_name"], "published": rel["published_at"][:10],
                       "binary": binary, "category": guess_category(r), "stars": r["stargazers_count"],
                       "description": r.get("description") or ""})
        checked[slug] = picked[-1]
        print(f"  {slug} {rel['tag_name']} ({picked[-1]['category']}, {r['stargazers_count']} stars) {binary}", flush=True)
    json.dump(checked, open(checked_path, "w"))
    picked.sort(key=lambda c: -c["stars"])
    json.dump(picked, open(os.path.join(OUT, "discover.json"), "w"), indent=1)
    print(f"discover: {len(picked)} new apps in out/discover.json", flush=True)


def adopt(base):
    """Publish every out/discover.json entry not in the store yet (github.json records each)."""
    gh_path = os.path.join(OUT, "github.json")
    for c in json.load(open(os.path.join(OUT, "discover.json"))):
        if c["slug"] in (json.load(open(gh_path)) if os.path.exists(gh_path) else {}):
            continue
        try:
            github_add(c["slug"], c["tag"], base, c["category"], c["binary"])
        except (SystemExit, Exception) as e:
            print(f"  FAILED {c['slug']}: {str(e)[:200]}", flush=True)
            shutil.rmtree(os.path.join(WORK, title_id("gh:" + c["slug"].lower())), ignore_errors=True)


def load_repo():
    """4TU's index, cached in out/repo.json so the store keeps its metadata if 4TU goes away."""
    cache = os.path.join(OUT, "repo.json")
    try:
        packages = json.loads(get(f"{CDN}/repo.json"))["packages"]
    except Exception as e:
        print(f"4TU unreachable ({e}), using {cache}", flush=True)
        return json.load(open(cache))
    os.makedirs(OUT, exist_ok=True)
    json.dump(packages, open(cache, "w"))
    return packages


def openpak(tag, base):
    """OpenPak as a store title (the base, version 0), release notes as the changelog."""
    import requests
    rel, tid = openpak_build(tag)
    upload(base, tid)
    publish(base, tid)
    h = {"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}
    requests.patch(f"{base}/internal/store/titles/{tid}", headers=h,
                   json={"changelog": (rel.get("body") or "").strip(), "category": "tool"})
    os.remove(os.path.join(OUT, f"{tid}.nsp"))
    print(f"{tid}: OpenPak {tag} published", flush=True)


def openpak_update(tag, base=None):
    """OpenPak <tag> as the update of 01fe000000000000. The title is already published, so the
    upload alone makes the version list advertise it. With no base URL: build, check, and print
    the requests; out/01fe000000000800.nsp stays for a later `upload`."""
    rel, pid = openpak_build(tag, store_version(tag))
    changelog = {"changelog": (rel.get("body") or "").strip(),
                 "distribution": {"source": "https://github.com/openpak/switch-patcher/releases/tag/" + tag,
                                  "permission": "OpenPak's own software"}}
    if base is None:
        upload("<URL>", pid, dry=True)
        print(f"PATCH <URL>/internal/store/titles/{OPENPAK_TID}  {json.dumps(changelog)[:200]}")
        return
    import requests
    upload(base, pid)
    requests.patch(f"{base}/internal/store/titles/{OPENPAK_TID}",
                   headers={"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}, json=changelog)
    os.remove(os.path.join(OUT, f"{pid}.nsp"))
    print(f"{pid}: OpenPak {tag} uploaded as the update", flush=True)
    try:
        openpak_nsp(tag)
    except Exception as e:  # the store update is already out; the GitHub NSP can be retried with `openpak-nsp`
        print(f"WARNING: no GitHub NSP for {tag}: {e}", flush=True)


def openpak_nsp(tag):
    """OpenPak <tag> as a base NSP (version 0, the same title the eShop installs, so eShop updates
    apply on top) attached to the GitHub release as OpenPak-<tag>.nsp, for installing by any
    means. Built here, not in CI: packaging needs the Nintendo keys, which must not sit in a public
    repository's runners."""
    _, pid = openpak_build(tag, 0)
    out = os.path.join(OUT, f"OpenPak-{tag}.nsp")
    shutil.move(os.path.join(OUT, f"{pid}.nsp"), out)
    try:
        subprocess.run(["gh", "release", "upload", tag, out, "--clobber", "--repo", "openpak/switch-patcher"], check=True)
    finally:
        os.remove(out)
    print(f"OpenPak-{tag}.nsp attached to the {tag} release", flush=True)


def loaderfix(na_id, base, repo):
    """Ship a rebuilt loader to the titles an account owns: each becomes an update one store
    revision (the low 16 bits of the version) above what the store serves now."""
    import requests
    h = {"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}
    ents = requests.get(f"{base}/internal/store/accounts/-/entitlements?na_id={na_id}", headers=h).json()["entitlements"]
    owned = {e["title_id"] for e in ents if e["status"] == "active"}
    names = {title_id(p["name"]): p["name"] for p in repo}
    for tid in sorted(owned):
        t = requests.get(f"{base}/internal/store/titles/{tid}", headers=h)
        if t.status_code != 200:
            print(f"{tid}: not in the store, skipped", flush=True)
            continue
        t = t.json()
        current = max((t.get("application") or {}).get("version", 0), (t.get("update") or {}).get("version", 0))
        if tid == OPENPAK_TID:
            rel = json.loads(get("https://api.github.com/repos/openpak/switch-patcher/releases/latest"))
            _, pid = openpak_build(rel["tag_name"], max(current, store_version(rel["tag_name"])) + 1)
        elif tid in names:
            build(names[tid], repo, current + 1)
            pid = tid[:-3] + "800"
        else:
            print(f"{tid}: not a 4TU package, skipped", flush=True)
            continue
        upload(base, pid)
        os.remove(os.path.join(OUT, f"{pid}.nsp"))
        print(f"{tid}: loader fix uploaded as update {current + 1}", flush=True)


def rebuild(base, repo, only=None):
    """Rebuild every 4TU title the store lists with the current loader and replace its base
    package (installed copies get the same fix through `loaderfix` updates). One failure is
    logged and the run goes on."""
    import requests
    h = {"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}
    listed = {t["title_id"] for t in requests.get(f"{base}/internal/store/titles", headers=h).json()["titles"]}
    todo = [p for p in repo if title_id(p["name"]) in listed and (not only or p["name"] in only)]
    failed = []
    for i, p in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {p['name']}", flush=True)
        try:
            tid = build(p["name"], repo)
            upload(base, tid)
            os.remove(os.path.join(OUT, f"{tid}.nsp"))
        except (SystemExit, Exception) as e:
            failed.append((p["name"], str(e)[:200]))
            print(f"  FAILED {p['name']}: {str(e)[:200]}", flush=True)
            shutil.rmtree(os.path.join(WORK, title_id(p["name"])), ignore_errors=True)
    print(f"rebuilt {len(todo) - len(failed)}, failed {len(failed)}", flush=True)
    for n, e in failed:
        print(f"  {n}: {e}", flush=True)


def title_id(name):
    # 01FE…000: inside the application range, above every retail id so far, stable per package.
    return "01fe" + hashlib.sha1(name.encode()).hexdigest()[:9] + "000"


def nro_assets(nro, pkg):
    """The NRO's icon and NACP; an NRO without them gets the store's icon and a NACP built
    from the store's name, author and version."""
    if nro[0x10:0x14] != b"NRO0":
        raise ValueError("not an NRO")
    base = struct.unpack_from("<I", nro, 0x18)[0]
    if nro[base:base + 4] == b"ASET":
        io_, isz, no, nsz = struct.unpack_from("<QQQQ", nro, base + 8)
        if isz and nsz:
            return nro[base + io_:base + io_ + isz], nro[base + no:base + no + nsz]
    nacp = bytearray(0x4000)
    nacp[0:0x300] = (pkg["title"] or pkg["name"]).encode()[:0x1ff].ljust(0x200, b"\0") + \
        (pkg.get("author") or "").encode()[:0xff].ljust(0x100, b"\0")
    nacp[0x3060:0x3070] = (pkg.get("version") or "").encode()[:0xf].ljust(0x10, b"\0")
    return get(pkg.get("icon_url") or f"{CDN}/packages/{pkg['name']}/icon.png"), bytes(nacp)


def fix_nacp(nacp, pkg):
    nacp = bytearray(nacp.ljust(0x4000, b"\0"))
    entries = [bytes(nacp[i * 0x300:(i + 1) * 0x300]) for i in range(16)]
    filled = next((e for e in entries if e[0]), None)
    if filled is None:  # no name at all: use the store's
        filled = pkg["title"].encode()[:0x1ff].ljust(0x200, b"\0") + pkg["author"].encode()[:0xff].ljust(0x100, b"\0")
    for i, e in enumerate(entries):
        if not e[0]:
            nacp[i * 0x300:(i + 1) * 0x300] = filled
    if not struct.unpack_from("<I", nacp, 0x302C)[0]:
        struct.pack_into("<I", nacp, 0x302C, 1)
    return bytes(nacp)


def jpeg_256(icon, tmp):
    p = os.path.join(tmp, "icon.in")
    open(p, "wb").write(icon)
    # A progressive JPEG passes every size check and still breaks the console: ns shrinks the
    # icon to 174x174 through capsrv's jpegdec, which runs out of work memory on multi-scan
    # images (2206-1017), so the title never gets its name/icon cache entry -- a blank HOME
    # tile that cannot launch (hardware 2026-10-05). Only a baseline JPEG passes through.
    size = subprocess.run(["identify", "-format", "%m %w %h %[interlace]", p + "[0]"], capture_output=True, text=True).stdout
    if size.strip() == "JPEG 256 256 None" and len(icon) <= 0x20000:
        return icon
    o = os.path.join(tmp, "icon.jpg")
    subprocess.run(["magick", p + "[0]", "-resize", "256x256!", "-strip", "-interlace", "none",
                    "-quality", "90", o], check=True)
    return open(o, "rb").read()


def nca_type(path):
    out = subprocess.run(["hactool", "-k", HBP_KEYS, path], capture_output=True, text=True).stdout
    m = re.search(r"Content Type:\s+(\w+)", out)
    return {"Program": "program", "Meta": "meta", "Control": "control"}[m.group(1)]


def check(nca_dir, nsp, tid, version):
    """Read the package back the way the console will: content ids are the first 16 bytes of each
    NCA's SHA-256; program/control carry the application id, the meta NCA the CNMT's id; the
    CNMT (type, id, version, extended header, extended data, content records) matches the NCAs;
    the NSP holds exactly these files."""
    app, patch = int(tid, 16), version > 0
    ncas = sorted(os.listdir(nca_dir))
    meta_nca = next(f for f in ncas if f.endswith(".cnmt.nca"))
    found = {}
    for f in ncas:
        p = os.path.join(nca_dir, f)
        digest = hashlib.sha256(open(p, "rb").read()).digest()
        if digest[:16].hex() != f.split(".")[0]:
            raise SystemExit(f"{f}: content id is not sha256[:16]")
        out = subprocess.run(["hactool", "-k", HBP_KEYS, p], capture_output=True, text=True).stdout
        pid = int(re.search(r"Title ID:\s+([0-9a-fA-F]{16})", out).group(1), 16)
        want = app | 0x800 if patch and f == meta_nca else app
        if pid != want:
            raise SystemExit(f"{f}: program id {pid:016x}, want {want:016x}")
        found[f.split(".")[0]] = (nca_type(p), os.path.getsize(p), digest)
    sec = os.path.join(nca_dir, "..", "cnmt")
    subprocess.run(["hactool", "-k", HBP_KEYS, "--section0dir", sec, os.path.join(nca_dir, meta_nca)],
                   capture_output=True, check=True)
    (name,) = os.listdir(sec)
    c = open(os.path.join(sec, name), "rb").read()
    cid, ver, typ, _, ext_size, n, m = struct.unpack_from("<QIBBHHH", c, 0)
    want_type, want_id, want_name = (0x81, app | 0x800, "Patch") if patch else (0x80, app, "Application")
    if (typ, cid, ver, m, name) != (want_type, want_id, version, 0, f"{want_name}_{want_id:016x}.cnmt"):
        raise SystemExit(f"cnmt {name}: type {typ:#x} id {cid:016x} version {ver:#x} metas {m}")
    if patch:
        ext_app, sysver, data_size = struct.unpack_from("<QII", c, 0x20)
        if (ext_size, ext_app, sysver, data_size) != (0x18, app, 0, 0x1C):
            raise SystemExit(f"cnmt patch header: size {ext_size:#x} app {ext_app:016x} data {data_size:#x}")
        if c[0x20 + ext_size + n * 0x38:][:data_size] != bytes(0x1C):
            raise SystemExit("cnmt patch extended data is not the empty header")
    else:
        data_size = 0
        if (ext_size, struct.unpack_from("<Q", c, 0x20)[0]) != (0x10, app | 0x800):
            raise SystemExit("cnmt application header: wrong size or patch id")
    if len(c) != 0x20 + ext_size + n * 0x38 + data_size + 0x20:
        raise SystemExit(f"cnmt: {len(c)} bytes, not header + records + data + digest")
    records = {}
    for i in range(n):
        h, rid, size, kind = struct.unpack_from("<32s16s6sB", c, 0x20 + ext_size + i * 0x38)
        records[rid.hex()] = ({1: "program", 3: "control", 4: "htmldocument", 5: "legalinformation"}[kind],
                              int.from_bytes(size, "little"), h)
    if records != {k: v for k, v in found.items() if v[0] != "meta"}:
        raise SystemExit("cnmt content records do not match the NCAs")
    raw = open(nsp, "rb").read(0x10000)
    count, strsize = struct.unpack_from("<4xII", raw, 0)
    strings = raw[0x10 + count * 0x18:][:strsize]
    entries = {}
    for i in range(count):
        off, size, noff = struct.unpack_from("<QQI", raw, 0x10 + i * 0x18)
        entries[strings[noff:strings.index(b"\0", noff)].decode()] = size
    if raw[:4] != b"PFS0" or entries != {f: os.path.getsize(os.path.join(nca_dir, f)) for f in ncas}:
        raise SystemExit("nsp entries do not match the NCAs")
    shutil.rmtree(sec)


def build(name, repo, version=0, zsrc=None):
    """Wrap a package as out/<id>.nsp + .json: the base (version 0, under the title id) or an
    update (a version above 0, under the patch id). zsrc: the package zip, instead of 4TU's."""
    pkg = next((p for p in repo if p["name"] == name), None)
    if pkg is None:
        raise SystemExit(f"{name}: not in the 4TU index")
    tid = title_id(name)
    nid = tid[:-3] + "800" if version else tid  # the package's own id: the patch id for an update
    w = os.path.join(WORK, nid)
    shutil.rmtree(w, ignore_errors=True)
    os.makedirs(w)
    zpath = os.path.join(w, "pkg.zip")
    if zsrc:
        shutil.copy(zsrc, zpath)
    else:
        get(f"{CDN}/zips/{name}.zip", zpath)
    z = zipfile.ZipFile(zpath)
    binary = pkg["binary"].lstrip("/")
    if binary not in z.namelist() or binary.startswith(SKIP_PREFIXES):
        raise SystemExit(f"{name}: no launchable {binary} in the package")
    nro = z.read(binary)
    icon, nacp = nro_assets(nro, pkg)
    stamp = hashlib.sha256(open(zpath, "rb").read()).hexdigest()

    for d in ("exefs", "romfs/sd", "control"):
        os.makedirs(os.path.join(w, d))
    shutil.copy(os.path.join(LOADER, "hbl.nso"), os.path.join(w, "exefs/main"))
    shutil.copy(os.path.join(LOADER, "hbl.npdm"), os.path.join(w, "exefs/main.npdm"))
    for info in z.infolist():
        if info.is_dir() or info.filename.startswith(SKIP_PREFIXES) or info.filename in ("info.json", "manifest.install"):
            continue
        dest = os.path.normpath(os.path.join(w, "romfs/sd", info.filename))
        if not dest.startswith(os.path.join(w, "romfs/sd") + os.sep):
            continue  # a path that escapes the tree is not a package file
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with z.open(info) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    open(os.path.join(w, "romfs/app.path"), "w").write("sdmc:/" + binary)
    open(os.path.join(w, "romfs/app.stamp"), "w").write(stamp)
    open(os.path.join(w, "control/control.nacp"), "wb").write(fix_nacp(nacp, pkg))
    icon = jpeg_256(icon, w)
    for lang in LANGS:
        open(os.path.join(w, f"control/icon_{lang}.dat"), "wb").write(icon)

    env = dict(os.environ, HBP_PATCH="1", HBP_TITLE_VERSION=str(version)) if version else None
    r = subprocess.run([HBP, "-k", HBP_KEYS, "--titleid", tid, "--nologo", "--keepncadir",
                        "--exefsdir", "exefs", "--romfsdir", "romfs", "--controldir", "control",
                        "--nspdir", "nsp", "--ncadir", "nca", "--tempdir", "temp", "--backupdir", "backup"],
                       cwd=w, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise SystemExit(f"{name}: hacbrewpack failed\n{r.stdout}\n{r.stderr}")
    nca_dir = os.path.join(w, "nca")
    contents = []
    for f in sorted(os.listdir(nca_dir)):
        p = os.path.join(nca_dir, f)
        data = open(p, "rb").read()
        h = hashlib.sha256(data).hexdigest()
        contents.append({"content_id": f.split(".")[0], "content_type": nca_type(p), "size": len(data), "sha256": h})
    check(nca_dir, os.path.join(w, "nsp", f"{nid}.nsp"), tid, version)

    os.makedirs(OUT, exist_ok=True)
    shutil.move(os.path.join(w, "nsp", f"{nid}.nsp"), os.path.join(OUT, f"{nid}.nsp"))
    shutil.copy(os.path.join(w, "control/icon_AmericanEnglish.dat"), os.path.join(OUT, f"{nid}.jpg"))
    label = nacp[0x3060:0x3070].split(b"\0")[0].decode(errors="replace") or pkg.get("version", "")
    meta = {
        "title": {
            "title_id": tid,
            "publisher": pkg.get("author") or "unknown",
            "category": category(pkg),
            "localized": {"en-US": {"name": pkg.get("title") or name, "description": pkg.get("description", "")}},
            "default_lang": "en-US",
            "languages": ["en-US"],
            "compatibility": ["handheld", "docked"],
            "screenshots": [],
            "distribution": {"source": pkg.get("url") or f"{CDN}/zips/{name}.zip",
                             "permission": f"{pkg.get('license') or 'unknown'}, via hb-appstore ({name}), OpenPak testing only"},
        },
        "version": {"kind": "update", "title_id": nid, "version": version, "version_label": label, "contents": contents}
                   if version else {"kind": "base", "version": 0, "version_label": label, "contents": contents},
        "stamp": stamp,
    }
    json.dump(meta, open(os.path.join(OUT, f"{nid}.json"), "w"), indent=1)
    shutil.rmtree(w)
    print(f"{nid}  {name}  {label}  {os.path.getsize(os.path.join(OUT, nid + '.nsp'))} B", flush=True)
    return nid


def upload(base, tid, dry=False):
    """Send out/<tid>.* (tid: a title id, or a patch id for an update) to the title's routes."""
    meta = json.load(open(os.path.join(OUT, f"{tid}.json")))
    app = meta["title"]["title_id"]
    if dry:
        nsp = os.path.join(OUT, f"{tid}.nsp")
        print(f"POST {base}/internal/store/titles  (201, or 409 when it exists)\n{json.dumps(meta['title'], indent=1)}")
        print(f"PUT  {base}/internal/store/titles/{app}/icon  image/jpeg {os.path.getsize(os.path.join(OUT, tid + '.jpg'))} B")
        print(f"POST {base}/internal/store/titles/{app}/versions  multipart: metadata, then nsp ({nsp}, {os.path.getsize(nsp)} B)")
        print(json.dumps(meta["version"], indent=1))
        return
    import requests  # streams the multipart body
    key = os.environ["NX_INTERNAL_KEY"]
    h = {"X-Internal-Key": key}
    s = requests.Session()
    r = s.post(f"{base}/internal/store/titles", json=meta["title"], headers=h)
    if r.status_code not in (201, 409):
        raise SystemExit(f"{tid}: create {r.status_code} {r.text}")
    r = s.put(f"{base}/internal/store/titles/{app}/icon", data=open(os.path.join(OUT, f"{tid}.jpg"), "rb"),
              headers={**h, "Content-Type": "image/jpeg"})
    if r.status_code != 200:
        raise SystemExit(f"{tid}: icon {r.status_code} {r.text}")
    with open(os.path.join(OUT, f"{tid}.nsp"), "rb") as f:
        r = s.post(f"{base}/internal/store/titles/{app}/versions", headers=h,
                   files=[("metadata", (None, json.dumps(meta["version"]), "application/json")),
                          ("nsp", (f"{tid}.nsp", f, "application/octet-stream"))])
    if r.status_code != 201:
        raise SystemExit(f"{tid}: version {r.status_code} {r.text}")
    print(f"{tid}: uploaded (unpublished)", flush=True)


def publish(base, tid):
    import requests
    r = requests.post(f"{base}/internal/store/titles/{tid}/publish", headers={"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]})
    if r.status_code != 200:
        raise SystemExit(f"{tid}: publish {r.status_code} {r.text}")


def add(names, base, repo):
    """Build, upload and publish the named packages now, then give them their store text and
    screenshots (enrich, limited to them)."""
    done_path = os.path.join(OUT, "uploaded.json")
    done = json.load(open(done_path)) if os.path.exists(done_path) else {}
    for name in names:
        tid = build(name, repo)
        upload(base, tid)
        publish(base, tid)
        done[tid] = json.load(open(os.path.join(OUT, f"{tid}.json")))["stamp"]
        json.dump(done, open(done_path, "w"), indent=1)
        os.remove(os.path.join(OUT, f"{tid}.nsp"))
    enrich(base, [p for p in repo if p["name"] in names])


def sync(base, repo):
    """Keep the store in step with 4TU (run daily): a package the store lacks is added; one
    4TU dated after the store's revised_at ships as an update one store version up (the high
    16 bits; the low 16 stay the packaging revision), so installed copies update through
    System Update. Each changed title is then enriched. One failure is logged, the run goes on."""
    import requests
    h = {"X-Internal-Key": os.environ["NX_INTERNAL_KEY"]}
    titles = {t["title_id"]: t for t in requests.get(f"{base}/internal/store/titles", headers=h).json()["titles"]}
    done_path = os.path.join(OUT, "uploaded.json")
    done = json.load(open(done_path)) if os.path.exists(done_path) else {}
    # Packages that failed to build, by the 4TU date they failed at: retried only once 4TU
    # publishes a newer one (a zip with no real NRO, a 404 zip), not every day.
    broken_path = os.path.join(OUT, "sync-broken.json")
    broken = json.load(open(broken_path)) if os.path.exists(broken_path) else {}
    srcs = json.load(open(SOURCES_PATH)) if os.path.exists(SOURCES_PATH) else {}
    seen_path = os.path.join(OUT, "upstream.json")  # {package: the upstream tag the store has}
    seen = json.load(open(seen_path)) if os.path.exists(seen_path) else {}
    changed, failed, upstream = [], [], []
    for p in repo:
        name, tid = p["name"], title_id(p["name"])
        if name in EXCLUDE or not p.get("binary", "").lower().endswith(".nro"):
            continue
        t = titles.get(tid)
        if t is not None and name in srcs:  # upstream first; 4TU only when it has nothing usable
            try:
                r = upstream_update(base, p, repo, t, srcs[name], seen, broken)
            except (SystemExit, Exception) as e:
                print(f"  upstream {srcs[name]}: {str(e)[:200]}, 4TU instead", flush=True)
                shutil.rmtree(os.path.join(WORK, tid[:-3] + "800"), ignore_errors=True)
                r = None
            json.dump(seen, open(seen_path, "w"), indent=1)
            if r == "same":
                continue
            if r is not None:
                changed.append(p)
                upstream.append((tid, r[1]))
                continue
        if broken.get(name) == p.get("updated"):
            continue
        if os.environ.get("HBSTORE_DRY"):
            if t is None or day(p.get("updated")) > (t.get("revised_at") or 0):
                print(f"would {'add' if t is None else 'update'}: {name} (4TU {p.get('updated')}, store {t and t.get('revised_at')})")
            continue
        try:
            if t is None:
                print(f"new: {name}", flush=True)
                build(name, repo)
                upload(base, tid)
                publish(base, tid)
                done[tid] = json.load(open(os.path.join(OUT, f"{tid}.json")))["stamp"]
            elif day(p.get("updated")) > (t.get("revised_at") or 0):
                version = ((t.get("version", 0) >> 16) + 1) << 16
                print(f"update: {name} -> {version} ({p.get('version')})", flush=True)
                build(name, repo, version)
                pid = tid[:-3] + "800"
                upload(base, pid)
                tid = pid
            else:
                continue
            os.remove(os.path.join(OUT, f"{tid}.nsp"))
            changed.append(p)
        except (SystemExit, Exception) as e:
            failed.append((name, str(e)[:200]))
            broken[name] = p.get("updated")
            print(f"  FAILED {name}: {str(e)[:200]}", flush=True)
            shutil.rmtree(os.path.join(WORK, title_id(name)), ignore_errors=True)
    json.dump(done, open(done_path, "w"), indent=1)
    json.dump(broken, open(broken_path, "w"), indent=1)
    if changed:
        enrich(base, changed)
    for tid, rel in upstream:  # after enrich: the release's own notes and date, not 4TU's
        requests.patch(f"{base}/internal/store/titles/{tid}", headers=h, json={
            "changelog": (rel.get("body") or "").strip(),
            "revised_at": int(datetime.datetime.fromisoformat(rel["published_at"].replace("Z", "+00:00")).timestamp())})
    github_sync(base)
    print(f"sync: {len(changed)} changed ({len(upstream)} from upstream), {len(failed)} failed", flush=True)
    for n, e in failed:
        print(f"  {n}: {e}", flush=True)


def since(date, base, repo):
    """Every wrappable package updated on or after date (dd/mm/yyyy): build, upload, publish.
    A package whose zip is unchanged since the last upload is skipped; one failure is logged
    and the run goes on."""
    cut = datetime.datetime.strptime(date, "%d/%m/%Y")
    done_path = os.path.join(OUT, "uploaded.json")
    done = json.load(open(done_path)) if os.path.exists(done_path) else {}
    todo = [p for p in repo if p["name"] not in EXCLUDE and p.get("binary", "").lower().endswith(".nro")
            and datetime.datetime.strptime(p["updated"], "%d/%m/%Y") >= cut]
    failed = []
    for i, p in enumerate(todo, 1):
        name = p["name"]
        if title_id(name) in done:
            continue  # already in the store; a changed package needs an update path, not a rebuild
        print(f"[{i}/{len(todo)}] {name}", flush=True)
        try:
            tid = build(name, repo)
            stamp = json.load(open(os.path.join(OUT, f"{tid}.json")))["stamp"]
            if done.get(tid) != stamp:
                upload(base, tid)
                publish(base, tid)
                done[tid] = stamp
                json.dump(done, open(done_path, "w"), indent=1)
            os.remove(os.path.join(OUT, f"{tid}.nsp"))  # the server keeps the package
        except (SystemExit, Exception) as e:
            failed.append((name, str(e)[:300]))
            print(f"  FAILED {name}: {str(e)[:300]}", flush=True)
            shutil.rmtree(os.path.join(WORK, title_id(name)), ignore_errors=True)
    print(f"done: {len(todo) - len(failed)} ok, {len(failed)} failed", flush=True)
    for n, e in failed:
        print(f"  {n}: {e}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("build", "upload", "since", "enrich", "openpak", "openpak-update", "openpak-nsp", "add", "loaderfix", "rebuild", "sync", "github",
                                                     "sources", "discover", "adopt", "github-sync"):
        raise SystemExit(__doc__)
    if sys.argv[1] == "github" and len(sys.argv) == 7:  # github <owner/repo> <tag> <category> <path in zip> <URL>
        github_add(sys.argv[2], sys.argv[3], sys.argv[-1].rstrip("/"), sys.argv[4], sys.argv[5], only=True)
    elif sys.argv[1] == "github-sync":  # just the github titles' part of sync
        github_sync(sys.argv[-1].rstrip("/"))
    elif sys.argv[1] == "sources":  # sources <URL>: the URL is only there because run-hb.sh appends it
        sources(load_repo())
    elif sys.argv[1] == "discover":  # discover [yyyy-mm-dd] <URL>
        discover(load_repo(), *sys.argv[2:-1])
    elif sys.argv[1] == "adopt":
        adopt(sys.argv[-1].rstrip("/"))
    elif sys.argv[1] == "github":  # github <owner/repo> <tag> [category] <URL>
        a = sys.argv[2:-1]
        github_add(a[0], a[1], sys.argv[-1].rstrip("/"), a[2] if len(a) > 2 else "tool")
    elif sys.argv[1] == "sync":
        sync(sys.argv[2].rstrip("/"), load_repo())
    elif sys.argv[1] == "rebuild":
        rebuild(sys.argv[-1].rstrip("/"), load_repo(), set(sys.argv[2:-1]))
    elif sys.argv[1] == "loaderfix":
        loaderfix(sys.argv[2], sys.argv[3].rstrip("/"), load_repo())
    elif sys.argv[1] == "openpak-update":
        openpak_update(sys.argv[2], sys.argv[3].rstrip("/") if len(sys.argv) > 3 else None)
    elif sys.argv[1] == "openpak-nsp":
        openpak_nsp(sys.argv[2])
    elif sys.argv[1] == "add":
        add(sys.argv[2:-1], sys.argv[-1].rstrip("/"), load_repo())
    elif sys.argv[1] == "openpak":
        openpak(sys.argv[2], sys.argv[3].rstrip("/"))
    elif sys.argv[1] == "enrich":
        repo = load_repo()
        only = set(sys.argv[2:-1])  # enrich [package…] <URL>: all titles, or just those
        enrich(sys.argv[-1].rstrip("/"), [p for p in repo if not only or p["name"] in only])
    elif sys.argv[1] == "since":
        since(sys.argv[2], sys.argv[3].rstrip("/"), load_repo())
    elif sys.argv[1] == "build":
        repo = load_repo()
        for n in sys.argv[2:]:
            build(n, repo)
    else:
        for t in sys.argv[3:]:
            upload(sys.argv[2].rstrip("/"), t)
