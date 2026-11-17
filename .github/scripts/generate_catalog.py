#!/usr/bin/env python3
"""
Genera apps.json + icons/*.png inspeccionando:
  1) Todos los .apk publicados en GitHub Releases (hasta 2 GB cada uno)
  2) Todos los .apk subidos en la carpeta apks/ del repositorio
Respeta personalizaciones opcionales en overrides.json.
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


def find_aapt_tool():
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/usr/local/lib/android/sdk"
    candidates = sorted(glob.glob(f"{android_home}/build-tools/*/aapt2"), reverse=True)
    if candidates:
        return ("aapt2", candidates[0])
    aapt2 = shutil.which("aapt2")
    if aapt2:
        return ("aapt2", aapt2)
    aapt = shutil.which("aapt")
    if aapt:
        return ("aapt", aapt)
    raise RuntimeError("No se encontró ni aapt2 ni aapt en el runner.")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_badging(tool_kind: str, tool_path: str, apk_path: Path) -> dict:
    cmd = [tool_path, "dump", "badging", str(apk_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out = proc.stdout or ""
    if proc.returncode != 0 and not out:
        raise RuntimeError(f"Error inspeccionando {apk_path.name}: {proc.stderr}")

    pkg_match = re.search(r"package:\s+name='([^']+)'\s+versionCode='(\d*)'\s+versionName='([^']*)'", out)
    if not pkg_match:
        raise RuntimeError(f"No se pudo leer package/versionCode en {apk_path.name}")

    pkg = pkg_match.group(1)
    vc = int(pkg_match.group(2) or 0)
    vn = pkg_match.group(3) or "1.0"

    label_match = (
        re.search(r"application-label-es[^:]*:'([^']+)'", out)
        or re.search(r"application-label:'([^']+)'", out)
        or re.search(r"application:\s+label='([^']+)'", out)
    )
    app_name = label_match.group(1).strip() if label_match else apk_path.stem

    min_sdk_match = re.search(r"(?:minSdkVersion|sdkVersion):'(\d+)'", out, re.IGNORECASE)
    min_sdk = int(min_sdk_match.group(1)) if min_sdk_match else 21

    native_match = re.search(r"native-code:\s*(.+)", out)
    if native_match:
        abis = re.findall(r"'([^']+)'", native_match.group(1))
    else:
        abis = ["universal"]

    # Prefer raster icon from high DPI to medium
    icon_paths = []
    for key in ["640", "480", "320", "240", "160", "120"]:
        m = re.search(rf"application-icon-{key}:'([^']+)'", out)
        if m:
            icon_paths.append(m.group(1))
    m_main = re.search(r"application:[^\n]*icon='([^']+)'", out)
    if m_main:
        icon_paths.append(m_main.group(1))

    return {
        "packageName": pkg,
        "versionCode": vc,
        "versionName": vn,
        "name": app_name,
        "minSdk": min_sdk,
        "abis": abis or ["universal"],
        "iconCandidates": icon_paths,
    }


def find_png_via_aapt2_resources(tool_path: str, apk_path: Path, icon_candidates: list) -> list:
    if not tool_path.endswith("aapt2") and not tool_path.endswith("aapt2.exe"):
        return []
    proc = subprocess.run([tool_path, "dump", "resources", str(apk_path)], capture_output=True, text=True, check=False)
    res_out = proc.stdout or ""
    if not res_out:
        return []

    blocks = []
    cur_res_id = ""
    cur_files = []
    for line in res_out.splitlines():
        m_res = re.match(r"^\s*resource\s+(0x[0-9a-fA-F]+)\s+(.*)$", line)
        if m_res:
            if cur_res_id:
                blocks.append((cur_res_id, cur_files))
            cur_res_id = m_res.group(1).lower()
            cur_files = []
        else:
            m_file = re.search(r"\(file\)\s+(\S+\.(?:png|webp|xml))", line, re.IGNORECASE)
            if m_file:
                cur_files.append((line.lower(), m_file.group(1)))
    if cur_res_id:
        blocks.append((cur_res_id, cur_files))

    cand_set = set(icon_candidates)
    matched_pngs = []
    for _, files in blocks:
        if any(f in cand_set for _, f in files):
            for meta_line, f in files:
                if f.lower().endswith((".png", ".webp")):
                    matched_pngs.append((meta_line, f))

    if not matched_pngs:
        for xml_cand in [c for c in icon_candidates if c.lower().endswith(".xml")]:
            xml_proc = subprocess.run(
                [tool_path, "dump", "xmltree", str(apk_path), "--file", xml_cand],
                capture_output=True,
                text=True,
                check=False,
            )
            fg_match = re.search(r"foreground[\s\S]{0,160}?@(0x[0-9a-fA-F]+)", xml_proc.stdout or "")
            if fg_match:
                fg_id = fg_match.group(1).lower()
                for rid, files in blocks:
                    if rid == fg_id:
                        for meta_line, f in files:
                            if f.lower().endswith((".png", ".webp")):
                                matched_pngs.append((meta_line, f))

    dpi_order = ["xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi"]
    def rank(entry):
        meta_line, _ = entry
        for idx, d in enumerate(dpi_order):
            if d in meta_line:
                return idx
        return len(dpi_order)

    return [f for _, f in sorted(matched_pngs, key=rank)]


def extract_best_icon(tool_path: str, apk_path: Path, icon_candidates: list, dest_path: Path) -> bool:
    try:
        resolved_candidates = list(icon_candidates) + find_png_via_aapt2_resources(tool_path, apk_path, icon_candidates)
        with zipfile.ZipFile(apk_path, "r") as zf:
            names = zf.namelist()
            for cand in resolved_candidates:
                if cand in names and cand.lower().endswith((".png", ".webp")):
                    data = zf.read(cand)
                    if data:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        dest_path.write_bytes(data)
                        return True

            # Fallback if adaptive XML icon was returned: pick highest-DPI launcher bitmap
            fallback_patterns = [
                r"res/mipmap-[^/]*xxxhdpi[^/]*/ic_launcher[^/]*\.(png|webp)$",
                r"res/mipmap-[^/]*xxhdpi[^/]*/ic_launcher[^/]*\.(png|webp)$",
                r"res/mipmap-[^/]*xhdpi[^/]*/ic_launcher[^/]*\.(png|webp)$",
                r"res/drawable-[^/]*xxhdpi[^/]*/.*(icon|logo|launcher)[^/]*\.(png|webp)$",
                r"res/mipmap-[^/]*/.*(launcher|icon)[^/]*\.(png|webp)$",
            ]
            for pat in fallback_patterns:
                matches = [n for n in names if re.search(pat, n, re.IGNORECASE) and "_round" not in n]
                if not matches:
                    matches = [n for n in names if re.search(pat, n, re.IGNORECASE)]
                if matches:
                    data = zf.read(sorted(matches)[0])
                    if data:
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        dest_path.write_bytes(data)
                        return True
    except Exception as exc:
        print(f"Aviso extrayendo ícono de {apk_path.name}: {exc}")
    return False


def github_get_json(url: str, token: str):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "HorneroCatalogBot/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url: str, dest: Path, token: str):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "HorneroCatalogBot/1.0")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as out:
        shutil.copyfileobj(resp, out)


def load_overrides(root: Path) -> dict:
    ov_path = root / "overrides.json"
    if not ov_path.exists():
        return {}
    try:
        return json.loads(ov_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Aviso leyendo overrides.json: {exc}")
        return {}


def main():
    root = Path.cwd()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref_name = os.environ.get("CATALOG_BRANCH") or "main"
    token = os.environ.get("GITHUB_TOKEN", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")

    tool_kind, tool_path = find_aapt_tool()
    print(f"Usando {tool_kind}: {tool_path}")

    overrides = load_overrides(root)
    ignored = set(overrides.get("ignorePackages", []))
    app_overrides = overrides.get("apps", {})

    icons_dir = root / "icons"
    icons_dir.mkdir(exist_ok=True)

    discovered = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # 1) Escanear APKs dentro de apks/*.apk en el repo
        for apk_file in sorted((root / "apks").glob("*.apk")):
            meta = parse_badging(tool_kind, tool_path, apk_file)
            rel_path = f"apks/{urllib.parse.quote(apk_file.name)}"
            discovered.append({
                "meta": meta,
                "localFile": apk_file,
                "url": f"https://raw.githubusercontent.com/{repo}/{ref_name}/{rel_path}",
                "sizeBytes": apk_file.stat().st_size,
                "sha256": sha256_file(apk_file),
                "changelog": "",
                "publishedAt": "",
                "releaseTag": "",
            })

        # 2) Escanear APKs en GitHub Releases
        if repo:
            rel_api = f"{api_url}/repos/{repo}/releases?per_page=100"
            try:
                releases = github_get_json(rel_api, token)
                for rel in releases:
                    if rel.get("draft"):
                        continue
                    tag = rel.get("tag_name") or ""
                    body = (rel.get("body") or "").strip()
                    pub = rel.get("published_at") or ""
                    for asset in rel.get("assets", []):
                        aname = asset.get("name") or ""
                        if not aname.lower().endswith(".apk"):
                            continue
                        dl_url = asset.get("browser_download_url")
                        if not dl_url:
                            continue
                        tmp_apk = tmp_dir / f"{tag}_{aname}".replace("/", "_")
                        print(f"Descargando release asset: {tag}/{aname}")
                        download_file(dl_url, tmp_apk, token)
                        meta = parse_badging(tool_kind, tool_path, tmp_apk)
                        discovered.append({
                            "meta": meta,
                            "localFile": tmp_apk,
                            "url": dl_url,
                            "sizeBytes": asset.get("size") or tmp_apk.stat().st_size,
                            "sha256": sha256_file(tmp_apk),
                            "changelog": body,
                            "publishedAt": pub,
                            "releaseTag": tag,
                        })
            except Exception as exc:
                print(f"Aviso leyendo releases: {exc}")

        # Agrupar por packageName -> elegir el mayor versionCode y sus variantes ABI
        by_pkg = {}
        for item in discovered:
            pkg = item["meta"]["packageName"]
            if pkg in ignored:
                continue
            by_pkg.setdefault(pkg, []).append(item)

        catalog_apps = []
        for pkg, entries in sorted(by_pkg.items()):
            max_vc = max(e["meta"]["versionCode"] for e in entries)
            latest_entries = [e for e in entries if e["meta"]["versionCode"] == max_vc]

            # Construir variantes por ABI (si hay varias APKs de la misma versión)
            variants = []
            seen_abi_urls = set()
            for e in latest_entries:
                abis = e["meta"]["abis"]
                abi_label = abis[0] if len(abis) == 1 else "universal"
                key = (abi_label, e["url"])
                if key not in seen_abi_urls:
                    seen_abi_urls.add(key)
                    variants.append({
                        "abi": abi_label,
                        "url": e["url"],
                        "sha256": e["sha256"],
                        "sizeBytes": e["sizeBytes"],
                        "minSdk": e["meta"]["minSdk"],
                    })

            primary = latest_entries[0]
            meta = primary["meta"]
            ov = app_overrides.get(pkg, {})

            icon_rel = f"icons/{pkg}.png"
            icon_out = root / icon_rel
            has_icon = extract_best_icon(tool_path, primary["localFile"], meta["iconCandidates"], icon_out) or icon_out.exists()

            all_abis = sorted({a for e in latest_entries for a in e["meta"]["abis"]})
            category = ov.get("category") or ("Tienda" if pkg == "com.hornero.tvstore" else "TV Apps")

            app_obj = {
                "id": pkg,
                "packageName": pkg,
                "name": ov.get("name") or meta["name"],
                "versionName": meta["versionName"],
                "versionCode": meta["versionCode"],
                "category": category,
                "description": ov.get("description") or f"Aplicación {meta['name']} lista para TV Box.",
                "changelog": ov.get("changelog") or primary["changelog"],
                "icon": ov.get("iconUrl") or (icon_rel if has_icon else ""),
                "apkUrl": variants[0]["url"],
                "sha256": variants[0]["sha256"],
                "sizeBytes": variants[0]["sizeBytes"],
                "minSdk": meta["minSdk"],
                "supportedAbis": all_abis,
                "publishedAt": primary["publishedAt"],
                "releaseTag": primary["releaseTag"],
                "pinned": bool(ov.get("pinned", pkg == "com.hornero.tvstore")),
                "variants": variants,
            }
            catalog_apps.append(app_obj)

    catalog = {
        "storeName": "Hornero Store",
        "apps": catalog_apps,
    }
    (root / "apps.json").write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Catálogo generado con {len(catalog_apps)} aplicación(es).")


if __name__ == "__main__":
    main()
