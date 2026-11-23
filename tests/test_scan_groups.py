import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub import core, server


class ScanGroupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "项目库"
        self.data = self.root.parent / "registry"
        self.paths = []
        for folder in ("团队一/tool", "团队二/tool"):
            path = self.root / folder
            path.mkdir(parents=True)
            (path / "index.html").write_text(folder, encoding="utf-8")
            self.paths.append(str(path))
        registry = patch.multiple(core, DATA_DIR=self.data, REGISTRY_FILE=self.data / "hub.json")
        registry.start()
        self.addCleanup(registry.stop)

    def test_rescan_via_directory_alias_preserves_existing_registration(self):
        alias = self.root.parent / "入口"
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(self.root), str(alias))
            self.addCleanup(os.rmdir, alias)
        else:
            alias.symlink_to(self.root, target_is_directory=True)
        first = core.add_project(str(alias / "团队一/tool"), group="设计", name="我的工具")
        found = core.scan(str(self.root))
        self.assertEqual([f["path"] for f in found if f["registered"]], self.paths[:1])
        added = server.api_post("/api/scan", {"paths": self.paths, "groupId": "default"})["added"]
        self.assertEqual(len(added), 1)
        retry = server.api_post("/api/scan", {"paths": self.paths, "group": "已有项目无需新分组"})
        self.assertEqual(retry["added"], [])
        self.assertTrue(all(f["registered"] for f in core.scan(str(alias))))
        self.assertEqual(core.find_project(self.paths[0]), first)
        self.assertEqual(len(core.load_registry()["projects"]), 2)
        self.assertEqual(len(core.load_registry()["groups"]), 2)

    def test_overlapping_cli_scans_count_new_projects_once(self):
        cli = [sys.executable, str(core.ROOT / "hub.py"), "--data", str(self.data), "scan",
               str(self.root), "--add", "--json"]
        processes = [subprocess.Popen(cli, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        added = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, stderr)
            added.extend(json.loads(stdout)["added"])
        self.assertEqual(sorted(added), ["tool", "tool-2"])
        self.assertEqual({p["path"] for p in core.load_registry()["projects"]}, set(self.paths))

    def test_delete_returns_members_and_stale_move_cannot_restore_group(self):
        members, _ = core.register_projects(self.paths)
        gid = core.set_group([], "归档")["group"]
        for p in members:
            server.api_post(f"/api/projects/{p['id']}/update", {"groupId": gid})
        self.assertTrue(all(p["group"] == gid for p in core.load_registry()["projects"]))
        server.api_post("/api/groups/" + gid, {"delete": True})
        self.assertEqual(core.load_registry()["projects"], members)
        self.assertEqual([Path(p, "index.html").read_text("utf-8") for p in self.paths],
                         ["团队一/tool", "团队二/tool"])
        with self.assertRaises(core.HubError):
            server.api_post(f"/api/projects/{members[0]['id']}/update", {"groupId": gid})
        with self.assertRaises(core.HubError):
            core.edit_group("default", delete=True)
        self.assertEqual([g["id"] for g in core.load_registry()["groups"]], ["default"])

    def test_missing_selection_rolls_back_batch_and_new_group(self):
        with self.assertRaisesRegex(core.HubError, "目录不存在"):
            server.api_post("/api/scan", {"paths": [self.paths[0], str(self.root / "missing")],
                                         "group": "待添加"})
        reg = core.load_registry()
        self.assertEqual(reg["projects"], [])
        self.assertEqual([g["id"] for g in reg["groups"]], ["default"])


if __name__ == "__main__":
    unittest.main()
