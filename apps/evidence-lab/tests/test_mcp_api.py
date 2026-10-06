"""Real MCP HTTP protocol requests with deterministic or no inference."""
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from evidence_lab.api import create_app
from evidence_lab.config import load_config
from test_api import APIStore, NoInferenceHub

HEADERS = {'Authorization': 'Bearer operator-test-token',
           'Accept': 'application/json, text/event-stream',
           'MCP-Protocol-Version': '2025-11-25'}


def rpc(client, method, params=None):
    return client.post('/api/mcp/', headers=HEADERS,
                       json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})


def operator_config():
    config = load_config('configs/mock.yaml')
    config.runtime.operator_token = SecretStr('operator-test-token')
    return config


def test_mcp_initialization_and_only_readonly_tools():
    with TestClient(create_app(operator_config(), store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        initialized = rpc(client, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                                'clientInfo': {'name': 'test', 'version': '1'}})
        assert initialized.status_code == 200
        assert initialized.json()['result']['serverInfo']['name'] == 'Evidence Lab RAG'
        tools = rpc(client, 'tools/list').json()['result']['tools']
        assert {tool['name'] for tool in tools} == {'search_evidence', 'get_evidence_source'}
        assert all(tool['annotations']['readOnlyHint'] for tool in tools)
        assert all('outputSchema' in tool for tool in tools)


def test_mcp_current_protocol_discovery_without_a_session():
    with TestClient(create_app(operator_config(), store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/mcp/', headers={**HEADERS, 'MCP-Protocol-Version': '2026-07-28',
                                                    'Mcp-Method': 'tools/list'}, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {'_meta': {
                'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                'io.modelcontextprotocol/clientCapabilities': {},
            }},
        })
        assert response.status_code == 200
        assert {tool['name'] for tool in response.json()['result']['tools']} == {
            'search_evidence', 'get_evidence_source',
        }
        assert 'Mcp-Session-Id' not in response.headers


async def test_official_sdk_http_client_discovers_tools():
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    app = create_app(operator_config(), store=APIStore(), hub=NoInferenceHub(), initialize=False)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={
            'Authorization': 'Bearer operator-test-token',
        }) as http:
            async with Client(streamable_http_client('http://127.0.0.1:8000/api/mcp/', http_client=http)) as agent:
                tools = await agent.list_tools()
                assert {tool.name for tool in tools.tools} == {'search_evidence', 'get_evidence_source'}


@pytest.mark.parametrize('token', [None, '', ' '])
def test_mcp_requires_operator_token_even_in_mock_mode(token):
    config = load_config('configs/mock.yaml')
    config.runtime.operator_token = SecretStr(token) if token is not None else None
    with TestClient(create_app(config, store=APIStore(), hub=NoInferenceHub(), initialize=False)) as client:
        assert rpc(client, 'tools/list').status_code == 401


