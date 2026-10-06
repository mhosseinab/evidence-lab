"""Dashboard modes and provider setup are isolated request inputs."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from evidence_lab.api import create_app
from evidence_lab.config import load_config
from test_api import APIStore, NoInferenceHub


def live_settings():
    return {
        'embedding_endpoint': 'https://llm.test/v1/embeddings', 'embedding_model': 'embedding-v1',
        'embedding_dimensions': 3, 'embedding_max_input_tokens': 8192,
        'chat_endpoint': 'https://llm.test/v1/chat/completions', 'chat_model': 'chat-v1',
        'chat_max_input_tokens': 65536, 'chat_max_output_tokens': 1200,
        'structured_output': 'text_json', 'output_limit_parameter': 'max_tokens',
        'cloudflare_account_id': 'a' * 32, 'budget_usd': 0.0,
        'embedding_input_usd_per_million': 1.0, 'chat_input_usd_per_million': 1.0,
        'chat_output_usd_per_million': 1.0,
    }


def headers(settings=None, mode='live'):
    return {'X-Evidence-Lab-Mode': mode,
            'X-Evidence-Lab-Live-Settings': json.dumps(settings or live_settings())}


@pytest.mark.parametrize('route', [
    '/api/queries', '/api/documents', '/api/evaluations',
    '/api/jobs/j1/retry', '/api/retrieval/preview',
])
@pytest.mark.parametrize('authentication', ['unconfigured', 'missing', 'wrong', 'empty', 'whitespace'])
def test_server_inference_requires_configured_operator_authentication(route, authentication):
    from pydantic import SecretStr

    config = load_config('configs/mock.yaml')
    config.runtime.mode = 'live'
    config.verification.mode = 'shadow'
    config.verification.policy_id = None
    config.role_profile('generator').api_key = SecretStr('server-secret-never-expose')
    if authentication != 'unconfigured':
        config.runtime.operator_token = SecretStr('operator-test-token')
    # Also defend explicitly supplied config objects that bypass YAML validation.
    if authentication in {'empty', 'whitespace'}:
        config.runtime.operator_token = SecretStr('' if authentication == 'empty' else ' ')
    store = APIStore()
    request_headers = {'Authorization': 'Bearer wrong'} if authentication == 'wrong' else {}
    if authentication == 'whitespace':
        request_headers = {'Authorization': 'Bearer  '}
    with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False)) as client:
        if route == '/api/documents':
            response = client.post(route, headers=request_headers,
                                   files={'file': ('source.txt', b'Untrusted source.', 'text/plain')})
        else:
            payload = {'question': 'Question?'} if route in {'/api/queries', '/api/retrieval/preview'} else {}
            response = client.post(route, headers=request_headers, json=payload)
    assert response.status_code == 401
    assert 'server-secret-never-expose' not in response.text
    assert not store.created_documents and not store.enqueued
    assert store.run['id'] == 'r1'
    assert store.jobs['j1']['status'] == 'running'


def test_authenticated_operator_can_enqueue_server_inference():
    from pydantic import SecretStr

    config = load_config('configs/mock.yaml')
    config.runtime.mode = 'live'
    config.verification.mode = 'shadow'
    config.verification.policy_id = None
    config.runtime.operator_token = SecretStr('operator-test-token')
    store = APIStore()
    with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False)) as client:
        response = client.post('/api/queries', headers={'Authorization': 'Bearer operator-test-token'},
                               json={'question': 'Question?'})
    assert response.status_code == 202
    assert store.run['id'] == 'new-run' and store.run['status'] == 'queued'


def test_mode_metadata_validates_without_keys_or_model_calls():
    config = load_config('configs/mock.yaml')
    with TestClient(create_app(config, store=APIStore(), hub=NoInferenceHub(), initialize=False)) as client:
        mock = client.get('/api/status').json()
        response = client.get('/api/status', headers=headers())
        assert response.status_code == 200
        live = response.json()
        assert live['mode'] == 'live' and live['credentials'] == 'browser'
        assert live['byok_key_scope'] == mock['byok_key_scope']
        assert {profile['key_group'] for profile in live['byok_profiles']} == {'llm', 'cloudflare'}
        assert live['profiles']['generator']['model'] == 'chat-v1'
        assert live['policy_state'] == 'shadow'
        assert live['embedding_space_matches'] is False
        assert client.get('/api/status', headers=headers(mode='mock')).json()['mode'] == 'mock'
    assert config.runtime.mode == 'mock'
    assert config.roles.generator == 'fixture-generator'


def test_incomplete_live_setup_and_invalid_mode_fail_actionably():
    with TestClient(create_app(load_config('configs/mock.yaml'), store=APIStore(), hub=NoInferenceHub(), initialize=False)) as client:
        missing = client.get('/api/status', headers={'X-Evidence-Lab-Mode': 'live'})
        assert missing.status_code == 422
        assert missing.json()['code'] == 'invalid_configuration'
        invalid = client.get('/api/status', headers=headers(mode='unsafe-mode'))
        assert invalid.status_code == 422


def test_simultaneous_browser_modes_do_not_change_each_other():
    with TestClient(create_app(load_config('configs/mock.yaml'), store=APIStore(), hub=NoInferenceHub(), initialize=False)) as client:
        def status(mode):
            return client.get('/api/status', headers=headers(mode=mode)).json()['mode']
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(status, ['live', 'mock', 'live', 'mock'])) == ['live', 'mock', 'live', 'mock']


@pytest.mark.integration
@pytest.mark.native_postgres
@pytest.mark.parametrize('kind', ['ingest', 'evaluation'])
def test_operator_retry_releases_browser_job_to_server_worker(isolated_storage_dsn, kind):
    from pydantic import SecretStr
    from evidence_lab.storage import Store

    config = load_config('configs/mock.yaml')
    config.runtime.operator_token = SecretStr('operator-test-token')
    store = Store(isolated_storage_dsn)
    job = store.enqueue_job(kind, {}, browser_credentials=True)
    claimed = store.claim_job('browser-worker', browser_job_id=job['id'])
    assert claimed is not None
    store.finish_job(job['id'], claimed['token'], 'failed')

    with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False)) as client:
        response = client.post(f"/api/jobs/{job['id']}/retry", headers={
            'Authorization': 'Bearer operator-test-token',
        })
        assert response.status_code == 202

    retried = store.claim_job('server-worker')
    assert retried is not None and retried['id'] == job['id']
    assert retried['payload']['browser_credentials'] is False


@pytest.mark.integration
@pytest.mark.native_postgres
def test_custom_live_ingestion_is_api_owned_and_uses_the_llm_key(isolated_storage_dsn, monkeypatch):
    import httpx
    from evidence_lab.api import make_store
    from evidence_lab.providers import ProviderHub
    import evidence_lab.providers as provider_module

    config = load_config('configs/mock.yaml')
    config.database.dsn = isolated_storage_dsn
    settings = live_settings()
    settings['budget_usd'] = 1.0
    seen = []

    def endpoint(request):
        assert str(request.url) == 'https://llm.test/v1/embeddings'
        assert request.headers['authorization'] == 'Bearer llm-request-secret'
        seen.append(request.url.path)
        body = json.loads(request.content)
        return httpx.Response(200, json={'data': [
            {'index': index, 'embedding': [1.0, 0.0, 0.0]} for index in range(len(body['input']))
        ], 'usage': {'prompt_tokens': 5, 'total_tokens': 5}})

    monkeypatch.setattr(provider_module, 'ProviderHub', lambda cfg, *, store: ProviderHub(
        cfg, store=store, client=httpx.MockTransport(endpoint),
    ))
    store = make_store(config)
    metadata_headers = headers(settings)
    inference_headers = {**metadata_headers, 'X-Evidence-Lab-Provider-Keys': json.dumps(
        {'llm': 'llm-request-secret', 'cloudflare': 'cf-request-secret'})}
    with TestClient(create_app(config, store=store)) as client:
        assert client.post('/api/corpora', headers=metadata_headers, json={'corpus_id': 'live-test'}).status_code == 201
        response = client.post('/api/documents', headers=inference_headers, data={'corpus_id': 'live-test'},
                               files={'file': ('source.txt', b'Browser modes remain isolated.', 'text/plain')})
        assert response.status_code == 202
        job = store.get_job(response.json()['job_id'])
        assert job['status'] == 'succeeded'
        assert job['payload']['browser_credentials'] is True
        assert 'request-secret' not in json.dumps(job)
        assert 'request-secret' not in json.dumps(store.get_calls())
        assert client.get('/api/status', headers=metadata_headers, params={'corpus_id': 'live-test'}).json()['embedding_space_matches'] is True
        assert seen == ['/v1/embeddings']
    assert config.runtime.mode == 'mock'
