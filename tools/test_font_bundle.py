"""Exercise real UnityPy binary I/O using self-authored synthetic UnityFS files.

Native objects use UnityPy's 2021.3 release type trees. The TMP type tree is a
small synthetic subset, and the Shader/atlas/font outlines are test data. These
tests verify serialization, object identity and references, NOT Unity rendering
or compatibility with the game's actual bundles.

Run: python -m unittest discover -s tools -p test_font_bundle.py -v
"""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile
import unittest
import zipfile

import UnityPy
from UnityPy.enums import ArchiveFlags, BuildTarget, ClassIDType
from UnityPy.files.BundleFile import BundleFile
from UnityPy.files.File import File
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.files.SerializedFile import SerializedFile, SerializedFileHeader, SerializedType
from UnityPy.helpers.Tpk import get_typetree_node
from UnityPy.helpers.TypeTreeHelper import write_typetree
from UnityPy.helpers.TypeTreeNode import TypeTreeNode
from UnityPy.helpers.UnityVersion import UnityVersion
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

import push_font
from test_push_font import synthetic_font


VERSION = "2021.3.34f1"
SHADER = "TextMeshPro/Mobile/Distance Field"
PIXELS = bytes(range(64))
CORPUS = "汉字 A"


def pointer(identifier=0):
    return {"m_FileID": 0, "m_PathID": identifier}


def defaults(node):
    """Build native default object data from the installed release type tree."""
    if node.m_Type == "string":
        return ""
    if node.m_Type == "TypelessData":
        return b""
    if node.m_Type == "bool":
        return False
    if node.m_Type in ("float", "double"):
        return 0.0
    if node.m_Children and node.m_Children[0].m_Type == "Array":
        return []
    if node.m_Children:
        return {child.m_Name: defaults(child) for child in node.m_Children}
    return 0


def inferred_node(name, value, level=1):
    """Only the custom TMP subset needs inferred fields; native trees stay real."""
    if isinstance(value, bool):
        kind, size = "bool", 1
    elif isinstance(value, int):
        kind, size = ("SInt64", 8) if name == "m_PathID" else ("int", 4)
    elif isinstance(value, float):
        kind, size = "float", 4
    elif isinstance(value, str):
        kind, size = "string", -1
    elif isinstance(value, list):
        kind, size = "vector", -1
    else:
        kind = "PPtr<Object>" if set(value) == {"m_FileID", "m_PathID"} else name.lstrip("m_")
        size = -1
    node = TypeTreeNode(level, kind, name, size, 1, [],
                        m_TypeFlags=0, m_Index=0, m_MetaFlag=0, m_RefTypeHash=0)
    if isinstance(value, dict):
        node.m_Children = [inferred_node(key, item, level + 1) for key, item in value.items()]
    elif isinstance(value, list):
        array = TypeTreeNode(level + 1, "Array", "Array", -1, 1, [],
                             m_TypeFlags=1, m_Index=0, m_MetaFlag=0, m_RefTypeHash=0)
        sample = value[0] if value else pointer()
        array.m_Children = [inferred_node("size", 0, level + 2),
                            inferred_node("data", sample, level + 2)]
        node.m_Children = [array]
    return node


def clone_node(node):
    # UnityPyBoost nodes cannot be copied with copy.deepcopy/pickle.
    return TypeTreeNode(node.m_Level, node.m_Type, node.m_Name, node.m_ByteSize,
                        node.m_Version, [clone_node(child) for child in node.m_Children],
                        m_TypeFlags=node.m_TypeFlags or 0, m_Index=node.m_Index or 0,
                        m_MetaFlag=node.m_MetaFlag or 0, m_RefTypeHash=node.m_RefTypeHash or 0)


