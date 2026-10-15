"""Offline font-tool checks; synthetic data is not Unity/Android render validation.

Run: .venv/Scripts/python.exe -m unittest discover -s tools -p test_push_font.py -v
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import font_adb
import font_unity
import push_font


PACKAGE = "com.example.game"
REMOTE = f"/sdcard/Android/data/{PACKAGE}/files/PUB/Fonts/original.bundle"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def temporary_directory():
    # Windows sandbox TEMP may not inherit write access; keep synthetic files
    # in a disposable directory within this checkout and clean it afterward.
    return tempfile.TemporaryDirectory(prefix=".font-tool-test-", dir=Path(__file__).resolve().parent.parent)


def synthetic_font(variable=False):
    """A tiny self-authored font: enough metadata/CJK cmap for offline checks."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import newTable

    names = [".notdef", "space", "A", "uni6C49", "uni5B57"]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({32: "space", 65: "A", 0x6C49: "uni6C49", 0x5B57: "uni5B57"})
    builder.setupGlyf({name: TTGlyphPen(None).glyph() for name in names})
    builder.setupHorizontalMetrics({name: (600, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Offline Chinese Test", "styleName": "Regular",
                            "uniqueFontIdentifier": "OfflineChineseTest-Regular",
                            "fullName": "Offline Chinese Test Regular", "psName": "OfflineChineseTest-Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    if variable:
        table = newTable("fvar")
        table.axes, table.instances = [], []
        builder.font["fvar"] = table
    output = io.BytesIO()
    builder.save(output)
    return output.getvalue()


class RemapTests(unittest.TestCase):
    def test_nested_local_references_are_remapped_without_mutating_input(self):
        original = {"nested": [{"m_FileID": 0, "m_PathID": 41}], "scalar": 41,
                    "null": {"m_FileID": 7, "m_PathID": 0}}
        output = push_font.remap(original, {41: 8})
        self.assertEqual(output["nested"][0], {"m_FileID": 0, "m_PathID": 8})
        self.assertEqual(output["scalar"], 41)
        self.assertEqual(output["null"], {"m_FileID": 0, "m_PathID": 0})
        self.assertEqual(original["nested"][0]["m_PathID"], 41)

    def test_external_or_dangling_references_are_rejected(self):
        for pointer in ({"m_FileID": 1, "m_PathID": 41}, {"m_FileID": 0, "m_PathID": 99}):
            with self.subTest(pointer=pointer), self.assertRaisesRegex(ValueError, "Unmapped donor PPtr"):
                push_font.remap({"deep": [pointer]}, {41: 8})


class FontMetadataTests(unittest.TestCase):
    def test_family_and_chinese_coverage(self):
        data = synthetic_font()
        report = push_font.font_info(data, "汉字 A\n汉")
        self.assertEqual(report["family"], "Offline Chinese Test")
        self.assertEqual(report["sha256"], digest(data))
        self.assertEqual(report["checked_characters"], 3)
        self.assertEqual(report["glyph_count"], 4)

    def test_missing_cjk_glyph_rejected(self):
        with self.assertRaisesRegex(ValueError, "U\\+851A"):
            push_font.font_info(synthetic_font(), "蔚")

    def test_variable_font_rejected_by_metadata_and_unity_input(self):
        data = synthetic_font(variable=True)
        with self.assertRaisesRegex(ValueError, "variable font"):
            push_font.font_info(data)
        with temporary_directory() as directory:
            font = Path(directory) / "variable.ttf"
            font.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "Variable fonts"):
                font_unity._check_font(font)

    def test_static_font_accepted_and_truncated_input_rejected(self):
        with temporary_directory() as directory:
            font = Path(directory) / "static.ttf"
            font.write_bytes(synthetic_font())
            font_unity._check_font(font)
            font.write_bytes(font.read_bytes()[:20])
            with self.assertRaisesRegex(ValueError, "Truncated SFNT"):
                font_unity._check_font(font)


class FakeAdb:
    """In-memory remote files; no executable or device is contacted."""
    def __init__(self, original=b"original bundle", uid="2000"):
        self.files = {REMOTE: original}
        self.uid = uid
        self.commands = []
        self.events = []
        self.corrupt_upload = False
        self.corrupt_backup = False

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        assert command[:3] == ["adb-for-test", "-s", "device-123"]
        assert kwargs["check"] is True and kwargs["timeout"] > 0
        arguments = command[3:]
        output = ""
        if arguments == ["get-state"]:
            output = "device"
        elif arguments[0] == "pull":
            self.events.append("backup")
            Path(arguments[2]).write_bytes(b"corrupt" if self.corrupt_backup else self.files[arguments[1]])
        elif arguments[0] == "push":
            self.events.append("push-temp")
            assert arguments[2] != REMOTE
            assert arguments[2].rsplit("/", 1)[0] == REMOTE.rsplit("/", 1)[0]
            self.files[arguments[2]] = b"corrupt" if self.corrupt_upload else Path(arguments[1]).read_bytes()
        elif arguments[0] == "shell":
            shell = arguments[1]
            if shell == "id -u":
                output = self.uid
            elif shell.startswith("am force-stop "):
                self.events.append("stop")
            elif "mv -f " in shell:
                self.events.append("rename")
                source, target = shlex.split(shell.rsplit("mv -f ", 1)[1])
                assert digest(self.files[target]) in shell
                self.files[target] = self.files.pop(source)
            elif "sha256sum " in shell:
                path = shlex.split(shell.split("sha256sum ", 1)[1].split(";", 1)[0])[0]
                self.events.append("hash-target" if path == REMOTE else "hash-temp")
                output = digest(self.files[path]) + "  " + path
            elif shell.startswith("if [ -e "):
                pass
            elif shell.startswith("rm -f "):
                self.events.append("cleanup-temp")
                self.files.pop(shlex.split(shell)[-1], None)
            else:
                raise AssertionError("Unexpected shell command: " + shell)
        else:
            raise AssertionError("Unexpected ADB command: " + repr(arguments))
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "original.bundle"
        self.source.write_bytes(b"replacement bundle")
        self.backups = self.root / "backups"

    def deploy(self, **overrides):
        arguments = dict(adb="adb-for-test", serial="device-123", package=PACKAGE,
                         remote_path=REMOTE, bundle_path=self.source,
                         original_sha256=digest(b"original bundle"), backup_dir=self.backups)
        arguments.update(overrides)
        return font_adb.deploy_bundle(**arguments)

    def test_dry_run_has_no_adb_or_filesystem_writes(self):
        with patch.object(font_adb.subprocess, "run", side_effect=AssertionError("ADB forbidden")):
            plan = self.deploy(serial=None)
        self.assertEqual(plan["status"], "dry-run")
        self.assertFalse(self.backups.exists())
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_invalid_remote_and_serial_inputs_never_reach_adb(self):
        invalid = [REMOTE + ";touch_pwned", REMOTE.replace("/PUB/", "/../"),
                   REMOTE.replace("/PUB/", "//"), REMOTE.replace(PACKAGE, "com.other.game"),
                   REMOTE + "\n", REMOTE.replace("original.bundle", "$(id)"),
                   "/data/data/" + PACKAGE + "/font.bundle"]
        with patch.object(font_adb.subprocess, "run", side_effect=AssertionError("ADB forbidden")):
            for remote in invalid:
                with self.subTest(remote=remote), self.assertRaises(ValueError):
                    self.deploy(remote_path=remote, apply=True)
            for serial in (None, "", "-s", "device;id", "device\n"):
                with self.subTest(serial=serial), self.assertRaises(ValueError):
                    self.deploy(serial=serial, apply=True)

    def test_hash_mismatch_does_not_stop_push_or_overwrite(self):
        adb = FakeAdb(b"unexpected version")
        with patch.object(font_adb.subprocess, "run", side_effect=adb):
            with self.assertRaisesRegex(font_adb.FontDeploymentError, "Remote SHA256 mismatch"):
                self.deploy(apply=True)
        self.assertEqual(adb.files, {REMOTE: b"unexpected version"})
        self.assertEqual(adb.events, ["hash-target"])
        self.assertFalse(self.backups.exists())

    def test_uid_zero_is_rejected_without_mutation(self):
        adb = FakeAdb(uid="0")
        with patch.object(font_adb.subprocess, "run", side_effect=adb):
            with self.assertRaisesRegex(font_adb.FontDeploymentError, "UID 2000"):
                self.deploy(apply=True)
        self.assertEqual(adb.events, [])
        self.assertFalse(self.backups.exists())

    def test_backup_temp_hash_rename_and_explicit_restore(self):
        adb = FakeAdb()
        with patch.object(font_adb.subprocess, "run", side_effect=adb):
            result = self.deploy(apply=True)
            self.assertEqual(result["status"], "deployed")
            backup = Path(result["backup_path"])
            self.assertEqual(backup.read_bytes(), b"original bundle")
            metadata = json.loads(Path(str(backup) + ".json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["backup_sha256"], digest(b"original bundle"))
            self.assertEqual(adb.files, {REMOTE: self.source.read_bytes()})
            self.assertEqual(adb.events, ["hash-target", "stop", "hash-target", "backup",
                                          "push-temp", "hash-temp", "rename", "hash-target"])
            restored = self.deploy(bundle_path=backup, original_sha256=digest(self.source.read_bytes()),
                                   restore=True, apply=True)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(adb.files, {REMOTE: b"original bundle"})
        self.assertEqual(Path(restored["backup_path"]).read_bytes(), b"replacement bundle")
        self.assertNotEqual(result["backup_path"], restored["backup_path"])

    def test_corrupt_upload_keeps_original_and_backup_and_removes_temp(self):
        adb = FakeAdb()
        adb.corrupt_upload = True
        with patch.object(font_adb.subprocess, "run", side_effect=adb):
            with self.assertRaisesRegex(font_adb.FontDeploymentError, "temporary bundle failed SHA256"):
                self.deploy(apply=True)
        self.assertEqual(adb.files, {REMOTE: b"original bundle"})
        self.assertNotIn("rename", adb.events)
        self.assertIn("cleanup-temp", adb.events)
        backups = list(self.backups.glob("*.backup"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original bundle")

    def test_corrupt_backup_blocks_upload(self):
        adb = FakeAdb()
        adb.corrupt_backup = True
        with patch.object(font_adb.subprocess, "run", side_effect=adb):
            with self.assertRaisesRegex(font_adb.FontDeploymentError, "pulled backup"):
                self.deploy(apply=True)
        self.assertNotIn("push-temp", adb.events)
        self.assertEqual(adb.files, {REMOTE: b"original bundle"})


class BackupValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.addCleanup(self.temporary.cleanup)
        self.backup = Path(self.temporary.name) / "original.backup"
        self.backup.write_bytes(b"original bundle")
        self.metadata = {"package": PACKAGE, "remote_path": REMOTE, "serial": "device-123",
                         "backup_sha256": digest(self.backup.read_bytes()),
                         "replacement_sha256": digest(b"replacement bundle")}
        Path(str(self.backup) + ".json").write_text(json.dumps(self.metadata), encoding="utf-8")

    def validate(self, **overrides):
        arguments = dict(path=self.backup, current_sha256=digest(b"replacement bundle"),
                         package=PACKAGE, remote=REMOTE, serial="device-123")
        arguments.update(overrides)
        return push_font.validate_backup(**arguments)

    def test_recorded_backup_is_accepted_and_corruption_is_rejected(self):
        self.validate()
        self.backup.write_bytes(b"corrupt backup")
        with self.assertRaisesRegex(ValueError, "Backup SHA256 mismatch"):
            self.validate()

    def test_wrong_device_package_path_or_installed_hash_is_rejected(self):
        for arguments in ({"serial": "another-device"}, {"package": "com.other.game"},
                          {"remote": REMOTE + ".other"}, {"current_sha256": "0" * 64}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.validate(**arguments)

    def test_restore_without_metadata_exits_before_adb(self):
        Path(str(self.backup) + ".json").unlink()
        with patch.object(font_adb.subprocess, "run", side_effect=AssertionError("ADB forbidden")):
            with self.assertRaises(SystemExit) as result:
                push_font.main(["restore", "--backup", str(self.backup), "--current-sha256", "0" * 64,
                                "--remote", REMOTE, "--package", PACKAGE,
                                "--serial", "device-123", "--apply"])
        self.assertEqual(result.exception.code, 1)


class UnityBatchTests(unittest.TestCase):
    def test_editor_batch_uses_json_paths_and_cleans_its_unique_assets(self):
        with temporary_directory() as directory:
            root = Path(directory).resolve()
            project = root / "scratch project"
            (project / "Assets").mkdir(parents=True)
            settings = project / "ProjectSettings"
            settings.mkdir()
            (settings / "ProjectVersion.txt").write_text("m_EditorVersion: 2021.3.34f1\n", encoding="utf-8")
            editor = root / "Unity.exe"
            editor.write_bytes(b"not an executable; subprocess is mocked")
            font = root / "汉字 font.ttf"
            font.write_bytes(synthetic_font())
            output = root / "output"

            def batch(command, **kwargs):
                self.assertIn("-batchmode", command)
                self.assertEqual(command[command.index("-projectPath") + 1], str(project))
                self.assertEqual(command[command.index("-buildTarget") + 1], "Android")
                config = json.loads(Path(kwargs["env"]["BA_FONT_BUILD_CONFIG"]).read_text(encoding="utf-8"))
                self.assertEqual(Path(config["outputDirectory"]), output)
                self.assertEqual((project / config["fontAssetPath"]).read_bytes(), font.read_bytes())
                (output / "donor.bundle").write_bytes(b"UnityFS\0" + bytes(40))
                (output / "donor-build.json").write_text(json.dumps({"unityVersion": "2021.3.34f1", "atlasCount": 1}))
                return subprocess.CompletedProcess(command, 0)

            with patch.object(font_unity.subprocess, "run", side_effect=batch):
                result = font_unity.prepare_font(editor, project, font, output, "2021.3.34f1")
            self.assertEqual(result, output / "donor.bundle")
            self.assertEqual(list((project / "Assets").iterdir()), [])

    def test_wrong_editor_version_never_launches_unity(self):
        with temporary_directory() as directory:
            root = Path(directory).resolve()
            (root / "ProjectSettings").mkdir()
            (root / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2020.3.0f1\n")
            editor = root / "Unity.exe"
            editor.write_bytes(b"mock")
            font = root / "font.ttf"
            font.write_bytes(synthetic_font())
            with patch.object(font_unity.subprocess, "run", side_effect=AssertionError("Unity forbidden")):
                with self.assertRaisesRegex(ValueError, "exactly match"):
                    font_unity.prepare_font(editor, root, font, root / "output", "2021.3.34f1")


if __name__ == "__main__":
    unittest.main()
