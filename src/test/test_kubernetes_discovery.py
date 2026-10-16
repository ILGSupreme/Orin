"""Exercise the installed Kubernetes client through real local HTTP transport."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from cortex.cluster.discovery.discovery_k8s import KubernetesDiscoveryProvider


class KubernetesDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.paths = []
        self.endpoint_status = 200
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.paths.append(self.path)
                if self.path.startswith('/api/v1/namespaces/orin/services'):
                    status = 200
                    payload = {
                        'apiVersion': 'v1', 'kind': 'ServiceList',
                        'items': [{
                            'metadata': {
                                'name': 'memory-service', 'namespace': 'orin',
                                'labels': {'orin.ai/backend': 'true', 'orin.ai/role': 'memory'},
                            },
                            'spec': {'ports': [{'name': 'http', 'port': 8080}]},
                        }],
                    }
                else:
                    status = test.endpoint_status
                    payload = {
                        'apiVersion': 'discovery.k8s.io/v1', 'kind': 'EndpointSliceList',
                        'items': [{
                            'metadata': {'labels': {'kubernetes.io/service-name': 'memory-service'}},
                            'addressType': 'IPv4',
                            'endpoints': [
                                {'addresses': ['10.0.0.1'], 'conditions': {'ready': True}},
                                {'addresses': ['10.0.0.2'], 'conditions': {'ready': False}},
                                {'addresses': ['10.0.0.3']},
                            ],
                        }],
                    } if status == 200 else {'message': 'discovery unavailable'}
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        cfg = client.Configuration(host=f'http://127.0.0.1:{server.server_port}')
        cfg.retries = 0
        api = client.ApiClient(cfg)
        self.addCleanup(api.close)
        self.provider = KubernetesDiscoveryProvider.__new__(KubernetesDiscoveryProvider)
        self.provider.namespace = 'orin'
        self.provider.core = client.CoreV1Api(api)
        self.provider.discovery = client.DiscoveryV1Api(api)

    async def test_discovery_uses_real_client_and_counts_only_ready_endpoints(self):
        backends = await self.provider.discover()
        self.assertEqual(len(backends), 1)
        self.assertEqual(backends[0].service_name, 'memory-service')
        self.assertEqual(backends[0].health.ready_endpoints, 1)
        self.assertEqual(backends[0].url, 'http://memory-service.orin.svc.cluster.local:8080')
        self.assertIn('/apis/discovery.k8s.io/v1/namespaces/orin/endpointslices', self.paths)
        query = parse_qs(urlsplit(self.paths[0]).query)
        self.assertEqual(query['labelSelector'], ['orin.ai/backend=true'])

    async def test_endpoint_api_failure_is_not_reported_as_empty_discovery(self):
        self.endpoint_status = 500
        with self.assertRaises(ApiException) as raised:
            await self.provider.discover()
        self.assertEqual(raised.exception.status, 500)
