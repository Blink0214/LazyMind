import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.notion import fetch_notion_document, page_id
from shared.github import fetch_github_document
from shared.obsidian import fetch_obsidian_document
from shared.readback_common import CliReader, sanitize

ID = '12345678-1234-1234-1234-123456789abc'


class ProviderReadbackTests(unittest.TestCase):
    def test_notion_url(self):
        self.assertEqual(page_id('https://www.notion.so/My-page-' + ID.replace('-', '') + '?x=1'), ID)
        with self.assertRaises(ValueError):
            page_id('https://evil.test/' + ID)

    @patch.object(CliReader, 'json')
    def test_notion_nested_and_paginated(self, call):
        call.side_effect = [
            {'object': 'page', 'last_edited_time': 'now', 'properties': {}},
            {'object': 'list', 'results': [{'id': 'table', 'type': 'table', 'has_children': True}],
             'has_more': True, 'next_cursor': 'next'},
            {'object': 'list', 'results': [{'type': 'table_row', 'table_row': {
                'cells': [[{'plain_text': '装备'}], [{'plain_text': '数量'}]]}}], 'has_more': False},
            {'object': 'list', 'results': [{'type': 'paragraph', 'paragraph': {
                'rich_text': [{'plain_text': '末段'}]}}], 'has_more': False}]
        result = fetch_notion_document(ID)
        self.assertEqual(result.content, '装备\t数量\n末段')
        self.assertIn('start_cursor==next', call.call_args.args[0])
        self.assertIn('children', result.raw['blocks'][0])

    @patch.object(CliReader, 'json')
    def test_notion_incomplete_pagination_fails(self, call):
        call.side_effect = [{'object': 'page'}, {'object': 'list', 'results': [], 'has_more': True}]
        with self.assertRaisesRegex(RuntimeError, 'cursor'):
            fetch_notion_document(ID)

    @patch.object(CliReader, 'json')
    def test_github_pins_commit(self, call):
        sha = 'a' * 40
        call.side_effect = [{'sha': sha}, {'type': 'file', 'encoding': 'base64',
                           'content': base64.b64encode('# 中文'.encode()).decode()}]
        result = fetch_github_document('owner/repo', '目录/note.md', ref='feature/docs')
        self.assertEqual(result.content, '# 中文')
        self.assertEqual(result.revision_id, sha)
        self.assertIn('?ref=' + sha, call.call_args.args[0][-1])
        self.assertIn('feature%2Fdocs', call.call_args_list[0].args[0][-1])

    @patch.object(CliReader, 'json')
    def test_github_rejects_directory(self, call):
        call.side_effect = [{'sha': 'a' * 40}, []]
        with self.assertRaises(RuntimeError):
            fetch_github_document('owner/repo', 'docs', ref='main')

    def test_obsidian_read_and_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / 'vault'
            vault.mkdir()
            (vault / '笔记.md').write_text('# 原文\n![[图片.png]]', encoding='utf-8')
            doc = fetch_obsidian_document(vault, '笔记.md')
            self.assertIn('![[图片.png]]', doc.content)
            self.assertEqual(len(doc.revision_id), 64)
            (root / 'outside.md').write_text('outside')
            (vault / 'escape.md').symlink_to(root / 'outside.md')
            with self.assertRaises(ValueError):
                fetch_obsidian_document(vault, 'escape.md')
            with self.assertRaises(ValueError):
                fetch_obsidian_document(vault, '../outside.md')

    def test_signed_urls_sanitized_without_modifying_normal_links(self):
        value = {'url': 'https://files.test/img?X-Amz-Signature=secret',
                 'text': '![a](https://files.test/a?token=secret)',
                 'normal': 'https://example.com/?q=hello'}
        encoded = json.dumps(sanitize(value))
        self.assertNotIn('secret', encoded)
        self.assertIn('?q=hello', encoded)


if __name__ == '__main__':
    unittest.main()
