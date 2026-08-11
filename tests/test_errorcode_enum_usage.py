import ast
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "ext", "aiovelib"))

import helper  # noqa: E402  (must be imported before globals -- circular import)
from globals import ErrorCode  # noqa: E402

# Reproduces production crash:
#   AttributeError: type object 'ErrorCode' has no attribute 'NO_WINDOW'
# raised from dynamicess.py's control loop because the code referenced an
# ErrorCode member that was never defined on the enum. Any `ErrorCode.X`
# reference where X isn't a real member blows up the control loop the exact
# same way, so this scans every module for that pattern instead of pinning
# to the one historical name.
_SOURCE_FILES = [
    "dynamicess.py",
    "ess_device.py",
    "vebus_device.py",
    "multirs_device.py",
    "evcs_delegate.py",
]


class ErrorCodeEnumUsageTest(unittest.TestCase):
    def test_all_referenced_errorcode_members_exist(self):
        valid_members = set(ErrorCode.__members__)
        bad_references = []

        for filename in _SOURCE_FILES:
            path = os.path.join(_REPO_ROOT, filename)
            with open(path, "r") as f:
                tree = ast.parse(f.read(), filename=filename)

            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "ErrorCode"
                    and node.attr not in valid_members
                    and node.attr != "_value2member_map_"
                ):
                    bad_references.append(f"{filename}:{node.lineno}: ErrorCode.{node.attr}")

        self.assertEqual(
            bad_references,
            [],
            "Found references to ErrorCode members that don't exist on the enum "
            "(these raise AttributeError and crash the control loop at runtime): "
            + ", ".join(bad_references),
        )


if __name__ == "__main__":
    unittest.main()