def fixture(font_bytes, base, streamed=False, native=False):
    """Build a parseable UnityFS/SerializedFile, without a Unity installation."""
    unity_version = UnityVersion.from_str(VERSION)
    bundle = BundleFile.__new__(BundleFile)
    File.__init__(bundle, name="fixture.bundle")
    bundle.signature, bundle.version = "UnityFS", 7
    bundle.version_player, bundle.version_engine = "5.x.x", VERSION
    bundle.dataflags = ArchiveFlags(64)
    bundle._uses_block_alignment = True
    bundle._block_info_flags = 64

    assets = SerializedFile.__new__(SerializedFile)
    File.__init__(assets, parent=bundle, name="CAB-synthetic-font")
    assets.flags = 4
    assets.reader = EndianBinaryReader(b"", endian="<")
    assets.unity_version, assets.version = VERSION, unity_version
    assets.target_platform, assets._m_target_platform = BuildTarget.Android, 13
    assets._enable_type_tree = True
    assets.types, assets.script_types, assets.externals, assets.ref_types = [], [], [], []
    assets.objects, assets._cache = {}, {}
    assets.unknown, assets.big_id_enabled, assets.userInformation = 0, 0, "synthetic test fixture"
    header = SerializedFileHeader.__new__(SerializedFileHeader)
    header.metadata_size, header.file_size, header.version, header.data_offset = 0, 0, 22, 0
    header.endian, header.reserved = "<", b"\0" * 3
    assets.header = header
    bundle.files[assets.name] = assets

    def add(class_id, identifier, changes, custom=None):
        node = clone_node(get_typetree_node(class_id, unity_version))
        if custom:
            node.m_Children.extend(inferred_node(name, value) for name, value in custom.items())
        data = defaults(node)
        data.update(changes)
        if custom:
            data.update(custom)
        serialized = SerializedType.__new__(SerializedType)
        serialized.__attrs_init__(class_id)
        serialized.is_stripped_type = False
        serialized.script_type_index = 0 if class_id == 114 else -1
        serialized.script_id = b"\x11" * 16 if class_id == 114 else None
        serialized.old_type_hash = b"\x22" * 16
        serialized.type_dependencies = ()
        serialized.node = node
        type_id = len(assets.types)
        assets.types.append(serialized)
        writer = EndianBinaryWriter(endian="<")
        write_typetree(data, node, writer, assets)
        raw = writer.bytes
        obj = ObjectReader(assets_file=assets, reader=EndianBinaryReader(raw, endian="<"),
                           path_id=identifier, type_id=type_id, serialized_type=serialized,
                           class_id=class_id, type=ClassIDType(class_id), byte_start=0,
                           byte_size=len(raw), is_destroyed=None, is_stripped=None, data=raw)
        assets.objects[identifier] = obj
        return data

    ids = {name: base + offset for offset, name in enumerate(
        ("tmp", "font", "material", "atlas", "shader", "script", "untouched", "container",
         "native_material", "native_atlas"), 1)}
    custom = {
        "m_Version": "1.1.0", "hashCode": base + 1100, "materialHashCode": base + 2200,
        "material": pointer(ids["material"]), "m_SourceFontFile": pointer(ids["font"]),
        "m_FaceInfo": {"m_FamilyName": "Offline Chinese Test", "m_StyleName": "Regular",
                       "m_PointSize": 90, "m_Scale": 1.0},
        "m_AtlasPopulationMode": 1, "m_IsMultiAtlasTexturesEnabled": True,
        "m_AtlasTextures": [pointer(ids["atlas"])], "m_AtlasWidth": 8, "m_AtlasHeight": 8,
        "m_FallbackFontAssetTable": [],
        "m_FontWeightTable": [{"regularTypeface": pointer(), "italicTypeface": pointer()}],
        "m_GlyphTable": [{"m_Index": 1, "m_AtlasIndex": 0, "m_Scale": 1.0,
                          "m_GlyphRect": {"m_X": 1, "m_Y": 1, "m_Width": 2, "m_Height": 2},
                          "m_Metrics": {"m_Width": 2.0, "m_Height": 2.0,
                                        "m_HorizontalBearingX": 0.0, "m_HorizontalBearingY": 2.0,
                                        "m_HorizontalAdvance": 3.0}}],
        "m_CharacterTable": [{"m_Unicode": 65, "m_GlyphIndex": 1,
                              "m_Scale": 1.0, "m_TextAsset": pointer(ids["tmp"])}],
    }
    add(114, ids["tmp"], {"m_Name": f"OriginalFont-{base}", "m_Script": pointer(ids["script"])}, custom)
    add(128, ids["font"], {"m_Name": f"SourceFont-{base}", "m_FontData": font_bytes,
                           "m_FontNames": ["Offline Chinese Test"],
                           "m_DefaultMaterial": pointer(ids["native_material"] if native else 0),
                           "m_Texture": pointer(ids["native_atlas"] if native else 0)})
    material_node = get_typetree_node(21, unity_version)
    saved = defaults(material_node)["m_SavedProperties"]
    saved["m_TexEnvs"] = [("_MainTex", {"m_Texture": pointer(ids["atlas"]),
                                       "m_Scale": {"x": 1.0, "y": 1.0},
                                       "m_Offset": {"x": 0.0, "y": 0.0}})]
    add(21, ids["material"], {"m_Name": f"Material-{base}", "m_Shader": pointer(ids["shader"]),
                              "m_SavedProperties": saved})
    resource_name = "CAB-synthetic-font.resS"
    stream = {"offset": 3, "size": len(PIXELS),
              "path": f"archive:/{assets.name}/{resource_name}"} if streamed else {
                  "offset": 0, "size": 0, "path": ""}
    add(28, ids["atlas"], {"m_Name": f"Atlas-{base}", "m_Width": 8, "m_Height": 8,
                           "m_CompleteImageSize": len(PIXELS), "m_TextureFormat": 1,
                           "m_IsReadable": True, "m_ImageCount": 1, "m_TextureDimension": 2,
                           "m_MipCount": 1, "image data": b"" if streamed else PIXELS,
                           "m_StreamData": stream})
    shader = defaults(get_typetree_node(48, unity_version))["m_ParsedForm"]
    shader["m_Name"] = SHADER
    add(48, ids["shader"], {"m_Name": "TMP_SDF-Mobile", "m_ParsedForm": shader})
    add(115, ids["script"], {"m_Name": "TMP_FontAsset", "m_ClassName": "TMP_FontAsset",
                             "m_Namespace": "TMPro", "m_AssemblyName": "Unity.TextMeshPro.dll"})
    add(49, ids["untouched"], {"m_Name": "DoNotTouch", "m_Script": "unrelated data stays identical"})
    asset_info = {"preloadIndex": 0, "preloadSize": 1, "asset": pointer(ids["tmp"])}
    add(142, ids["container"], {"m_Name": "original.bundle", "m_AssetBundleName": "original.bundle",
                               "m_PreloadTable": [pointer(ids["tmp"])],
                               "m_Container": [("assets/fonts/original.asset", asset_info)]})
    if native:
        native_saved = defaults(material_node)["m_SavedProperties"]
        native_saved["m_TexEnvs"] = [("_MainTex", {"m_Texture": pointer(ids["native_atlas"]),
                                                   "m_Scale": {"x": 1.0, "y": 1.0},
                                                   "m_Offset": {"x": 0.0, "y": 0.0}})]
        add(21, ids["native_material"], {"m_Name": f"NativeMaterial-{base}",
                                         "m_Shader": pointer(ids["shader"]),
                                         "m_SavedProperties": native_saved})
        add(28, ids["native_atlas"], {"m_Name": f"NativeAtlas-{base}", "m_Width": 8, "m_Height": 8,
                                      "m_CompleteImageSize": len(PIXELS), "m_TextureFormat": 1,
                                      "m_IsReadable": True, "m_ImageCount": 1, "m_TextureDimension": 2,
                                      "m_MipCount": 1, "image data": PIXELS})
    if streamed:
        resource = EndianBinaryReader(b"pad" + PIXELS + b"tail")
        resource.flags = 0
        bundle.files[resource_name] = resource
    return bundle.save(packer="none"), ids


class BinaryBundleTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1]
        self.workspace = workspace
        work = workspace / "work"
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="font-binary-test-", dir=work)
        self.root = Path(self.temporary.name)
        self.assertIn(work.resolve(), self.root.resolve().parents)
        self.addCleanup(self.temporary.cleanup)
        self.font = synthetic_font()

    def test_real_binary_build_transplant_and_manifest_verify(self):
        original, ids = fixture(self.font, 100)
        donor, donor_ids = fixture(self.font, 900)
        template, replacement = self.root / "original.bundle", self.root / "donor.bundle"
        template.write_bytes(original)
        replacement.write_bytes(donor)
        font_path, corpus_path = self.root / "font.ttf", self.root / "corpus.txt"
        font_path.write_bytes(self.font)
        corpus_path.write_text(CORPUS, encoding="utf-8")
        before = UnityPy.load(original)
        original_tree = push_font.tree(push_font.select_tmp(before))
        report = push_font.build(argparse.Namespace(
            template=str(template), font=str(font_path), out_dir=str(self.root / "output"),
            characters=str(corpus_path), target_id=ids["tmp"], donor_id=donor_ids["tmp"],
            donor=str(replacement)))
        manifest, verified = push_font.verify(report["bundle"], report["manifest"])
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["family"], "Offline Chinese Test")
        self.assertEqual(verified["fallbacks"], [])
        output = UnityPy.load(Path(report["bundle"]).read_bytes())
        target = push_font.select_tmp(output)
        data = push_font.tree(target)
        self.assertEqual(data["m_Name"], original_tree["m_Name"])
        for key in ("hashCode", "materialHashCode", "m_Script"):
            self.assertEqual(data[key], original_tree[key])
        self.assertEqual(data["m_SourceFontFile"], pointer(ids["font"]))
        self.assertEqual(data["m_CharacterTable"][0]["m_TextAsset"], pointer(ids["tmp"]))
        self.assertTrue(manifest["unchanged_objects"])
        self.assertEqual(push_font.inventory(before)[f"CAB-synthetic-font:{ids['untouched']}"],
                         push_font.inventory(output)[f"CAB-synthetic-font:{ids['untouched']}"])
        self.assertEqual(set(before.file.container), set(output.file.container))

    def test_streamed_donor_pixels_are_inlined_without_changing_original_stream(self):
        original, ids = fixture(self.font, 100, streamed=True)
        donor, donor_ids = fixture(self.font, 900, streamed=True)
        template, replacement = self.root / "original.bundle", self.root / "donor.bundle"
        template.write_bytes(original)
        replacement.write_bytes(donor)
        raw, report, _ = push_font.transplant(template, replacement, ids["tmp"], donor_ids["tmp"],
                                             self.font, CORPUS)
        reopened = UnityPy.load(raw)
        atlas = push_font.graph(push_font.select_tmp(reopened))[3]
        self.assertEqual(push_font.texture_bytes(atlas), PIXELS)
        self.assertEqual(bytes(push_font.tree(atlas)["image data"]), PIXELS)
        self.assertEqual(push_font.tree(atlas)["m_StreamData"]["path"], "")
        self.assertEqual(reopened.file.files["CAB-synthetic-font.resS"].bytes, b"pad" + PIXELS + b"tail")
        self.assertEqual(report["platform"], 13)

    def test_apk_scan_uses_original_bundle_resource_and_font_names(self):
        original, ids = fixture(self.font, 100)
        apk = self.root / "fixture.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("assets/aa/Android/original.bundle", original)
            archive.writestr("assets/fonts/input.ttf", self.font)
        report = push_font.scan(argparse.Namespace(input=str(apk), max_mb=8,
                                                   extract_dir=str(self.root / "extracted")))
        self.assertEqual(report["errors"], [])
        resource = report["resources"][0]
        self.assertEqual(resource["warnings"], [])
        self.assertEqual(resource["tmp_assets"][0]["path_id"], ids["tmp"])
        self.assertEqual(resource["bundle_name"], "original.bundle")
        self.assertEqual(resource["container_paths"], ["assets/fonts/original.asset"])
        self.assertEqual(Path(resource["extracted_bundle"]).read_bytes(), original)
        self.assertTrue(report["raw_font_files"][0].endswith("input.ttf"))

    def test_native_font_material_texture_graph_is_remapped(self):
        original, ids = fixture(self.font, 100, native=True)
        donor, donor_ids = fixture(self.font, 900, native=True)
        template, replacement = self.root / "original.bundle", self.root / "donor.bundle"
        template.write_bytes(original)
        replacement.write_bytes(donor)
        raw, _, _ = push_font.transplant(template, replacement, ids["tmp"], donor_ids["tmp"],
                                         self.font, CORPUS)
        reopened = UnityPy.load(raw)
        font = push_font.graph(push_font.select_tmp(reopened))[1]
        data = push_font.tree(font)
        self.assertEqual(data["m_DefaultMaterial"], pointer(ids["native_material"]))
        self.assertEqual(data["m_Texture"], pointer(ids["native_atlas"]))
        material = push_font.local_ref(font, data["m_DefaultMaterial"], "Material")
        atlas_pointer = push_font.tree(material)["m_SavedProperties"]["m_TexEnvs"][0][1]["m_Texture"]
        self.assertEqual(atlas_pointer, pointer(ids["native_atlas"]))
        self.assertEqual(push_font.tree(material)["m_Shader"], pointer(ids["shader"]))


if __name__ == "__main__":
    unittest.main()
