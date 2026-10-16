"""Run with: PYTHONPATH=src python -m unittest discover -s src/test -p 'test_memory_storage.py'."""
import unittest

from cortex.cluster.deployment.pod_factory import PodFactoryDefaults, create_pod_app_specs


class MemoryStorageTests(unittest.TestCase):
    def make_spec(self, **kwargs):
        return create_pod_app_specs(
            app_name="memory", platform="jetson",
            defaults=PodFactoryDefaults(namespace="orin"), **kwargs
        )[0]

    def test_memory_defaults_match_volume(self):
        spec = self.make_spec(role="memory")
        self.assertEqual(spec.pvc.mount_path, "/data/memory")
        self.assertEqual(spec.env["MEMORY_BASE_DIR"], spec.pvc.mount_path)

    def test_custom_mount_and_nested_database(self):
        env = {"MEMORY_BASE_DIR": "/persistent/db"}
        spec = self.make_spec(role="memory", pvc_mount_path="/persistent", env=env)
        self.assertEqual(spec.env, env)
        self.assertEqual(spec.pvc.mount_path, "/persistent")

    def test_environment_is_not_mutated(self):
        env = {}
        spec = self.make_spec(role="memory", pvc_mount_path="/persistent", env=env)
        self.assertEqual(env, {})
        self.assertEqual(spec.env["MEMORY_BASE_DIR"], "/persistent")

    def test_paths_outside_volume_are_rejected(self):
        for path in ("/tmp/db", "relative/db", "/data/memory/../db", "/data/memory-other"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.make_spec(role="memory", env={"MEMORY_BASE_DIR": path})

    def test_explicit_ephemeral_memory_is_unchanged(self):
        spec = self.make_spec(role="memory", pvc_enabled=False)
        self.assertIsNone(spec.pvc)
        self.assertNotIn("MEMORY_BASE_DIR", spec.env)

    def test_llm_storage_is_unchanged(self):
        spec = self.make_spec(role="llm")
        self.assertEqual(spec.pvc.mount_path, "/models/huggingface")
        self.assertNotIn("MEMORY_BASE_DIR", spec.env)


if __name__ == "__main__":
    unittest.main()
