#!/usr/bin/env python3
"""Inspect, rebuild and deploy an EXISTING Android TMP font bundle.

UnityPy does not bake TMP fonts from TTF. `build` needs the original bundle and
either a compatible Unity Editor project or a Unity-built donor bundle. No
cache path, resource priority, game catalog format or font name is invented.
See docs/FONT_PATCH_PR.md for prerequisites and maintainer verification.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import ntpath
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PROBE = "活动任务购买学院网络服务器辉煌档案一石，。！？ABCabc0123456789"
TMP_FIELDS = {"m_FaceInfo", "m_GlyphTable", "m_CharacterTable", "m_FallbackFontAssetTable"}
REVIEW_NOTE = "此方案未经真机测试，需由维护者李伟连接手机后，执行推送脚本并在游戏中实测验证后再合并。"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def dependencies():
    try:
        import UnityPy
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise ValueError("Install font-only dependencies: pip install -r tools/requirements-font.txt") from exc
    return UnityPy, TTFont


def font_info(data, characters=""):
    _, TTFont = dependencies()
    with TTFont(io.BytesIO(data)) as font:
        require("fvar" not in font, "Use a static TTF/OTF instance, not a variable font")
        family = font["name"].getDebugName(16) or font["name"].getDebugName(1)
        families = sorted({entry.toUnicode() for entry in font["name"].names if entry.nameID in (1, 16)})
        cmap = font.getBestCmap() or {}
        required = {ord(c) for c in characters if not c.isspace()}
        missing = sorted(required - {cp for cp, glyph in cmap.items() if glyph != ".notdef"})
        require(not missing, "Font lacks required glyphs: " + ", ".join(f"U+{cp:04X}" for cp in missing[:30]))
        return {"family": family, "family_names": families, "sha256": sha256(data), "glyph_count": len(cmap),
                "checked_characters": len(required)}


def tree(obj):
    try:
        return obj.read_typetree()
    except Exception as exc:
        raise ValueError(f"Cannot read type tree for {obj.path_id}: {exc}. "
                         "Stripped TMP types are unsupported; supply the original type tree.") from exc


def is_tmp(data):
    return isinstance(data, dict) and TMP_FIELDS <= data.keys()


def object_key(obj):
    return (obj.assets_file.name, obj.path_id)


def local_ref(owner, pointer, expected=None):
    require(isinstance(pointer, dict) and pointer.get("m_FileID") == 0
            and pointer.get("m_PathID", 0) != 0, "Required reference is null or external")
    obj = owner.assets_file.objects.get(pointer["m_PathID"])
    require(obj is not None, "Dangling local PPtr")
    require(expected is None or obj.type.name == expected, f"Expected reference to {expected}")
    return obj


def material_field(data):
    for name in ("material", "m_Material"):
        if name in data:
            return name
    raise ValueError("TMP asset has no material field")


def graph(tmp):
    data = tree(tmp)
    require(is_tmp(data), "Selected object is not a supported TMP_FontAsset")
    font = local_ref(tmp, data.get("m_SourceFontFile"), "Font")
    material = local_ref(tmp, data[material_field(data)], "Material")
    textures = [local_ref(tmp, ref, "Texture2D") for ref in data.get("m_AtlasTextures", [])]
    require(len(textures) == 1, "Only one embedded atlas per TMP asset is supported")
    result = [tmp, font, material, *textures]
    require(len({object_key(obj) for obj in result}) == len(result), "Aliased font graph")
    return result


def texture_bytes(obj):
    data = tree(obj)
    if data.get("image data"):
        return bytes(data["image data"])
    stream = data.get("m_StreamData", {})
    name = ntpath.basename(stream.get("path", ""))
    files = getattr(obj.assets_file.parent, "files", {})
    resource = files.get(name)
    require(name and resource is not None and hasattr(resource, "bytes"),
            "Texture stream must be embedded in the same bundle; external resources are unsupported")
    raw = resource.bytes
    offset, size = stream.get("offset", -1), stream.get("size", 0)
    require(offset >= 0 and size > 0 and offset + size <= len(raw), "Truncated atlas stream")
    return raw[offset:offset + size]


def shader_name(obj):
    data = tree(obj)
    return data.get("m_ParsedForm", {}).get("m_Name") or data.get("m_Name")


def shader_identity(owner, pointer):
    require(isinstance(pointer, dict) and pointer.get("m_PathID"), "Material shader is null")
    if pointer["m_FileID"] == 0:
        return ("local", shader_name(local_ref(owner, pointer, "Shader")))
    index = pointer["m_FileID"] - 1
    require(0 <= index < len(owner.assets_file.externals), "Invalid shader external reference")
    external = owner.assets_file.externals[index]
    return ("external", external.path, external.guid, external.type, pointer["m_PathID"])


def extend_native_font_graph(old_graph, new_graph):
    # UnityEngine.Font can also own a native material/texture, separate from TMP.
    # Copy those only when both bundles have the same topology; never leave donor IDs.
    old_data, new_data = tree(old_graph[1]), tree(new_graph[1])
    for field, expected in (("m_DefaultMaterial", "Material"), ("m_Texture", "Texture2D")):
        old_pointer, new_pointer = old_data.get(field, {}), new_data.get(field, {})
        require(bool(old_pointer.get("m_PathID")) == bool(new_pointer.get("m_PathID")),
                f"Native Font dependency topology differs at {field}")
        if not new_pointer.get("m_PathID"):
            continue
        old = local_ref(old_graph[1], old_pointer, expected)
        new = local_ref(new_graph[1], new_pointer, expected)
        if old in old_graph or new in new_graph:
            require(old_graph.index(old) == new_graph.index(new), "Native dependency alias mismatch")
        else:
            old_graph.append(old)
            new_graph.append(new)


def select_tmp(env, path_id=None):
    found = []
    for obj in env.objects:
        if obj.type.name == "MonoBehaviour":
            data = tree(obj)
            if is_tmp(data) and (path_id is None or obj.path_id == path_id):
                found.append(obj)
    require(len(found) == 1, f"Expected one identifiable TMP_FontAsset; found {len(found)}. "
            "Use scan and --target-id/--donor-id; unreadable type trees cannot be skipped for a build.")
    return found[0]


def load_bundle(path):
    UnityPy, _ = dependencies()
    data = Path(path).read_bytes()
    require(data.startswith(b"UnityFS\0"), "Expected an unencrypted UnityFS bundle")
    env = UnityPy.load(data)
    require(env.file is not None and hasattr(env.file, "save"), "Not a single writable bundle")
    return env


def schema(obj):
    node = obj.serialized_type.node if obj.serialized_type else None
    require(node is not None, "Embedded type trees are required for rebuilding")
    return [(n.m_Level, n.m_Type, n.m_Name, n.m_ByteSize, n.m_MetaFlag, n.m_Version)
            for n in node.traverse()]


def remap(value, mapping):
    """Never copy donor object IDs or external file IDs into the original file."""
    if isinstance(value, dict):
        if {"m_FileID", "m_PathID"} <= value.keys():
            if value["m_PathID"] == 0:
                return {"m_FileID": 0, "m_PathID": 0}
            require(value["m_FileID"] == 0 and value["m_PathID"] in mapping,
                    f"Unmapped donor PPtr: {value}")
            return {"m_FileID": 0, "m_PathID": mapping[value["m_PathID"]]}
        return {key: remap(item, mapping) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [remap(item, mapping) for item in value]
    return value


def inventory(env):
    return {f"{obj.assets_file.name}:{obj.path_id}": sha256(obj.get_raw_data()) for obj in env.objects}


def tmp_report(tmp):
    data = tree(tmp)
    fallbacks = data["m_FallbackFontAssetTable"]
    return {"name": data.get("m_Name"), "family": data["m_FaceInfo"].get("m_FamilyName"),
            "path_id": tmp.path_id, "serialized_file": tmp.assets_file.name,
            "unity_version": tmp.assets_file.unity_version, "platform": int(tmp.platform),
            "atlas_mode": data.get("m_AtlasPopulationMode"), "fallbacks": fallbacks}


def validate_font(tmp, font_bytes, characters):
    objects = graph(tmp)
    data = tree(tmp)
    report = tmp_report(tmp)
    require(int(tmp.platform) == 13, "Bundle is not built for Android (BuildTarget.Android=13)")
    require(data.get("m_AtlasPopulationMode") == 1, "A Dynamic TMP asset is required")
    require(not data["m_FallbackFontAssetTable"], "Expected an empty fallback list; primary font handles Chinese")
    require(data.get("m_IsMultiAtlasTexturesEnabled") is True, "Dynamic multi-atlas support must be enabled")
    require(not data.get("m_FallbackSearchQueryLookup"), "Unexpected serialized fallback cache")
    embedded = bytes(tree(objects[1]).get("m_FontData", []))
    require(embedded == font_bytes, "Embedded source font differs from the supplied TTF/OTF")
    metadata = font_info(embedded, characters)
    require(report["family"] in metadata["family_names"], "TMP FaceInfo family differs from the source font")
    require(not any(ref.get("m_PathID") for ref in tree(objects[1]).get("m_FallbackFonts", [])),
            "Embedded Unity Font has fallback references")
    for key in ("m_FontWeightTable", "m_fontWeightTable"):
        for entry in data.get(key, []):
            for pointer in entry.values():
                if isinstance(pointer, dict) and "m_PathID" in pointer:
                    require(pointer["m_PathID"] == 0, "Alternative font weight points to another font")
    material = tree(objects[2])
    texture_ptrs = [v for v in walk(material) if isinstance(v, dict)
                    and v.get("m_PathID") == objects[3].path_id and v.get("m_FileID") == 0]
    require(texture_ptrs, "Material does not reference the font atlas")
    atlas = tree(objects[3])
    require(atlas.get("m_IsReadable") is True and atlas.get("m_TextureFormat") == 1,
            "Dynamic TMP requires a readable Alpha8 atlas")
    require(atlas.get("m_Width") == data.get("m_AtlasWidth")
            and atlas.get("m_Height") == data.get("m_AtlasHeight"), "Atlas dimensions differ from TMP settings")
    pixels = texture_bytes(objects[3])
    require(atlas["m_Width"] > 0 and atlas["m_Height"] > 0
            and atlas.get("m_MipCount", 1) == 1
            and len(pixels) == atlas["m_Width"] * atlas["m_Height"], "Invalid Alpha8 atlas payload size")
    glyphs = {item["m_Index"] for item in data["m_GlyphTable"]}
    require(glyphs and all(item["m_GlyphIndex"] in glyphs for item in data["m_CharacterTable"]),
            "Character table references missing glyphs")
    for glyph in data["m_GlyphTable"]:
        rect = glyph["m_GlyphRect"]
        require(glyph.get("m_AtlasIndex") == 0 and rect["m_X"] >= 0 and rect["m_Y"] >= 0
                and rect["m_Width"] >= 0 and rect["m_Height"] >= 0
                and rect["m_X"] + rect["m_Width"] <= atlas["m_Width"]
                and rect["m_Y"] + rect["m_Height"] <= atlas["m_Height"], "Glyph lies outside its atlas")
    report["source_font"] = metadata
    report["review_note"] = REVIEW_NOTE
    return report


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from walk(item)


def transplant(template, donor, target_id, donor_id, font_bytes, characters):
    original = load_bundle(template)
    replacement = load_bundle(donor)
    target = select_tmp(original, target_id)
    source = select_tmp(replacement, donor_id)
    validate_font(source, font_bytes, characters)
    old_graph, new_graph = graph(target), graph(source)
    extend_native_font_graph(old_graph, new_graph)
    require(target.assets_file.unity_version == source.assets_file.unity_version,
            "Original and donor Unity versions differ")
    require(tree(target).get("m_Version") == tree(source).get("m_Version"), "TMP serialization versions differ")
    require(int(target.platform) == 13, "Original bundle is not Android")
    before = inventory(original)
    mapping = {new.path_id: old.path_id for old, new in zip(old_graph, new_graph)}
    # The original script and shader references must remain valid in the game.
    old_script = tree(target).get("m_Script")
    original_shader_name = shader_identity(old_graph[2], tree(old_graph[2]).get("m_Shader"))
    donor_shader_name = shader_identity(new_graph[2], tree(new_graph[2]).get("m_Shader"))
    require(original_shader_name == donor_shader_name and original_shader_name in (
                ("local", "TextMeshPro/Distance Field"), ("local", "TextMeshPro/Mobile/Distance Field")),
            "Shader mismatch: only matching TMP SDF shaders are supported")
    for old, new in zip(old_graph, new_graph):
        require(schema(old) == schema(new), f"Serialized schemas differ for {old.type.name}")
        data = copy.deepcopy(tree(new))
        # Strip preserved references before remapping; they belong to the old file.
        if old is target:
            data["m_Script"] = {"m_FileID": 0, "m_PathID": 0}
        if old.type.name == "Material":
            require(shader_identity(old, tree(old).get("m_Shader"))
                    == shader_identity(new, data.get("m_Shader")), "Native material shader mismatch")
            data["m_Shader"] = {"m_FileID": 0, "m_PathID": 0}
        if old.type.name == "Texture2D":
            require("image data" in data, "Texture lacks an inline image data field")
            data["image data"] = texture_bytes(new)
            if "m_StreamData" in data:
                data["m_StreamData"].update(offset=0, size=0, path="")
        data = remap(data, mapping)
        data["m_Name"] = tree(old).get("m_Name", data.get("m_Name"))
        if old is target:
            data["m_Script"] = old_script
            for field in ("hashCode", "materialHashCode", "m_HashCode", "m_MaterialHashCode"):
                if field in tree(old):
                    data[field] = tree(old)[field]
        if old.type.name == "Material":
            data["m_Shader"] = tree(old).get("m_Shader")
        old.save_typetree(data)
    result = original.file.save(packer="none")
    # Re-open the actual serialized output, not the in-memory edited objects.
    UnityPy, _ = dependencies()
    reopened = UnityPy.load(result)
    report = validate_font(select_tmp(reopened, target.path_id), font_bytes, characters)
    after = inventory(reopened)
    require(before.keys() == after.keys(), "Object IDs or serialized file names changed")
    modified = {f"{o.assets_file.name}:{o.path_id}" for o in old_graph}
    untouched = {key: value for key, value in before.items() if key not in modified}
    require(all(after[key] == value for key, value in untouched.items()), "Unrelated object bytes changed")
    require(report["name"] == tree(target)["m_Name"], "Original TMP resource name changed")
    return result, report, untouched


def write_new(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def build(args):
    template = Path(args.template).resolve()
    font_path = Path(args.font).resolve()
    require(font_path.suffix.lower() in (".ttf", ".otf"), "Supply one static TTF or OTF, not TTC")
    font_bytes = font_path.read_bytes()
    characters = Path(args.characters).read_text(encoding="utf-8") if args.characters else PROBE
    font_info(font_bytes, characters)
    output = Path(args.out_dir).resolve() / template.name
    manifest_path = output.with_name(output.name + ".font.json")
    require(not output.exists() and not manifest_path.exists(), "Output exists; choose a new output directory")
    if args.donor:
        donor = Path(args.donor)
        result, report, untouched = transplant(template, donor, args.target_id, args.donor_id,
                                                font_bytes, characters)
    else:
        require(args.unity and args.unity_project, "Provide --donor, or both --unity and --unity-project. "
                "UnityPy alone cannot bake a TMP font.")
        from font_unity import prepare_font
        env = load_bundle(template)
        version = select_tmp(env, args.target_id).assets_file.unity_version
        original_tmp = select_tmp(env, args.target_id)
        original_material = graph(original_tmp)[2]
        shader = shader_name(local_ref(original_material, tree(original_material)["m_Shader"], "Shader"))
        output.parent.mkdir(parents=True, exist_ok=True)
        # Keep donor and Editor logs for review, including when Unity fails.
        temporary = tempfile.mkdtemp(prefix=".ba-font-donor-", dir=output.parent)
        donor = prepare_font(args.unity, args.unity_project, font_path, temporary, version, shader)
        result, report, untouched = transplant(template, donor, args.target_id, args.donor_id,
                                                font_bytes, characters)
    manifest = {"format": 1, "bundle_name": template.name,
                "template_sha256": sha256(template.read_bytes()), "bundle_sha256": sha256(result),
                "font_sha256": sha256(font_bytes), "required_characters": characters,
                "coverage_scope": "provided corpus" if args.characters else "smoke probe only",
                "target_id": report["path_id"], "report": report, "unchanged_objects": untouched,
                "review_note": REVIEW_NOTE}
    write_new(output, result)
    write_new(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"bundle": str(output), "manifest": str(manifest_path), **report,
            "coverage_scope": manifest["coverage_scope"]}


def verify(bundle_path, manifest_path):
    bundle_path = Path(bundle_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    require(manifest.get("format") == 1, "Unsupported manifest version")
    data = bundle_path.read_bytes()
    require(bundle_path.name == manifest["bundle_name"], "Bundle filename differs from the original")
    require(sha256(data) == manifest["bundle_sha256"], "Bundle SHA256 differs from the build manifest")
    env = load_bundle(bundle_path)
    target = select_tmp(env, manifest["target_id"])
    embedded = bytes(tree(graph(target)[1]).get("m_FontData", []))
    require(sha256(embedded) == manifest["font_sha256"], "Source font hash differs from manifest")
    report = validate_font(target, embedded, manifest["required_characters"])
    require(report == manifest["report"], "Reopened TMP metadata differs from the build report")
    current = inventory(env)
    require(all(current.get(key) == value for key, value in manifest["unchanged_objects"].items()),
            "Unrelated objects differ from the original bundle")
    return manifest, {"verified": True, "coverage_scope": manifest["coverage_scope"], **report}


def validate_backup(path, current_sha256, package, remote, serial=None):
    backup = Path(path)
    metadata = json.loads(Path(str(backup) + ".json").read_text(encoding="utf-8"))
    require(metadata.get("package") == package and metadata.get("remote_path") == remote,
            "Backup belongs to a different package or device path")
    require(not serial or metadata.get("serial") == serial, "Backup belongs to a different device")
    require(sha256(backup.read_bytes()) == metadata.get("backup_sha256"), "Backup SHA256 mismatch")
    require(current_sha256.lower() == metadata.get("replacement_sha256"),
            "Current hash does not match the replacement recorded with this backup")


def inspect_data(data, label, extract_dir=None):
    UnityPy, _ = dependencies()
    env = UnityPy.load(data)
    report = {"source": label, "sha256": sha256(data), "fonts": [], "tmp_assets": [], "warnings": []}
    for obj in env.objects:
        if obj.type.name not in ("Font", "MonoBehaviour", "AssetBundle"):
            continue
        try:
            obj_tree = tree(obj)
            if obj.type.name == "AssetBundle":
                report["bundle_name"] = obj_tree.get("m_Name")
                report["container_paths"] = [entry[0] for entry in obj_tree.get("m_Container", [])]
            elif obj.type.name == "Font":
                report["fonts"].append({"name": obj_tree.get("m_Name"), "path_id": obj.path_id,
                                        "embedded_data": bool(obj_tree.get("m_FontData"))})
            elif is_tmp(obj_tree):
                report["tmp_assets"].append(tmp_report(obj))
        except Exception as exc:
            report["warnings"].append(f"path_id={obj.path_id}: {exc}")
    if extract_dir and data.startswith(b"UnityFS\0") and (report["fonts"] or report["tmp_assets"]):
        name = ntpath.basename(label.split("!", 1)[-1])
        require(re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None and name not in (".", ".."),
                "Unsafe bundle filename in archive")
        path = Path(extract_dir) / report["sha256"] / name
        if path.exists():
            require(path.read_bytes() == data, "Extraction destination already contains different data")
        else:
            write_new(path, data)
        report["extracted_bundle"] = str(path.resolve())
    return report


def scan(args):
    source = Path(args.input)
    require(source.exists(), "Scan input does not exist")
    require(args.max_mb > 0, "--max-mb must be positive")
    results, errors, raw_fonts = [], [], []
    limit = args.max_mb * 1024 * 1024

    def accept(label, data):
        try:
            results.append(inspect_data(data, label, args.extract_dir))
        except Exception as exc:
            errors.append({"source": label, "error": str(exc)})

    def candidate(name, head):
        return head.startswith((b"UnityFS\0", b"UnityRaw\0", b"UnityWeb\0")) or name.endswith(
            (".assets", "globalgamemanagers", "level0"))

    files = sorted(source.rglob("*")) if source.is_dir() else [source]
    for path in files:
        if not path.is_file():
            continue
        if path.suffix.lower() in (".apk", ".zip"):
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    label = str(path) + "!" + info.filename
                    if info.filename.lower().endswith((".ttf", ".otf", ".ttc")):
                        raw_fonts.append(label)
                    with archive.open(info) as stream:
                        head = stream.read(16)
                    if candidate(info.filename, head):
                        if info.file_size > limit:
                            errors.append({"source": label, "error": "Above --max-mb limit"})
                        else:
                            accept(label, archive.read(info))
        else:
            with path.open("rb") as stream:
                head = stream.read(16)
            if candidate(path.name, head):
                if path.stat().st_size > limit:
                    errors.append({"source": str(path), "error": "Above --max-mb limit"})
                else:
                    accept(str(path), path.read_bytes())
    return {"resources": results, "raw_font_files": raw_fonts, "errors": errors,
            "note": "APK may not contain downloaded fonts. Scan pulled PUB resources too. "
                    "An asset name does not establish a device load path or cache priority."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("scan", help="List original resource names in APK / pulled bundles")
    command.add_argument("--input", required=True)
    command.add_argument("--extract-dir")
    command.add_argument("--max-mb", type=int, default=256)
    command = commands.add_parser("build", help="Build a donor with Unity, then preserve original bundle IDs")
    command.add_argument("--template", required=True)
    command.add_argument("--font", required=True)
    command.add_argument("--out-dir", required=True)
    command.add_argument("--characters", help="UTF-8 corpus to check; default is only a smoke probe")
    command.add_argument("--target-id", type=int)
    command.add_argument("--donor-id", type=int)
    command.add_argument("--donor", help="Already built, compatible Android TMP font bundle")
    command.add_argument("--unity", help="Matching Unity Editor executable")
    command.add_argument("--unity-project", help="Dedicated project with matching TMP and Android support")
    command = commands.add_parser("verify", help="Re-open the bundle with UnityPy and check its manifest")
    command.add_argument("--bundle", required=True)
    command.add_argument("--manifest", required=True)
    command = commands.add_parser("push", help="Plan deployment; --apply writes an existing resource")
    command.add_argument("--bundle", required=True)
    command.add_argument("--manifest", required=True)
    command.add_argument("--remote", required=True, help="Exact existing file path, verified by maintainer")
    command.add_argument("--package", default="com.nexon.bluearchive")
    command.add_argument("--serial", default="")
    command.add_argument("--adb", default="adb")
    command.add_argument("--backup-dir", default="work/font-backups")
    command.add_argument("--apply", action="store_true")
    command = commands.add_parser("restore", help="Restore a backup, checking the currently installed hash")
    command.add_argument("--backup", required=True)
    command.add_argument("--current-sha256", required=True)
    command.add_argument("--remote", required=True)
    command.add_argument("--package", default="com.nexon.bluearchive")
    command.add_argument("--serial", default="")
    command.add_argument("--adb", default="adb")
    command.add_argument("--backup-dir", default="work/font-backups")
    command.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            result = scan(args)
        elif args.command == "build":
            result = build(args)
        elif args.command == "verify":
            _, result = verify(args.bundle, args.manifest)
        else:
            from font_adb import deploy_bundle
            if args.command == "push":
                manifest, _ = verify(args.bundle, args.manifest)
                require(PurePosixPath(args.remote).name == manifest["bundle_name"],
                        "Remote filename differs from the original bundle name")
                bundle, expected = args.bundle, manifest["template_sha256"]
            else:
                bundle, expected = args.backup, args.current_sha256
                validate_backup(bundle, expected, args.package, args.remote, args.serial)
            result = deploy_bundle(args.adb, args.serial, args.package, args.remote, bundle,
                                   expected, args.backup_dir, apply=args.apply,
                                   restore=args.command == "restore")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
