"""Sync ALL local CAD binaries to the Azure `assets` container in one shot.

Mirrors every *.FCStd / *.SLDPRT / *.SLDASM / *.STEP / *.step / *.stl /
*.ply found under the task family trees to
https://openaievalsetub0ktk.blob.core.windows.net/assets/<repo-relative-path>:
new files are uploaded and changed files replaced (compared by content
hash, not timestamp). Blobs are deleted only when NO task.toml pins them;
a file that another checkout pinned but this machine lacks is left alone,
both in blob storage and in the toml manifests. It then refreshes the
sha256/bytes pins for the files present here and reports asset files that
have no toml entry yet. Safe to run from a partial checkout.

Requires: az CLI (logged in) and azcopy. Run from anywhere:

    python3 tools/sync.py            # sync + refresh toml pins
    python3 tools/sync.py FreeCAD/4_sling_lift         # sync only that subtree
    python3 tools/sync.py SolidWorks CadQuery/10_living_hinge  # several subtrees

A path-filtered run uploads/replaces/deletes blobs only inside the named
subtrees and refreshes only their task.toml pins; blobs outside the filter
are never touched.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "openaievalsetub0ktk"
CONTAINER = "test-example"
BASE_URL = f"https://{ACCOUNT}.blob.core.windows.net/{CONTAINER}"
FAMILIES = ("CadQuery", "FreeCAD", "SolidWorks", "STEP", "Rhino", "Blender")
EXTENSIONS = frozenset(  # matched case-insensitively via _is_asset()
    {".fcstd", ".sldprt", ".sldasm", ".step", ".stl", ".ply", ".igs", ".dxf", ".x_b", ".prt", ".slddrw",
     # neutral and AutoCAD exports shipped as task inputs (11_kindling_splitter)
     ".stp", ".dwg", ".dwf",
     # SolidWorks Simulation results (multi-GB); never in git
     ".cwr",
     # task prompt/reference documents imported alongside the models
     ".ppt", ".pptx", ".docx", ".png",
     # appearance assets referenced by SolidWorks models
     ".bmp", ".hdr", ".p2m", ".p2s", ".jpg", ".jpeg", ".blend",
     #: A RENDERED VIDEO CAN BE THE ANSWER. 101_ohlins_suspension asks
     #: for a turntable animation and its checklist grades the .mp4, so
     #: the reference and every adversarial ships one. filetree.MD says
     #: to skip .mp4, and it means the author's screen recordings --
     #: not a deliverable the rubric scores.
     ".mp4",
     #: Wavefront geometry, beside the .step in a vendor source drop.
     ".obj", ".mtl",
     #: SolidWorks Plastics study files -- settings, mesh and Fill/Pack/
     #: Warp results in "<part>/00/". 73_front_panel_fill_analysis grades
     #: the saved results, so the study travels with every model.
     ".adv3", ".atp3", ".bc3", ".bcm3", ".bco3", ".clp", ".cpu", ".ds3", ".exinfo", ".exinfo3", ".fr3", ".fr3s",
     ".fs3", ".ft3", ".gx3", ".kd3", ".klg3", ".mal", ".mch", ".op3", ".pg3", ".pr3", ".prc", ".pt3", ".pv3",
     ".pxb3", ".rs3", ".rst3", ".sync", ".wd3", ".wdl3", ".wlg3", ".wn3", ".wr3", ".wr30", ".wr34", ".wr37", ".wr3a",
     #: SOLIDWORKS Simulation result and mesh files beside a saved study
     #: (<doc>-<study>.CWR and its companions). 78_spherical_gas_holder ships
     #: them in one example; the small text logs (.LOG, .OUT) stay in git.
     ".bd2", ".bdf", ".csy", ".dsp", ".egr", ".ele", ".elf", ".geo", ".gsz", ".ilc", ".lcd", ".lod", ".mas", ".mat", ".mfc", ".nod", ".rlc", ".sl1", ".sl2", ".sl4", ".ste",
     #: Autodesk Inventor, and STEP written with the short suffix.
     #: 7_light_gantry_crane ships its purchased parts as the vendors
     #: supplied them -- .ipt parts and .iam assemblies -- and two of
     #: its neutral exports are .stp rather than .step. Without these
     #: three, sync read 28 files that were sitting on disk as "not on
     #: this machine" and left them out of blob storage.
     ".ipt", ".iam", ".stp"})


def _is_asset(p: Path) -> bool:
    #: `~$Name.SLDPRT` is the lock file SolidWorks writes beside a document
    #: while it is open, 8 bytes of nothing. It carries an asset extension,
    #: so without this it is uploaded, pinned and fetched like real
    #: geometry: 34 of them were, across two tasks, and ten survived a
    #: round trip through blob storage before anyone noticed. Guarded here
    #: rather than in the callers so `local_assets` and `folder_manifest`
    #: agree; a manifest row and an upload must never disagree about what
    #: counts as an asset.
    if p.name.startswith("~$"):
        return False
    return p.suffix.lower() in EXTENSIONS


def local_assets(prefixes: list[str] | None = None) -> list[Path]:
    out = []
    for family in FAMILIES:
        base = ROOT / family
        if not base.is_dir():
            continue
        out.extend(p for p in base.rglob("*")
                   if p.is_file() and _is_asset(p)
                   and "__pycache__" not in p.parts)
    if prefixes:
        out = [p for p in out
               if any(p.relative_to(ROOT).as_posix() == pre
                      or p.relative_to(ROOT).as_posix().startswith(pre + "/")
                      for pre in prefixes)]
    return sorted(out)


def tool(name: str) -> str:
    """The full path to an external tool, resolved the way a SHELL would.

    On Windows `az` is `az.cmd`, a batch file. `subprocess` without
    `shell=True` calls CreateProcess directly, and CreateProcess does not
    apply PATHEXT -- so a bare "az" raises FileNotFoundError [WinError 2]
    on a machine where az is installed and works fine from the prompt. The
    error names no file, so it reads as "something is missing" rather than
    "this argument is not an executable image". `shutil.which` does apply
    PATHEXT, so it finds az.cmd and hands over a path CreateProcess can
    run. `azcopy` is a real .exe and never needed this, which is exactly
    why the difference stayed invisible.
    """
    #: PATH IS NOT ENOUGH, AND WAITING FOR IT TO BE IS HOW THIS BREAKS.
    #: winget installs both of these into per-user directories and adds
    #: them to the USER Path -- which a shell that is already running
    #: never picks up. azcopy sat installed, and working at the prompt,
    #: while sync.py refused to start. So: an explicit override first,
    #: then PATH, then the places winget and the MSI actually put them.
    override = os.environ.get(name.upper() + "_CMD")
    if override:
        if not Path(override).is_file():
            raise SystemExit(f"{name.upper()}_CMD is set to {override}, "
                             f"which is not a file.")
        return override
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA") or ""
    progfiles = os.environ.get("ProgramFiles") or ""
    for cand in (
        Path(local) / "Microsoft" / "WinGet" / "Links" / (name + ".exe"),
        Path(local) / "Microsoft" / "WinGet" / "Links" / (name + ".cmd"),
        Path(progfiles) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / (name + ".cmd"),
    ):
        if cand.is_file():
            return str(cand)
    raise SystemExit(
        f"{name} was not found.\n"
        f"  looked at: {name.upper()}_CMD, PATH, "
        f"%LOCALAPPDATA%/Microsoft/WinGet/Links\n"
        f"  az     -> https://aka.ms/installazurecliwindows, then `az login`\n"
        f"  azcopy -> winget install azcopy, or "
        f"https://aka.ms/downloadazcopy\n"
        f"Either install it, or set {name.upper()}_CMD in the repo .env to "
        f"the binary's full path.\n"
        f"Both are needed: the SAS is generated before anything is "
        f"uploaded.")


def container_sas() -> str:
    az = tool("az")
    key = subprocess.check_output(
        [az, "storage", "account", "keys", "list", "--account-name", ACCOUNT,
         "--query", "[0].value", "-o", "tsv"], text=True).strip()
    if not key:
        raise SystemExit(
            f"az returned no account key for {ACCOUNT}. Check `az login` and "
            f"that this account can read its keys (`az account show`).")
    expiry = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%MZ")
    return subprocess.check_output(
        [az, "storage", "container", "generate-sas", "--name", CONTAINER,
         "--account-name", ACCOUNT, "--account-key", key,
         "--permissions", "racwdl", "--expiry", expiry, "-o", "tsv"],
        text=True).strip()


def sync(files: list[Path], prefixes: list[str] | None = None) -> None:
    # Staged under ROOT, not the OS tempdir: os.link() (via hardlink_to)
    # requires source and destination on the same filesystem, and on WSL
    # the default tempdir (/tmp, WSL's native fs) and a repo checked out
    # under /mnt/c/... (the Windows drive, mounted via 9p/DrvFs) are two
    # different filesystems -- cross-device hardlinks fail there.
    #
    # Even same-filesystem hardlinks fail on DrvFs itself (confirmed live
    # 2026-08-24: linking two /mnt/c/... paths raises PermissionError, not
    # the EXDEV the comment above was guarding against) -- DrvFs just
    # doesn't support hard links, so fall back to a real copy.
    # Stage only files some task.toml pins. An unpinned local file (a retired
    # example folder never deleted from disk, say) used to be uploaded and
    # then deleted again as unpinned on every run.
    import unicodedata
    keep = pinned_names()
    def _pinned(f):   # pinned_names() lower-cases and stores both normalisations
        rel = f.relative_to(ROOT).as_posix().lower()
        return any(unicodedata.normalize(n, rel) in keep for n in ("NFC", "NFD"))
    skipped = [f for f in files if not _pinned(f)]
    files = [f for f in files if f not in set(skipped)]
    if skipped:
        print(f"{len(skipped)} local file(s) not pinned by any task.toml -- "
              "not uploaded (pin them or delete them):")
        for f in skipped[:20]:
            print("  " + f.relative_to(ROOT).as_posix())
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")
    with tempfile.TemporaryDirectory(prefix=".assets_stage_", dir=ROOT) as stage:
        stage = Path(stage)
        for f in files:
            rel = f.relative_to(ROOT)
            dest = stage / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.hardlink_to(f)
            except OSError:
                shutil.copy2(f, dest)
        sas = container_sas()
        if prefixes:
            # One scoped azcopy per subtree: deletions can only happen
            # inside the named prefixes, never across the whole container.
            targets = [(stage / pre, f"{BASE_URL}/{pre}?{sas}")
                       for pre in prefixes if (stage / pre).is_dir()]
        else:
            targets = [(stage, f"{BASE_URL}?{sas}")]
        for src, dst in targets:
            # Compare by content, not last-modified time: a blob re-uploaded
            # from another machine (SolidWorks rewrites parts on open) carries
            # a newer LMT and would otherwise shadow the pinned local file
            # forever. --put-md5 stores the hash so later runs can compare.
            # Never --delete-destination: a checkout that lacks a file another
            # machine uploaded must not wipe that file's blob. Stale blobs are
            # removed afterwards by delete_unpinned(), which only touches blobs
            # that no task.toml pins.
            cmd = [tool("azcopy"), "sync", str(src), dst,
                   "--compare-hash=MD5", "--put-md5"]
            subprocess.run(cmd, check=True)
        delete_unpinned(sas, prefixes)


def pinned_names() -> set[str]:
    """Every blob name some task.toml pins (single-file or manifest row),
    in both Unicode normalisations and case-folded.

    Case-folded because SolidWorks tasks mix .SLDASM/.sldasm (and similar)
    case inconsistently between what a manifest row spells and what the
    file is actually named on disk -- a case-sensitive comparison here
    reads a real, current file as unpinned and deletes its blob. Confirmed
    live: 33_rotary_vane's manifest pins "input.SLDASM" while the file on
    disk is "input.sldasm", and delete_unpinned() was deleting that file's
    blob on every sync.
    """
    import unicodedata
    names = set()
    for toml_path in ROOT.glob("*/*/task.toml"):
        task = toml_path.parent.relative_to(ROOT).as_posix()
        meta = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("metadata", {})
        for a in meta.get("assets", []):
            if "manifest" in a:
                names.update(f"{task}/{a['path']}/{r}" for r, _, _ in a["manifest"])
            else:
                names.add(f"{task}/{a['path']}")
    return {unicodedata.normalize(f, n).casefold()
            for n in names for f in ("NFC", "NFD")}


def list_blobs(sas: str, prefix: str = "") -> list[str]:
    """Blob names under `prefix`. Names come back XML-escaped (`&` as
    `&amp;`, etc.), so they are unescaped before use."""
    import html
    import re
    import urllib.parse
    import urllib.request
    out, marker = [], ""
    while True:
        url = (f"{BASE_URL}?restype=container&comp=list&maxresults=5000"
               f"&prefix={urllib.parse.quote(prefix)}{marker}&{sas}")
        xml = urllib.request.urlopen(url).read().decode()
        out += [html.unescape(n) for n in re.findall(r"<Name>(.*?)</Name>", xml)]
        nm = re.search(r"<NextMarker>(.+?)</NextMarker>", xml)
        if not nm:
            return out
        marker = "&marker=" + urllib.parse.quote(nm.group(1))


def delete_unpinned(sas: str, prefixes: list[str] | None) -> None:
    """Remove blobs under the synced prefixes that no task.toml pins.
    Pinned-but-missing-locally blobs are left alone: another checkout owns
    them."""
    import unicodedata
    import urllib.parse
    import urllib.request
    keep = pinned_names()
    dead = [n for pre in (prefixes or [""]) for n in list_blobs(sas, pre)
            if unicodedata.normalize("NFC", n).casefold() not in keep]
    for n in dead:
        print(f"deleting unpinned blob: {n}")
        urllib.request.urlopen(urllib.request.Request(
            f"{BASE_URL}/{urllib.parse.quote(n)}?{sas}", method="DELETE",
            #: A blob restored from soft-delete carries a snapshot, and Azure
            #: refuses to delete a blob with snapshots unless told to take
            #: them too (409). Soft-delete keeps both for its retention period.
            headers={"x-ms-delete-snapshots": "include"}))


def folder_manifest(local_dir: Path):
    """(rows, aggregate_sha, total_bytes) for every asset-extension file
    under `local_dir`. rows are (relpath, sha256, bytes), sorted; the
    aggregate is the sha256 of the canonical manifest text, so it changes
    iff any file's name, content, or size changes."""
    rows = []
    # sort by the posix relpath string: Path ordering is case-insensitive on
    # Windows, which made manifests (and their aggregate sha) churn per machine
    for f in sorted(local_dir.rglob("*"), key=lambda f: f.relative_to(local_dir).as_posix()):
        if f.is_file() and _is_asset(f):
            rows.append((f.relative_to(local_dir).as_posix(),
                         hashlib.sha256(f.read_bytes()).hexdigest(),
                         f.stat().st_size))
    canon = "".join(f"{r} {s} {b}\n" for r, s, b in rows)
    return rows, hashlib.sha256(canon.encode()).hexdigest(), sum(
        b for _, _, b in rows)


