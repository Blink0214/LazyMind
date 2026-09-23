import os
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import URLError
from shared.wechat import fetch_wechat_document, _post_json


class WeChatReadbackTests(unittest.TestCase):
    @patch.dict(os.environ, {'LAZYMIND_E2E_WECHAT_ACCESS_TOKEN': 'private-token'}, clear=True)
    @patch('shared.wechat._post_json')
    def test_token_and_article_selection(self, post):
        post.return_value = {'news_item': [{'content': '<p>first</p>'}, {
            'title': '测试', 'content': '<p>正文</p><img data-src="https://img.test/a" alt="配图">',
            'content_source_url': 'https://original.test', 'thumb_media_id': 'cover'}]}
        doc = fetch_wechat_document('draft', article_index=1)
        self.assertEqual(doc.title, '测试')
        self.assertEqual(doc.url, '')
        self.assertEqual(doc.raw['images'][0]['src'], 'https://img.test/a')
        self.assertNotIn('private-token', str(doc.to_dict()))
        self.assertEqual(post.call_args.args[1], {'media_id': 'draft'})

    @patch.dict(os.environ, {'LAZYMIND_E2E_WECHAT_APP_ID': 'app',
                            'LAZYMIND_E2E_WECHAT_APP_SECRET': 'secret'}, clear=True)
    @patch('shared.wechat._post_json')
    def test_stable_token(self, post):
        post.side_effect = [{'access_token': 'token'}, {'news_item': [{'content': '<p>text</p>'}]}]
        fetch_wechat_document('draft')
        self.assertIs(post.call_args_list[0].args[1]['force_refresh'], False)
        self.assertEqual(post.call_count, 2)

    @patch.dict(os.environ, {}, clear=True)
    @patch('shared.wechat._post_json')
    def test_missing_credentials_and_bad_index(self, post):
        with self.assertRaises(RuntimeError):
            fetch_wechat_document('draft')
        with self.assertRaises(ValueError):
            fetch_wechat_document('draft', article_index=-1)
        post.assert_not_called()

    @patch.dict(os.environ, {'LAZYMIND_E2E_WECHAT_ACCESS_TOKEN': 'token'}, clear=True)
    @patch('shared.wechat._post_json')
    def test_incomplete_responses_fail(self, post):
        for value in ({}, {'news_item': []}, {'news_item': [{}]}):
            post.return_value = value
            with self.assertRaises(RuntimeError):
                fetch_wechat_document('draft')

    @patch('shared.wechat.request.build_opener')
    def test_network_errors_do_not_leak_token(self, opener):
        opener.return_value.open.side_effect = URLError('https://x?access_token=secret')
        with self.assertRaises(RuntimeError) as error:
            _post_json('draft/get?access_token=secret', {}, 1)
        self.assertNotIn('secret', str(error.exception))

    @patch('shared.wechat.request.build_opener')
    def test_api_error_does_not_leak_message(self, opener):
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"errcode":40014,"errmsg":"secret"}'
        with self.assertRaisesRegex(RuntimeError, '40014') as error:
            _post_json('draft/get', {}, 1)
        self.assertNotIn('secret', str(error.exception))


class WeChatEnvTests(unittest.TestCase):
    @patch.dict(os.environ, {'LAZYMIND_E2E_WECHAT_APP_ID': 'environment'}, clear=True)
    def test_env_load_is_selective_and_preserves_environment(self):
        import tempfile
        from pathlib import Path
        from shared.readback import load_wechat_env
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('LAZYMIND_E2E_WECHAT_APP_ID=file\n'
                            'LAZYMIND_E2E_WECHAT_APP_SECRET="literal${VALUE}"\n'
                            'UNRELATED=value\n', encoding='utf-8')
            load_wechat_env(path)
            self.assertEqual(os.environ['LAZYMIND_E2E_WECHAT_APP_ID'], 'environment')
            self.assertEqual(os.environ['LAZYMIND_E2E_WECHAT_APP_SECRET'], 'literal${VALUE}')
            self.assertNotIn('UNRELATED', os.environ)
