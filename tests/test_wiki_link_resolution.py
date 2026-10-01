from types import SimpleNamespace
import unittest
from uuid import uuid4

from knowgrain.wiki_repository import WikiRepository


class WikiLinkResolutionTests(unittest.TestCase):
    def test_missing_explicit_evidence_path_does_not_bind_to_wiki_basename(self):
        target_id, source_id = uuid4(), uuid4()
        source = SimpleNamespace(
            page_id=source_id, vault_path="Wiki/Drafts/Source.md", title="Source",
            links=[SimpleNamespace(target="Sources/Evidence/Shared", anchor=None)],
        )
        target = SimpleNamespace(
            page_id=target_id, vault_path="Wiki/Pages/Shared.md", title="Shared", links=[],
        )
        links = WikiRepository._resolve_targets([source, target])[source_id]
        self.assertIsNone(links[0][1])
        source.links[0].target = "Shared"
        links = WikiRepository._resolve_targets([source, target])[source_id]
        self.assertEqual(links[0][1], target_id)

    def test_relative_path_resolves_but_missing_nested_path_does_not_fall_back(self):
        source_id, target_id = uuid4(), uuid4()
        source = SimpleNamespace(
            page_id=source_id, vault_path="Wiki/Drafts/Folder/Source.md", title="Source",
            links=[SimpleNamespace(target="Child/Target", anchor="Heading")],
        )
        target = SimpleNamespace(
            page_id=target_id, vault_path="Wiki/Drafts/Folder/Child/Target.md", title="Target", links=[],
        )
        self.assertEqual(WikiRepository._resolve_targets([source, target])[source_id][0][1], target_id)
        source.links[0].target = "Missing/Target"
        self.assertIsNone(WikiRepository._resolve_targets([source, target])[source_id][0][1])
