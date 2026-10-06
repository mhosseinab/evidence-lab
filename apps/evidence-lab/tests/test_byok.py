"""Browser credentials are transient request inputs, never durable job data."""
import json
from copy import deepcopy

import pytest
from pydantic import SecretStr

from evidence_lab.config import AppConfig, ConfigError


def browser_config(mock_data):
    data = deepcopy(mock_data)
    data['runtime']['mode'] = 'live'
    data['runtime']['credentials'] = 'browser'
    data['verification']['mode'] = 'shadow'
    data['verification']['policy_id'] = None
    for profile in data['profiles'].values():
        profile['endpoint'] = 'https://service.test/v1/model'
        profile['model'] = 'model-v1'
        profile['embedding_space'] = 'space-v1'
        for field in ('api_key', 'api_key_env', 'api_key_file'):
            profile.pop(field, None)
    return AppConfig.model_validate(data)


@pytest.fixture
def mock_data():
    import yaml
    from pathlib import Path
    return yaml.safe_load(Path('configs/mock.yaml').read_text())


def test_browser_config_requires_no_server_provider_keys(mock_data):
    config = browser_config(mock_data)
    assert all(p.api_key is None for p in config.profiles.values())
    data = config.model_dump()
    data['profiles'][config.roles.generator]['api_key'] = SecretStr('should-never-be-stored')
    with pytest.raises(ValueError, match='Browser credentials'):
        AppConfig.model_validate(data)


def test_request_keys_are_isolated_and_do_not_change_provenance(mock_data):
    from evidence_lab.byok import request_config
    config = browser_config(mock_data)
    keys = {name: 'client-only-secret' for name in config.active_profile_names()}
    scoped = request_config(config, json.dumps(keys))
    key = scoped.role_profile('generator').api_key
    assert key is not None and key.get_secret_value() == 'client-only-secret'
    assert config.role_profile('generator').api_key is None
    assert scoped.fingerprint() == config.fingerprint()
    assert 'client-only-secret' not in json.dumps(scoped.safe_dict())


def clef_browser_config(mock_data):
    data = browser_config(mock_data).model_dump()
    data['runtime']['require_openai_compatible'] = False
    data['profiles']['clef'] = {
        'protocol': 'cloudflare_clef',
        'endpoint': 'https://service.test/accounts/account/ai/run/@cf/cloudflare/clef',
        'model': 'clef',
        'max_input_tokens': 65536,
        'max_questions': 64,
    }
    data['roles']['verifier'] = 'clef'
    data['roles']['evaluation_judge'] = 'clef'
    return AppConfig.model_validate(data)


def test_grouped_keys_cover_all_active_profiles_without_changing_config(mock_data):
    from evidence_lab.byok import request_config
    config = clef_browser_config(mock_data)
    config.roles.repair_generator = config.roles.generator
    scoped = request_config(config, json.dumps({'llm': 'llm-secret', 'cloudflare': 'cf-secret'}))
    for name in config.active_profile_names():
        profile = scoped.profiles[name]
        expected = 'cf-secret' if profile.protocol == 'cloudflare_clef' else 'llm-secret'
        assert profile.api_key is not None
        assert profile.api_key.get_secret_value() == expected
        assert config.profiles[name].api_key is None
    assert scoped.fingerprint() == config.fingerprint()
    safe = json.dumps(scoped.safe_dict())
    assert 'llm-secret' not in safe and 'cf-secret' not in safe
    assert scoped.profiles['fixture-verifier'].api_key is None


@pytest.mark.parametrize('cloudflare', [None, ''])
def test_grouped_keys_allow_unused_cloudflare_key_to_be_absent_or_empty(mock_data, cloudflare):
    from evidence_lab.byok import request_config
    keys = {'llm': 'llm-secret'}
    if cloudflare is not None:
        keys['cloudflare'] = cloudflare
    config = request_config(browser_config(mock_data), json.dumps(keys))
    for name in config.active_profile_names():
        key = config.profiles[name].api_key
        assert key is not None
        assert key.get_secret_value() == 'llm-secret'


@pytest.mark.parametrize('keys', [
    {'llm': 'llm-secret'},
    {'llm': 'llm-secret', 'cloudflare': ''},
    {'llm': '', 'cloudflare': 'cf-secret'},
    {'llm': 'llm-secret', 'cloudflare': 'REPLACE_CLOUDFLARE_KEY'},
    {'llm': 'llm-secret', 'cloudflare': 'cf-secret\n'},
    {'llm': 'llm-secret', 'cloudflare': 'cf-secret', 'unknown': 'extra-secret'},
    {'llm': 123, 'cloudflare': 'cf-secret'},
    {'llm': 'x' * 4097, 'cloudflare': 'cf-secret'},
])
def test_grouped_keys_reject_missing_invalid_or_unknown_groups_safely(mock_data, keys):
    from evidence_lab.byok import request_config
    with pytest.raises(ConfigError) as caught:
        request_config(clef_browser_config(mock_data), json.dumps(keys))
    assert 'llm-secret' not in str(caught.value)
    assert 'cf-secret' not in str(caught.value)
    assert 'extra-secret' not in str(caught.value)


@pytest.mark.parametrize('header', [None, '{}', '{bad', '{"unknown":"client-only-secret"}', '[]'])
def test_missing_or_invalid_keys_fail_safely(mock_data, header):
    from evidence_lab.byok import request_config
    with pytest.raises(ConfigError) as caught:
        request_config(browser_config(mock_data), header)
    assert 'client-only-secret' not in str(caught.value)