def folder_pin_text(path: str, url: str, rows, agg: str, total: int) -> str:
    lines = [f'path = "{path}"', f'url = "{url}"',
             f'sha256 = "{agg}"', f"bytes = {total}", f"files = {len(rows)}",
             "manifest = ["]
    lines += [f'  ["{r}", "{s}", {b}],' for r, s, b in rows]
    lines.append("]")
    return "\n".join(lines)


_FOLDER_BLOCK = (r'path = "{p}"\nurl = "(?P<url>[^"]*)"\n'
                 r'(?:sha256 = "[0-9a-f]{{64}}"\n)?(?:bytes = \d+\n)?files = \d+\n'
                 r"manifest = \[\n(?:  \[.*\],\n)*\]")


def refresh_toml_pins(files: list[Path],
                      prefixes: list[str] | None = None) -> None:
    by_path = {f.relative_to(ROOT).as_posix(): f for f in files}
    # Case-insensitive fallback lookup: a manifest row or single-file pin's
    # declared case doesn't always match the file's real on-disk case (a
    # SolidWorks task's own .SLDASM/.sldasm inconsistency, most often), and
    # a case-sensitive lookup here misreports a present file as "not on
    # this machine" and can duplicate its manifest row under both cases.
    by_path_ci = {k.casefold(): v for k, v in by_path.items()}
    covered = set()
    tomls = sorted(ROOT.glob("*/*/task.toml"))
    if prefixes:
        tomls = [t for t in tomls
                 if any(t.parent.relative_to(ROOT).as_posix() == pre
                        or t.parent.relative_to(ROOT).as_posix()
                        .startswith(pre + "/")
                        or pre.startswith(
                            t.parent.relative_to(ROOT).as_posix() + "/")
                        for pre in prefixes)]
    for toml_path in tomls:
        task_dir = toml_path.parent
        #: THE FILE'S OWN NEWLINES ARE PRESERVED. `read_text` folds CRLF
        #: to LF and `write_text` expands LF back to the PLATFORM default,
        #: so refreshing one pin from Windows rewrote every line of the
        #: file and a one-row change arrived as a 469-line diff.
        raw = toml_path.read_bytes()
        nl = "\r\n" if b"\r\n" in raw else "\n"
        text = raw.decode("utf-8").replace("\r\n", "\n")
        meta = tomllib.loads(text).get("metadata", {})
        changed = False
        for asset in meta.get("assets", []):
            local = task_dir / asset["path"]
            repo_rel = local.relative_to(ROOT).as_posix()

            if "manifest" in asset:      # ---- folder pin ----
                if not local.is_dir():
                    print(f"WARNING {toml_path.relative_to(ROOT)}: folder "
                          f"asset {asset['path']} missing locally")
                    continue
                rows, agg, total = folder_manifest(local)
                stored = [tuple(r) for r in asset["manifest"]]
                # A checkout that lacks a file another machine pinned must not
                # drop its row: keep stored rows whose file is absent here.
                # Matched case-insensitively -- a stored row's case doesn't
                # always match the file's real case on disk.
                have = {r.casefold() for r, _, _ in rows}
                kept = [r for r in stored if r[0].casefold() not in have]
                if kept:
                    for r in kept:
                        print(f"note {toml_path.relative_to(ROOT)}: "
                              f"{asset['path']}/{r[0]} not on this machine; pin kept")
                    rows = sorted(rows + kept)
                    canon = "".join(f"{r} {s} {b}\n" for r, s, b in rows)
                    agg = hashlib.sha256(canon.encode()).hexdigest()
                    total = sum(b for _, _, b in rows)
                covered.update(f"{repo_rel}/{r}" for r, _, _ in rows)
                if (agg != asset.get("sha256") or total != asset.get("bytes")
                        or [tuple(r) for r in
                            ((r, s, b) for r, s, b in rows)] != stored):
                    print(f"folder pin update: {repo_rel} ({len(rows)} files)")
                    block = re.compile(
                        _FOLDER_BLOCK.format(p=re.escape(asset["path"])))
                    m = block.search(text)
                    assert m, (toml_path, asset["path"])
                    text = (text[:m.start()]
                            + folder_pin_text(asset["path"],
                                              m.group("url"),
                                              rows, agg, total)
                            + text[m.end():])
                    changed = True
                continue

            covered.add(repo_rel)        # ---- single-file pin ----
            f = by_path.get(repo_rel) or by_path_ci.get(repo_rel.casefold())
            if f is None:
                print(f"note {toml_path.relative_to(ROOT)}: asset "
                      f"{asset['path']} not on this machine; pin kept")
                continue
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            size = f.stat().st_size
            if sha != asset["sha256"] or size != asset.get("bytes"):
                print(f"pin update: {repo_rel}")
                block = re.compile(
                    r'(path = "' + re.escape(asset["path"]) + r'"\n'
                    r'url = "[^"]*"\n)'
                    r'sha256 = "[0-9a-f]{64}"\n'
                    r"bytes = \d+")
                text, n = block.subn(
                    rf'\g<1>sha256 = "{sha}"\nbytes = {size}', text)
                assert n == 1, (toml_path, asset["path"])
                changed = True
        if changed:
            toml_path.write_bytes(nl.join(text.split("\n")).encode("utf-8"))
            tomllib.loads(text)
    orphans = sorted(set(by_path) - covered)
    if orphans:
        print("\nPresent locally but not pinned by any task.toml -- not "
              "uploaded (add [[metadata.assets]] entries or delete them):")
        for o in orphans:
            print("  " + o)


