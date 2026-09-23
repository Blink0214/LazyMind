import tempfile
import unittest
from pathlib import Path
import yaml
from shared.case_loader import load_writer_e2e_scenario


class ProviderRegistryTests(unittest.TestCase):
    def test_current_cases_resolve(self):
        for id, provider in [('R01', 'notion'), ('R02', 'wechat'), ('R03', 'github'), ('R04', 'obsidian')]:
            case = load_writer_e2e_scenario(id)
            self.assertNotIn('${', case.prompt_text)
            self.assertEqual(case.extras['provider_reference']['provider'], provider)
            self.assertIn(case.extras['provider_reference']['reference'], case.prompt_text)

    def test_registry_change_and_invalid_reference(self):
        data = {'provider_docs': {'note': {'provider': 'notion', 'reference': 'https://example.test/new'}},
                'scenarios': [{'id': 'R99', 'provider_doc': 'note', 'request': {'text': '修改 ${note}'}}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cases.yaml'
            path.write_text(yaml.safe_dump(data))
            self.assertEqual(load_writer_e2e_scenario('R99', rules_path=path).prompt_text, '修改 https://example.test/new')
            data['provider_docs']['note']['reference'] = ''
            path.write_text(yaml.safe_dump(data))
            with self.assertRaises(ValueError):
                load_writer_e2e_scenario('R99', rules_path=path)
