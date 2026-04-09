import unittest
import os
import json
import tempfile
import sys
from pathlib import Path

# Add app to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.core.utils import safe_write_json


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, "test.json")

    def tearDown(self):
        if os.path.exists(self.test_file):
            os.remove(self.test_file)
        if os.path.exists(self.test_dir):
            os.rmdir(self.test_dir)

    def test_safe_write_json_success(self):
        data = {"foo": "bar", "count": 42}
        safe_write_json(self.test_file, data)

        self.assertTrue(os.path.exists(self.test_file))
        with open(self.test_file, "r") as f:
            read_data = json.load(f)
        self.assertEqual(data, read_data)

    def test_safe_write_json_creates_dirs(self):
        deep_file = os.path.join(self.test_dir, "nested", "dir", "test.json")
        data = {"hello": "world"}
        safe_write_json(deep_file, data)

        self.assertTrue(os.path.exists(deep_file))
        with open(deep_file, "r") as f:
            read_data = json.load(f)
        self.assertEqual(data, read_data)

        # Cleanup
        os.remove(deep_file)
        os.removedirs(os.path.dirname(deep_file))


if __name__ == "__main__":
    unittest.main()
