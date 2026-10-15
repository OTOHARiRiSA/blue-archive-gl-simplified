"""Build a donor TMP font with Unity's font engine, not a Python SDF imitation.

Use a disposable Unity project with the game's exact Editor and TMP versions,
TMP Essential Resources, and Android Build Support already installed. UnityPy
can transplant serialized assets, but cannot import a TTF or bake a TMP atlas.
This module never builds or edits an APK. Unity can update the supplied scratch
project's Library/ProjectSettings while switching to Android.
"""

import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid


_BUILDER = r'''
using System;
using System.IO;
using TMPro;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEngine;
using UnityEngine.TextCore.LowLevel;

namespace BUILD_NAMESPACE
{
    public static class FontBuilder
    {
        [Serializable]
        private sealed class Config
        {
            public string assetRoot;
            public string fontAssetPath;
            public string outputDirectory;
            public string unityVersion;
            public string shaderName;
        }

        [Serializable]
        private sealed class Result
        {
            public string unityVersion;
            public string tmpVersion;
            public string family;
            public string style;
            public string shader;
            public int atlasCount;
        }

        public static void Build()
        {
            try
            {
                var configPath = Environment.GetEnvironmentVariable("BA_FONT_BUILD_CONFIG");
                if (String.IsNullOrEmpty(configPath))
                    throw new InvalidOperationException("Missing BA_FONT_BUILD_CONFIG.");
                var config = JsonUtility.FromJson<Config>(File.ReadAllText(configPath));
                if (Application.unityVersion != config.unityVersion)
                    throw new InvalidOperationException("Editor version differs from original bundle.");
                if (!BuildPipeline.IsBuildTargetSupported(BuildTargetGroup.Android, BuildTarget.Android))
                    throw new InvalidOperationException("Install Android Build Support first.");

                var package = PackageInfo.FindForAssembly(typeof(TMP_FontAsset).Assembly);
                if (package == null || !package.version.StartsWith("3.", StringComparison.Ordinal))
                    throw new InvalidOperationException("This builder supports TMP 3.x only; match the game's package version.");

                AssetDatabase.ImportAsset(config.fontAssetPath, ImportAssetOptions.ForceSynchronousImport);
                var importer = AssetImporter.GetAtPath(config.fontAssetPath) as TrueTypeFontImporter;
                if (importer == null)
                    throw new InvalidOperationException("Unity could not import the supplied TTF/OTF.");
                importer.fontTextureCase = FontTextureCase.Dynamic;
                importer.includeFontData = true;
                importer.fontReferences = new Font[0];
                importer.SaveAndReimport();
                var font = AssetDatabase.LoadAssetAtPath<Font>(config.fontAssetPath);
                if (font == null)
                    throw new InvalidOperationException("Source Font asset is missing.");

                var asset = TMP_FontAsset.CreateFontAsset(font, 90, 9,
                    GlyphRenderMode.SDFAA, 1024, 1024, AtlasPopulationMode.Dynamic, true);
                if (asset == null || asset.material == null || asset.material.shader == null)
                    throw new InvalidOperationException("Import TMP Essential Resources before building.");
                if (!asset.material.shader.name.StartsWith("TextMeshPro/", StringComparison.Ordinal))
                    throw new InvalidOperationException("Unexpected TMP donor shader.");
                if (config.shaderName != "TextMeshPro/Distance Field" &&
                    config.shaderName != "TextMeshPro/Mobile/Distance Field")
                    throw new InvalidOperationException("Unsupported source TMP shader.");
                var shader = Shader.Find(config.shaderName);
                if (shader == null)
                    throw new InvalidOperationException("Import the matching TMP SDF shader: " + config.shaderName);
                asset.material.shader = shader;
                asset.name = "UnifiedChineseSDF";
                asset.fallbackFontAssetTable = new System.Collections.Generic.List<TMP_FontAsset>();
                for (int i = 0; i < asset.fontWeightTable.Length; ++i)
                    asset.fontWeightTable[i] = new TMP_FontWeightPair();

                // Allocate a real readable atlas and verify basic CJK support.
                // Other glyphs are generated from the embedded Font at runtime.
                string missing;
                if (!asset.TryAddCharacters(" Aa09\u6c49\u5b57\u851a\u84dd\u6863\u6848", out missing))
                    throw new InvalidOperationException("Font lacks required probe glyphs: " + missing);
                if (asset.atlasTextures.Length != 1 || asset.atlasTextures[0] == null)
                    throw new InvalidOperationException("Expected a single seed atlas.");

                string assetPath = config.assetRoot + "/UnifiedChineseSDF.asset";
                AssetDatabase.CreateAsset(asset, assetPath);
                asset.material.name = "UnifiedChineseSDF Material";
                AssetDatabase.AddObjectToAsset(asset.material, asset);
                foreach (var texture in asset.atlasTextures)
                {
                    texture.name = "UnifiedChineseSDF Atlas";
                    texture.hideFlags = HideFlags.None;
                    AssetDatabase.AddObjectToAsset(texture, asset);
                }
                // Some TMP versions clear seed data during a build by default.
                var serialized = new SerializedObject(asset);
                var clearOnBuild = serialized.FindProperty("m_ClearDynamicDataOnBuild");
                if (clearOnBuild != null)
                    clearOnBuild.boolValue = false;
                serialized.ApplyModifiedPropertiesWithoutUndo();
                EditorUtility.SetDirty(asset);
                EditorUtility.SetDirty(asset.material);
                AssetDatabase.SaveAssets();

                Directory.CreateDirectory(config.outputDirectory);
                var build = new AssetBundleBuild {
                    assetBundleName = "donor.bundle",
                    assetNames = new[] { assetPath, config.fontAssetPath }
                };
                var manifest = BuildPipeline.BuildAssetBundles(config.outputDirectory,
                    new[] { build }, BuildAssetBundleOptions.UncompressedAssetBundle,
                    BuildTarget.Android);
                if (manifest == null || !File.Exists(Path.Combine(config.outputDirectory, "donor.bundle")))
                    throw new InvalidOperationException("Android font AssetBundle build failed.");
                var result = new Result {
                    unityVersion = Application.unityVersion,
                    tmpVersion = package.version,
                    family = asset.faceInfo.familyName,
                    style = asset.faceInfo.styleName,
                    shader = asset.material.shader.name,
                    atlasCount = asset.atlasTextures.Length
                };
                File.WriteAllText(Path.Combine(config.outputDirectory, "donor-build.json"),
                    JsonUtility.ToJson(result, true));
            }
            catch (Exception error)
            {
                Debug.LogException(error);
                EditorApplication.Exit(1);
            }
        }
    }
}
'''