def main() -> None:
    # Reject unknown flags rather than skipping them. This loop used to
    # `continue` past anything starting with "--", so a stale --dry-run
    # from muscle memory or an old note would be swallowed and the real
    # run -- uploads and blob deletions included -- would proceed anyway.
    unknown = [a for a in sys.argv[1:] if a.startswith("-")]
    if unknown:
        raise SystemExit("unknown option(s): " + " ".join(unknown)
                         + "\nusage: sync.py [SUBTREE ...]")
    prefixes = []
    for arg in sys.argv[1:]:
        pre = arg.strip("/").replace("\\", "/")
        if not (ROOT / pre).exists():
            sys.exit(f"error: {pre} does not exist under {ROOT}")
        if pre.split("/")[0] not in FAMILIES:
            sys.exit(f"error: {pre} is not under a task family "
                     f"{FAMILIES}")
        prefixes.append(pre)
    files = local_assets(prefixes or None)
    # Resolved BEFORE anything is staged. Staging hardlinks or copies every
    # asset -- 401 MB for one task here -- and discovering that az is not on
    # PATH after paying for that is a slow way to learn it.
    for name in ("az", "azcopy"):
        tool(name)
    total_mb = sum(f.stat().st_size for f in files) / 1e6
    scope = f" (filtered to {', '.join(prefixes)})" if prefixes else ""
    print(f"{len(files)} local asset file(s), {total_mb:.0f} MB{scope}")
    sync(files, prefixes or None)
    refresh_toml_pins(files, prefixes or None)


if __name__ == "__main__":
    main()
