"""Real MCP stdio handshake/tool calls with a controlled kubectl executable."""

import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = Path(__file__).resolve().parents[1] / "server.py"
FAKE_KUBECTL = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if os.environ.get('FAKE_DENIED') == '1':
    print('Forbidden: pods is forbidden for this user', file=sys.stderr)
    sys.exit(1)
if 'logs' in args:
    print('2026-10-20T00:00:00Z Previous Cortex instance log')
elif 'pods' in args:
    print(json.dumps({'items': [{
        'metadata': {'name': 'cortex-test-abc'},
        'spec': {'nodeName': 'master-node', 'containers': [{
            'name': 'cortex', 'image': 'orin-gw:5000/cortex:test',
            'env': [{'name': 'PRIVATE_KEY', 'value': 'DO_NOT_RETURN_THIS'}],
        }]},
        'status': {'phase': 'Running', 'containerStatuses': [{
            'name': 'cortex', 'ready': True, 'restartCount': 2,
            'imageID': 'registry/cortex@sha256:test', 'state': {'running': {}},
        }]},
    }]}))
elif 'nodes' in args:
    print(json.dumps({'items': [
        {'metadata': {'name': 'master-node'}, 'status': {
            'nodeInfo': {'architecture': 'amd64'},
            'conditions': [{'type': 'Ready', 'status': 'True', 'reason': 'KubeletReady'}],
        }},
        {'metadata': {'name': 'jetson-1'}, 'status': {
            'nodeInfo': {'architecture': 'arm64'},
            'conditions': [{'type': 'Ready', 'status': 'False'}],
        }},
    ]}))
else:
    sys.exit(2)
'''


class LabServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.calls = self.directory / "calls.jsonl"
        executable = self.directory / "kubectl"
        executable.write_text("#!" + sys.executable + "\n" + FAKE_KUBECTL)
        executable.chmod(0o700)
        self.environment = {
            **os.environ, "ORIN_LAB_KUBECTL": str(executable),
            "ORIN_LAB_NAMESPACE": "orin", "FAKE_CALLS": str(self.calls),
        }

    @asynccontextmanager
    async def session(self):
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=self.environment)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                yield session

    def commands(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    async def test_real_handshake_and_read_only_tool_metadata(self):
        async with self.session() as session:
            tools = (await session.list_tools()).tools
            self.assertEqual({tool.name for tool in tools},
                             {"server_info", "get_pods", "pod_logs", "get_nodes"})
            self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
            info = await session.call_tool("server_info", {})
            self.assertFalse(info.isError)
            self.assertEqual(info.structuredContent['namespace'], 'orin')

    async def test_pod_summary_uses_live_command_and_omits_environment(self):
        async with self.session() as session:
            result = await session.call_tool('get_pods', {})
        self.assertFalse(result.isError)
        pod = result.structuredContent['pods'][0]
        self.assertEqual(pod['name'], 'cortex-test-abc')
        self.assertTrue(pod['containers'][0]['ready'])
        self.assertEqual(pod['containers'][0]['restart_count'], 2)
        self.assertEqual(pod['containers'][0]['image_id'], 'registry/cortex@sha256:test')
        self.assertNotIn('DO_NOT_RETURN_THIS', json.dumps(result.structuredContent))
        self.assertIn('--namespace', self.commands()[0])
        self.assertIn('orin', self.commands()[0])

    async def test_previous_logs_are_bounded_and_have_explicit_container(self):
        async with self.session() as session:
            result = await session.call_tool('pod_logs', {
                'pod': 'cortex-test-abc', 'container': 'cortex', 'previous': True, 'tail': 80,
            })
        self.assertFalse(result.isError)
        self.assertIn('Previous Cortex', result.structuredContent['logs'])
        args = self.commands()[0]
        self.assertIn('--previous=true', args)
        self.assertIn('--tail=80', args)
        self.assertIn('--limit-bytes=65536', args)
        self.assertEqual(args[args.index('--container') + 1], 'cortex')

    async def test_invalid_log_inputs_never_reach_kubectl(self):
        async with self.session() as session:
            for arguments in (
                {'pod': '--all-namespaces'}, {'pod': 'cortex; touch /tmp/attack'},
                {'pod': 'cortex', 'tail': -1}, {'pod': 'cortex', 'tail': 501},
                {'pod': 'cortex', 'container': '../secret'},
            ):
                result = await session.call_tool('pod_logs', arguments)
                self.assertTrue(result.isError, arguments)
        self.assertEqual(self.commands(), [])

    async def test_nodes_report_ready_conditions_not_merely_registration(self):
        async with self.session() as session:
            result = await session.call_tool('get_nodes', {})
        self.assertFalse(result.isError)
        nodes = result.structuredContent['nodes']
        self.assertEqual([node['ready'] for node in nodes], ['True', 'False'])
        self.assertEqual([node['architecture'] for node in nodes], ['amd64', 'arm64'])

    async def test_permission_failure_is_a_tool_error_not_an_empty_pod_list(self):
        self.environment['FAKE_DENIED'] = '1'
        async with self.session() as session:
            result = await session.call_tool('get_pods', {})
        self.assertTrue(result.isError)
        self.assertIn('Forbidden', result.content[0].text)


if __name__ == '__main__':
    unittest.main()