def test_api_rejects_missing_keys_before_creating_a_job(mock_data):
    from fastapi.testclient import TestClient
    from evidence_lab.api import create_app
    from test_api import APIStore, NoInferenceHub
    config = browser_config(mock_data)
    store = APIStore()
    with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False)) as client:
        response = client.post('/api/queries', json={'question': 'Question?'})
        assert response.status_code == 422
        assert store.run['id'] == 'r1'
        assert 'client-only-secret' not in response.text
        status = client.get('/api/status').json()
        assert status['credentials'] == 'browser'
        assert len(status['byok_profiles']) == len(config.active_profile_names())


def test_api_credentials_are_request_scoped_and_never_in_job_settings(mock_data, monkeypatch):
    from fastapi.testclient import TestClient
    from evidence_lab.api import create_app
    from evidence_lab.worker import Worker
    from test_api import APIStore, NoInferenceHub
    config = browser_config(mock_data)
    store = APIStore()
    captured = []

    def claim(worker_id, lease_seconds, *, browser_job_id):
        assert browser_job_id == 'query-job'
        return {'id': browser_job_id, 'token': 'lease', 'kind': 'query', 'payload': {'run_id': 'new-run'}}

    async def process(worker, job):
        assert worker.config.role_profile('generator').api_key.get_secret_value() == 'client-only-secret'
        captured.append(worker.config)
        return {'status': 'succeeded'}

    monkeypatch.setattr(store, 'claim_job', claim, raising=False)
    monkeypatch.setattr(Worker, 'process', process)
    headers = {'X-Evidence-Lab-Provider-Keys': json.dumps({'llm': 'client-only-secret'})}
    with TestClient(create_app(config, store=store, hub=NoInferenceHub(), initialize=False)) as client:
        response = client.post('/api/queries', headers=headers, json={'question': 'Question?'})
        assert response.status_code == 202
        assert 'client-only-secret' not in response.text
        assert 'client-only-secret' not in json.dumps(store.run)
        assert 'client-only-secret' not in client.get('/api/runs/new-run/trace').text
    assert len(captured) == 1
    assert all(profile.api_key is None for profile in captured[0].profiles.values())
    assert config.role_profile('generator').api_key is None


@pytest.mark.integration
@pytest.mark.native_postgres
def test_browser_jobs_are_claimed_only_by_the_request_and_contain_no_keys(isolated_storage_dsn):
    from evidence_lab.storage import Store
    store = Store(isolated_storage_dsn, browser_credentials=True)
    first = store.enqueue_job('query', {'run_id': 'first'})
    second = store.enqueue_job('query', {'run_id': 'second'})
    assert first['payload'] == {'run_id': 'first', 'browser_credentials': True}
    assert store.claim_job('ordinary-worker') is None
    claimed = store.claim_job('browser-worker', browser_job_id=second['id'])
    assert claimed is not None and claimed['id'] == second['id']
    assert store.claim_job('browser-other', browser_job_id=second['id']) is None
    claimed = store.claim_job('browser-worker', browser_job_id=first['id'])
    assert claimed is not None and claimed['id'] == first['id']


@pytest.mark.integration
@pytest.mark.native_postgres
@pytest.mark.parametrize('provider_status', [200, 401])
def test_byok_ingestion_uses_transient_auth_without_persisting_keys(
    mock_data, isolated_storage_dsn, monkeypatch, provider_status,
):
    import httpx
    from datetime import date
    from fastapi.testclient import TestClient
    from evidence_lab.api import create_app, make_store
    from evidence_lab.config import Pricing
    from evidence_lab.providers import ProviderHub
    import evidence_lab.providers as provider_module

    config = browser_config(mock_data)
    config.database.dsn = isolated_storage_dsn
    config.budgets.total_max_estimated_cost_usd = 1
    config.budgets.phase_max_estimated_cost_usd['ingestion'] = 1
    for profile in config.profiles.values():
        profile.pricing = Pricing(input_usd_per_million=1, output_usd_per_million=1, checked_on=date.today().isoformat())
    seen = []

    def endpoint(request):
        assert request.headers['authorization'] == 'Bearer client-only-secret'
        seen.append(request.url.path)
        if provider_status != 200:
            return httpx.Response(provider_status, json={'error': {'message': 'client-only-secret'}})
        body = json.loads(request.content)
        assert 'input' in body
        return httpx.Response(200, json={'data': [
            {'index': index, 'embedding': [1.0] + [0.0] * 63} for index in range(len(body['input']))
        ], 'usage': {'prompt_tokens': 5, 'total_tokens': 5}})

    monkeypatch.setattr(provider_module, 'ProviderHub', lambda cfg, *, store: ProviderHub(
        cfg, store=store, client=httpx.MockTransport(endpoint),
    ))
    store = make_store(config)
    headers = {'X-Evidence-Lab-Provider-Keys': json.dumps({'llm': 'client-only-secret'})}
    with TestClient(create_app(config, store=store)) as client:
        response = client.post('/api/documents', headers=headers, files={'file': ('source.txt', b'Browser keys stay private.', 'text/plain')})
        assert response.status_code == 202
        job = store.get_job(response.json()['job_id'])
        assert job['status'] == ('succeeded' if provider_status == 200 else 'failed')
        assert job['payload']['browser_credentials'] is True
        assert 'client-only-secret' not in json.dumps(job)
        assert 'client-only-secret' not in json.dumps(store.get_calls())
        assert 'client-only-secret' not in client.get('/api/status').text
        assert seen == ['/v1/model']
    assert all(profile.api_key is None for profile in config.profiles.values())
