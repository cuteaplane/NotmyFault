import unittest

from notmyfault.bindings import iter_legacy_event_payload_paths, resolve_value


class LegacyBindingTests(unittest.TestCase):
    def test_legacy_event_payload_paths_include_nested_and_full_payload(self):
        rule = {
            "actions": [{
                "params": {
                    "message": "文件：{{ event.payload.file.name }}",
                    "payload": "{{ event.payload }}",
                },
            }],
        }

        self.assertEqual(
            list(iter_legacy_event_payload_paths(rule)),
            [("file", "name"), ()],
        )

    def test_legacy_event_payload_resolves_with_manual_context(self):
        value = resolve_value(
            "文件：{{ event.payload.file.name }}",
            {"event": {"payload": {"file": {"name": "report.txt"}}}},
        )

        self.assertEqual(value, "文件：report.txt")


if __name__ == "__main__":
    unittest.main()