def test_mcp_rejects_wrong_token_and_host_before_tool_execution():
    with TestClient(create_app(operator_config(), store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        assert client.post('/api/mcp/', headers={**HEADERS, 'Authorization': 'Bearer wrong'},
                           json={}).status_code == 401
        assert client.post('/api/mcp/', headers={**HEADERS, 'Host': 'attacker.test'},
                           json={}).status_code == 421
        assert client.post('/api/mcp/', headers={**HEADERS, 'Origin': 'https://attacker.test'},
                           json={}).status_code == 403


def test_mcp_remote_hostname_requires_explicit_allowlist():
    config = operator_config()
    with TestClient(create_app(config, store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='https://rag.service.test') as client:
        assert rpc(client, 'tools/list').status_code == 421
    config.agent_rag.allowed_hosts.append('rag.service.test')
    config.agent_rag.allowed_origins.append('https://rag.service.test')
    with TestClient(create_app(config, store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='https://rag.service.test') as client:
        assert rpc(client, 'tools/list').status_code == 200


def test_mcp_honors_allowed_origin_without_weakening_rest_auth_or_write_policy():
    config = operator_config()
    origin = 'http://localhost:3000'
    config.agent_rag.allowed_origins.append(origin)
    store = APIStore()
    request_headers = {**HEADERS, 'Origin': origin, 'Sec-Fetch-Site': 'cross-site'}
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
    with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        allowed = client.post('/api/mcp/', headers=request_headers, json=payload)
        assert allowed.status_code == 200
        assert len(allowed.json()['result']['tools']) == 2
        wrong_token = client.post('/api/mcp/', json=payload, headers={
            **request_headers, 'Authorization': 'Bearer wrong',
        })
        assert wrong_token.status_code == 401
        denied_origin = client.post('/api/mcp/', json=payload, headers={
            **request_headers, 'Origin': 'https://attacker.test',
        })
        assert denied_origin.status_code == 403
        rest = client.post('/api/queries', headers=request_headers, json={'question': 'Question?'})
        assert rest.status_code == 403
        assert store.run['id'] == 'r1'


def test_mcp_rejects_oversized_http_body():
    with TestClient(create_app(operator_config(), store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/mcp/', headers={**HEADERS, 'Content-Type': 'application/json'},
                               content=b' ' * 65537)
        assert response.status_code == 413


@pytest.mark.parametrize('failure', ['budget', 'internal', 'large_result'])
def test_mcp_safe_errors_and_result_bounds(monkeypatch, failure):
    from evidence_lab.domain import EvidenceItem, EvidencePack, ProviderError
    import evidence_lab.mcp_server as module

    config = operator_config()
    config.agent_rag.max_result_bytes = 1024

    async def retrieve(*args):
        if failure == 'budget':
            raise ProviderError('budget_exhausted', 'Budget exhausted.')
        if failure == 'internal':
            raise ValueError('private-secret-do-not-log')
        return EvidencePack(corpus_id='default', space_id='fixture-space', corpus_revision=1,
                            items=[EvidenceItem(id='e', document_id='d', version_id='v', title='source', text='x' * 2000)])

    monkeypatch.setattr(module, 'retrieve_evidence', retrieve)
    with TestClient(create_app(config, store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        response = rpc(client, 'tools/call', {'name': 'search_evidence', 'arguments': {'question': 'Question?'}})
        assert response.json()['result']['isError'] is True
        assert 'private-secret' not in response.text
        code = {'budget': 'budget_exhausted', 'internal': 'internal_error', 'large_result': 'over_budget'}[failure]
        assert code in response.text


@pytest.mark.parametrize('arguments', [
    {'question': 'Q?', 'corpus_id': 'private'}, {'question': ' '},
    {'question': 'Q?' * 10000}, {'question': 'Q?', 'corpus_id': '../private'},
])
def test_mcp_rejects_invalid_or_ungranted_corpus_before_retrieval(arguments):
    with TestClient(create_app(operator_config(), store=APIStore(), hub=NoInferenceHub(), initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        response = rpc(client, 'tools/call', {'name': 'search_evidence', 'arguments': arguments})
        assert response.status_code == 200
        assert response.json()['result']['isError'] is True


@pytest.mark.integration
@pytest.mark.native_postgres
def test_mcp_native_search_source_and_deleted_source(isolated_storage_dsn):
    from evidence_lab.api import make_store
    from evidence_lab.providers import ProviderHub
    from evidence_lab.retrieval import space_manifest
    from evidence_lab.worker import Worker
    import asyncio

    config = operator_config()
    config.agent_rag.allowed_corpora = ['default', 'other']
    config.database.dsn = isolated_storage_dsn
    store = make_store(config)
    store.ensure_corpus('default', space_manifest(config))
    document = store.create_document('source.txt', b'The Atlas refund period is 30 days.', 'text/plain')
    hub = ProviderHub(config, store=store)
    asyncio.run(Worker(store, hub, config).run_once())
    with TestClient(create_app(config, store=store, hub=hub, initialize=False),
                    base_url='http://127.0.0.1:8000') as client:
        response = rpc(client, 'tools/call', {'name': 'search_evidence',
                                            'arguments': {'question': 'Atlas refund period?'}})
        result = response.json()['result']
        assert not result.get('isError', False)
        data = result['structuredContent']
        assert data['qualification'] == 'fixture_only' and data['verified_answer'] is False
        assert data['evidence']['corpus_revision'] == store.get_corpus('default')['revision']
        item = data['evidence']['items'][0]
        assert item['text'] == 'The Atlas refund period is 30 days.'
        arguments = {'corpus_id': 'default', 'version_id': item['version_id'], 'evidence_id': item['id']}
        source = rpc(client, 'tools/call', {'name': 'get_evidence_source', 'arguments': arguments})
        assert source.json()['result']['structuredContent']['item']['text_hash'] == item['text_hash']
        foreign = rpc(client, 'tools/call', {'name': 'get_evidence_source',
                                           'arguments': {**arguments, 'corpus_id': 'other'}})
        assert foreign.json()['result']['isError'] is True
        store.ensure_corpus('other', space_manifest(config))
        empty = rpc(client, 'tools/call', {'name': 'search_evidence',
                                         'arguments': {'question': 'Missing source?', 'corpus_id': 'other'}})
        assert empty.json()['result']['structuredContent']['evidence']['items'] == []
        assert not any(call['profile'] == config.roles.generator for call in store.get_calls())
        calls_before = len(store.get_calls())
        config.role_profile('embeddings').model = 'changed-embedding-model'
        mismatch = rpc(client, 'tools/call', {'name': 'search_evidence',
                                            'arguments': {'question': 'Atlas refund?'}})
        assert mismatch.json()['result']['isError'] is True
        assert 'space_changed' in mismatch.text
        assert len(store.get_calls()) == calls_before
        store.delete_document(document['document_id'])
        missing = rpc(client, 'tools/call', {'name': 'get_evidence_source', 'arguments': arguments})
        assert missing.json()['result']['isError'] is True
        assert 'test-token' not in json.dumps(data)
    asyncio.run(hub.aclose())
