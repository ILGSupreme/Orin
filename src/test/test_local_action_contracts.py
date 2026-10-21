"""Regression coverage for the contracts exercised by the local-model lab."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from common.primer import Primer
from common.protocol.adapter.openai_adapter import OpenAIStyleMessageAdapter
from common.protocol.agent_types import Decision
from common.protocol.agent_types import TurnPlan
from common.protocol.grammar_schema import grammar_schema
from common.protocol.unified_types import ContentPart, RuntimeMessage
from common.system.configuration import InferenceConfiguration
from cortex.cortex.actions import ActionRegistry
from cortex.cortex.inference import InferenceGateway
from cortex.cortex.inference import strict_schema


class LocalActionContractTests(unittest.IsolatedAsyncioTestCase):
    def test_grammar_projection_retains_contract_and_runtime_bounds(self):
        original = strict_schema(TurnPlan)
        snapshot = json.dumps(original, sort_keys=True)
        compact = grammar_schema(original)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)
        self.assertNotIn('maxLength', json.dumps(compact))
        self.assertNotIn('maxItems', json.dumps(compact))
        self.assertEqual(compact['required'], original['required'])
        self.assertFalse(compact['additionalProperties'])
        self.assertEqual(compact['$defs']['GoalUpdate']['properties']['action']['enum'],
                         original['$defs']['GoalUpdate']['properties']['action']['enum'])
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            TurnPlan(reply='x' * 6001)
        with self.assertRaises(ValidationError):
            Decision(kind='finish', text='x' * 3001)

    async def test_inspection_accepts_only_empty_input_or_exact_action_echo(self):
        commands = SimpleNamespace(execute_agent_action=AsyncMock(return_value='observed nodes'))
        actions = ActionRegistry(inference=None, commands=commands)
        for name in ('list_nodes', 'list_pods', 'cluster_snapshot'):
            for text in ('', '  ', name, ' ' + name + ' '):
                result = await actions.execute(name, text, messages=[], privacy='local_only',
                                               budget=100, checkpoint=None, charge=None)
                self.assertEqual(result.text, 'observed nodes')
                commands.execute_agent_action.assert_awaited_with(name)
            for text in (name + ' --all', 'delete nodes', '/list_nodes', 'other input'):
                commands.execute_agent_action.reset_mock()
                with self.assertRaises(ValueError):
                    await actions.execute(name, text, messages=[], privacy='local_only',
                                          budget=100, checkpoint=None, charge=None)
                commands.execute_agent_action.assert_not_awaited()

    async def test_schema_grammar_reaches_native_completion_through_real_local_stack(self):
        # Replace only native dependencies. Run the real gateway, Primer,
        # message adapter and GGUF generation methods without model weights.
        schemas, native_calls = [], []

        class Grammar:
            @classmethod
            def from_json_schema(cls, schema, verbose):
                schemas.append(json.loads(schema))
                return cls()

        def completion(**kwargs):
            native_calls.append(kwargs)
            return {'choices': [{'message': {'content': json.dumps({
                'kind': 'execute', 'action': 'list_nodes', 'text': '',
                'satisfied': False, 'evidence': [],
            })}}]}

        path = Path(__file__).parents[1] / 'common/engine/gguf.py'
        spec = importlib.util.spec_from_file_location('_lab_gguf', path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
            'llama_cpp': SimpleNamespace(Llama=object, LlamaGrammar=Grammar),
            'transformers': SimpleNamespace(AutoTokenizer=object),
        }):
            spec.loader.exec_module(module)
            engine = module.GGUFPrimerEngine.__new__(module.GGUFPrimerEngine)
            engine.engine = SimpleNamespace(create_chat_completion=completion)
            engine.max_new_tokens, engine.temperature, engine.top_p = 512, 0, 0.95
            engine.is_ready = lambda: True
            engine.count_tokens = lambda **kw: 20
            engine.status = lambda: {'effective_n_ctx': 4096}
            primer = Primer(external_http=None, cfg='cortex')
            primer._engine, primer.engine_type = engine, 'gguf'
            primer._message_adapter = OpenAIStyleMessageAdapter()
            gateway = InferenceGateway(InferenceConfiguration(), primer=primer, router=None, http=None)
            with patch.object(primer, 'primer_config', return_value=SimpleNamespace(max_new_tokens=1400)):
                result = await gateway.generate('decide', [RuntimeMessage(role='user', parts=[
                    ContentPart(type='text', data='Inspect nodes'),
                ])], privacy='local_only', model=Decision)
        self.assertEqual(Decision.model_validate_json(result.text).action, 'list_nodes')
        self.assertFalse(schemas[0]['additionalProperties'])
        self.assertEqual(set(schemas[0]['required']), set(schemas[0]['properties']))
        self.assertIsInstance(native_calls[0]['grammar'], Grammar)