def _check_font(path):
    """Reject collections/variable fonts rather than silently choosing a face."""
    if path.suffix.lower() not in (".ttf", ".otf"):
        raise ValueError("Use a static .ttf/.otf face; TTC collections are unsupported.")
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] not in (b"\x00\x01\x00\x00", b"OTTO"):
            raise ValueError("Invalid or unsupported SFNT font header.")
        count = struct.unpack(">H", header[4:6])[0]
        directory = stream.read(count * 16)
        if not count or len(directory) != count * 16:
            raise ValueError("Truncated SFNT table directory.")
        size = path.stat().st_size
        for index in range(count):
            tag, _, offset, length = struct.unpack_from(">4sIII", directory, index * 16)
            if tag == b"fvar":
                raise ValueError("Variable fonts are unsupported; export a static instance first.")
            if offset + length > size:
                raise ValueError("Truncated SFNT table: " + repr(tag))


def prepare_font(unity_exe, project_path, font_path, output_dir, unity_version,
                 shader_name="TextMeshPro/Mobile/Distance Field"):
    """Return a donor bundle path; a failed Unity build is never treated as success.

    Paths are passed as JSON/environment data, never interpolated into C# or a
    shell command. Use an output directory outside Assets. The project must not
    already be open in Unity. Build logs and donor-build.json remain for review.
    """
    project = Path(project_path).resolve(strict=True)
    font = Path(font_path).resolve(strict=True)
    output = Path(output_dir).resolve()
    exe = shutil.which(str(unity_exe)) or str(Path(unity_exe).resolve())
    if not Path(exe).is_file():
        raise ValueError("Unity Editor executable does not exist: " + exe)
    if shader_name not in ("TextMeshPro/Distance Field", "TextMeshPro/Mobile/Distance Field"):
        raise ValueError("Only the original matching official TMP SDF shader is supported.")
    if not re.fullmatch(r"\d+\.\d+\.\d+[abfp]\d+(?:c\d+)?", unity_version):
        raise ValueError("Supply the full original bundle Unity version, for example 2021.3.34f1.")
    version_file = project / "ProjectSettings" / "ProjectVersion.txt"
    text = version_file.read_text(encoding="utf-8-sig")
    match = re.search(r"^m_EditorVersion:\s*(\S+)\s*$", text, re.MULTILINE)
    if match is None or match.group(1) != unity_version:
        raise ValueError("Scratch project Editor version must exactly match the original bundle.")
    assets = (project / "Assets").resolve(strict=True)
    if not assets.is_dir():
        raise ValueError("Unity project Assets directory is missing.")
    if output == assets or assets in output.parents:
        raise ValueError("Build output must be outside the Unity project's Assets directory.")
    if (project / "Temp" / "UnityLockfile").exists():
        raise ValueError("Close the scratch Unity project before running the batch build.")
    _check_font(font)
    output.mkdir(parents=True, exist_ok=True)
    donor = output / "donor.bundle"
    metadata = output / "donor-build.json"
    if donor.exists() or metadata.exists():
        raise ValueError("Use a fresh donor output directory to prevent stale build results.")

    identifier = uuid.uuid4().hex
    root = assets / ("__BAFontBuild_" + identifier)
    namespace = "BAFontBuild_" + identifier
    log_path = output / "font-unity-build.log"
    root.mkdir()
    try:
        (root / "Editor").mkdir()
        font_copy = root / ("UnifiedChinese" + font.suffix.lower())
        shutil.copyfile(font, font_copy)
        (root / "Editor" / "FontBuilder.cs").write_text(
            _BUILDER.replace("BUILD_NAMESPACE", namespace), encoding="utf-8")
        config = root / "build-config.json"
        config.write_text(json.dumps({
            "assetRoot": root.relative_to(project).as_posix(),
            "fontAssetPath": font_copy.relative_to(project).as_posix(),
            "outputDirectory": str(output),
            "unityVersion": unity_version,
            "shaderName": shader_name,
        }), encoding="utf-8")
        environment = os.environ.copy()
        environment["BA_FONT_BUILD_CONFIG"] = str(config)
        completed = subprocess.run([
            exe, "-batchmode", "-nographics", "-quit", "-buildTarget", "Android",
            "-projectPath", str(project), "-executeMethod", namespace + ".FontBuilder.Build",
            "-logFile", str(log_path),
        ], env=environment, check=False)
        if completed.returncode or not donor.is_file() or not metadata.is_file():
            raise RuntimeError("Unity donor build failed; inspect " + str(log_path))
        result = json.loads(metadata.read_text(encoding="utf-8-sig"))
        if result.get("unityVersion") != unity_version or result.get("atlasCount") != 1:
            raise RuntimeError("Unexpected donor build metadata; inspect " + str(metadata))
        if donor.stat().st_size < 32:
            raise RuntimeError("Unity produced an empty or truncated donor bundle.")
        return donor
    finally:
        # Delete only the unique directory created by this invocation, after
        # checking its resolved parent. Preserve logs in the caller's output.
        if root.resolve().parent != assets:
            raise RuntimeError("Refusing cleanup outside the Unity Assets directory.")
        shutil.rmtree(root)
        root.with_suffix(".meta").unlink(missing_ok=True)
