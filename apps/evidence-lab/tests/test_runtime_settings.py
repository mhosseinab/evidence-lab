"""Browser mode choices remain isolated, explicit, and unqualified."""
import json
from copy import deepcopy

import pytest
from pydantic import SecretStr

from evidence_lab.config import load_config
from evidence_lab.runtime_settings import select_runtime
from evidence_lab.retrieval import space_manifest


@pytest.fixture
def config():
    return load_config('configs/mock.yaml')


@pytest.fixture
def settings():
    return {
        'embedding_endpoint': 'https://service.test/v1/embeddings',
        'embedding_model': 'embed-v1', 'embedding_dimensions': 1536,
        'embedding_max_input_tokens': 8192,
        'chat_endpoint': 'https://service.test/v1/chat/completions',
        'chat_model': 'chat-v1', 'chat_max_input_tokens': 65536,
        'chat_max_output_tokens': 1200,
        'structured_output': 'json_object', 'output_limit_parameter': 'max_tokens',
        'cloudflare_account_id': '0123456789abcdef0123456789abcdef',
        'embedding_input_usd_per_million': 0.02,
        'chat_input_usd_per_million': 0.4, 'chat_output_usd_per_million': 1.6,
    }


def test_no_mode_and_mock_mode_preserve_original_config(config):
    assert select_runtime(config, None, '{invalid') is config
    assert select_runtime(config, 'mock', '{invalid') is config


def test_live_mode_builds_explicit_profiles_with_zero_budget_and_shadow_gate(config, settings):
    selected = select_runtime(config, 'live', json.dumps(settings))
    assert selected.runtime.mode == 'live'
    assert selected.runtime.credentials == 'browser'
    assert selected.runtime.require_openai_compatible is False
    assert set(selected.profiles) == {'byok-embeddings', 'byok-chat', 'byok-clef'}
    assert selected.verification.mode == 'shadow'
    assert selected.verification.policy_id is None
    assert selected.verification.policy_path is None
    assert selected.verification.max_content_repairs == config.verification.max_content_repairs
    assert selected.roles.repair_generator is None
    assert selected.roles.evaluation_judge is None
    assert selected.budgets.total_max_estimated_cost_usd == 0
    assert all(cap == 0 for cap in selected.budgets.phase_max_estimated_cost_usd.values())
    assert all(profile.api_key is None and profile.api_key_env is None and profile.api_key_file is None
               for profile in selected.profiles.values())
    assert all(profile.semantic_revision is None for profile in selected.profiles.values())
    embedding = selected.role_profile('embeddings')
    assert embedding.dimensions == 1536
    assert embedding.batch_size == 16 and embedding.request_dimensions is False
    assert embedding.max_batch_input_tokens == settings['embedding_max_input_tokens']
    chat = selected.role_profile('generator')
    assert chat.capabilities is not None
    assert chat.capabilities.structured_output == 'json_object'
    assert chat.capabilities.output_limit_parameter == 'max_tokens'
    assert chat.capabilities.temperature is False
    clef = selected.role_profile('verifier')
    assert settings['cloudflare_account_id'] in clef.endpoint
    assert clef.model == 'clef' and clef.max_input_tokens == 65536
    assert clef.max_questions == 64 and clef.context_headroom_fraction == 0.25
    assert clef.pricing is not None
    assert clef.pricing.input_usd_per_million == 0.24
    for field in ('database', 'ingestion', 'retrieval', 'memory', 'langsmith'):
        assert getattr(selected, field) == getattr(config, field)


def test_live_selections_isolate_requests_replace_server_keys_and_preserve_space_identity(config, settings):
    config.role_profile('generator').api_key = SecretStr('server-only-secret')
    first = select_runtime(config, 'live', json.dumps(settings))
    second_settings = deepcopy(settings)
    second_settings['budget_usd'] = 3
    second_settings['chat_model'] = 'other-chat'
    second = select_runtime(config, 'live', json.dumps(second_settings))
    assert first.role_profile('embeddings').embedding_space == second.role_profile('embeddings').embedding_space
    second_settings['embedding_model'] = 'other-embed'
    third = select_runtime(config, 'live', json.dumps(second_settings))
    assert first.role_profile('embeddings').embedding_space != third.role_profile('embeddings').embedding_space
    assert first.budgets.total_max_estimated_cost_usd == 0
    assert second.budgets.total_max_estimated_cost_usd == 3
    assert all(cap == 3 for cap in second.budgets.phase_max_estimated_cost_usd.values())
    first.role_profile('generator').api_key = SecretStr('request-only-secret')
    assert second.role_profile('generator').api_key is None
    assert config.role_profile('generator').api_key.get_secret_value() == 'server-only-secret'
    assert config.runtime.mode == 'mock'
    assert 'server-only-secret' not in str(first.safe_dict())


def test_live_to_mock_clears_provider_credentials_and_restores_fixture_gate(config, settings):
    live = select_runtime(config, 'live', json.dumps(settings))
    live.role_profile('generator').api_key = SecretStr('request-only-secret')
    mock = select_runtime(live, 'mock', None)
    assert mock.runtime.mode == 'mock'
    assert mock.runtime.credentials == 'browser'
    assert mock.verification.mode == 'gated'
    assert mock.verification.policy_id == 'mock-fixture-only'
    assert mock.role_profile('generator').api_key is None
    live_key = live.role_profile('generator').api_key
    assert live_key is not None
    assert live_key.get_secret_value() == 'request-only-secret'
    live_space = live.role_profile('embeddings').embedding_space
    mock_space = mock.role_profile('embeddings').embedding_space
    assert mock_space is not None and mock_space.startswith('mock-')
    assert mock_space != live_space
    assert select_runtime(live, 'mock', None).role_profile('embeddings').embedding_space == mock_space
    assert select_runtime(mock, 'mock', None) is mock
    assert all(profile.api_key is None and profile.api_key_env is None and profile.api_key_file is None
               for profile in mock.profiles.values())


@pytest.mark.parametrize('header', [None, '', '{}', '[]', '{invalid', 'x' * 16385])
def test_live_mode_requires_complete_bounded_setup(config, header):
    with pytest.raises(ValueError, match='Complete valid live provider settings'):
        select_runtime(config, 'live', header)


@pytest.mark.parametrize(('field', 'value'), [
    ('embedding_endpoint', 'ftp://secret.test/v1/model'),
    ('chat_endpoint', 'https://secret.test'),
    ('chat_endpoint', 'https://user:secret@service.test/v1/model'),
    ('chat_endpoint', 'https://service.test/v1/model?key=secret'),
    ('embedding_dimensions', 16001), ('embedding_dimensions', 0),
    ('embedding_max_input_tokens', 0), ('chat_max_input_tokens', 0),
    ('chat_max_output_tokens', 65536), ('chat_max_output_tokens', 0),
    ('budget_usd', -1), ('budget_usd', float('inf')),
    ('chat_input_usd_per_million', float('nan')),
    ('chat_output_usd_per_million', -1),
    ('cloudflare_account_id', '../secret'), ('cloudflare_account_id', 'g' * 32),
    ('unknown', 'secret'), ('api_key', 'secret'),
    ('chat_max_input_tokens', '65536'), ('embedding_dimensions', True),
    ('structured_output', 'secret'), ('output_limit_parameter', 'secret'),
])
def test_invalid_settings_fail_with_fixed_safe_error(config, settings, field, value):
    settings[field] = value
    with pytest.raises(ValueError) as error:
        select_runtime(config, 'live', json.dumps(settings))
    assert str(error.value) == 'Complete valid live provider settings in workspace connection.'
    assert 'secret' not in str(error.value)


def test_unknown_mode_has_safe_error(config):
    with pytest.raises(ValueError, match='Choose mock or live mode') as error:
        select_runtime(config, 'private-mode', None)
    assert 'private-mode' not in str(error.value)


def workspace_settings(settings):
    return {key: value for key, value in {**settings, 'embedding_source': 'workspace'}.items()
            if key == 'embedding_source' or not key.startswith('embedding_')}


def test_workspace_fixture_embeddings_preserve_corpus_identity_without_keys(config, settings):
    from evidence_lab.byok import request_config
    selected = select_runtime(config, 'live', json.dumps(workspace_settings(settings)))
    assert selected.runtime.effective_embedding_mode == 'mock'
    assert selected.role_profile('embeddings') == config.role_profile('embeddings')
    assert space_manifest(selected) == space_manifest(config)
    scoped = request_config(selected, json.dumps({'llm': 'llm-secret', 'cloudflare': 'cf-secret'}))
    assert scoped.role_profile('embeddings').api_key is None
    assert config.runtime.mode == 'mock'


def test_custom_embeddings_still_require_explicit_settings(config, settings):
    incomplete = workspace_settings(settings)
    incomplete['embedding_source'] = 'custom'
    with pytest.raises(ValueError, match='Complete valid live provider settings'):
        select_runtime(config, 'live', json.dumps(incomplete))


def test_workspace_live_embeddings_strip_server_key_and_accept_browser_key(config, settings):
    from evidence_lab.byok import request_config
    live = select_runtime(config, 'live', json.dumps(settings))
    live.role_profile('embeddings').api_key = SecretStr('server-only-key')
    selected = select_runtime(live, 'live', json.dumps(workspace_settings(settings)))
    assert selected.runtime.effective_embedding_mode == 'live'
    assert selected.role_profile('embeddings').api_key is None
    assert space_manifest(selected) == space_manifest(live)
    scoped = request_config(selected, json.dumps({'llm': 'browser-key', 'cloudflare': 'cf-key'}))
    key = scoped.role_profile('embeddings').api_key
    assert key is not None and key.get_secret_value() == 'browser-key'


def test_workspace_fixture_to_mock_preserves_existing_fixture_space(config, settings):
    live = select_runtime(config, 'live', json.dumps(workspace_settings(settings)))
    restored = select_runtime(live, 'mock', None)
    assert space_manifest(restored) == space_manifest(config)


def test_workspace_manifest_still_binds_embedding_semantics(config, settings):
    selected = select_runtime(config, 'live', json.dumps(workspace_settings(settings)))
    before = space_manifest(selected)
    for field, value in [('model', 'other-model'), ('dimensions', 63),
                         ('endpoint', 'fixture://different-embeddings')]:
        changed = selected.model_copy(deep=True)
        setattr(changed.role_profile('embeddings'), field, value)
        assert space_manifest(changed)['fingerprint'] != before['fingerprint']


def test_workspace_fixture_browser_config_rejects_server_keys(config, settings):
    from evidence_lab.config import AppConfig
    selected = select_runtime(config, 'live', json.dumps(workspace_settings(settings)))
    data = selected.model_dump()
    data['profiles'][selected.roles.embeddings]['api_key'] = SecretStr('server-secret')
    with pytest.raises(ValueError, match='Browser credentials'):
        AppConfig.model_validate(data)


def test_workspace_status_excludes_fixture_key_and_labels_embedding_mode(config, settings):
    from fastapi.testclient import TestClient
    from evidence_lab.api import create_app
    from test_api import APIStore, NoInferenceHub
    headers = {
        'X-Evidence-Lab-Mode': 'live',
        'X-Evidence-Lab-Live-Settings': json.dumps(workspace_settings(settings)),
    }
    with TestClient(create_app(config, store=APIStore(), hub=NoInferenceHub(), initialize=False)) as client:
        response = client.get('/api/status', headers=headers)
        assert response.status_code == 200
        status = response.json()
        assert status['mode'] == 'live'
        assert status['embedding_mode'] == 'mock'
        assert status['embedding_source'] == 'workspace'
        assert status['embedding_space_matches'] is True
        assert {profile['name'] for profile in status['byok_profiles']} == {'byok-chat', 'byok-clef'}


@pytest.mark.asyncio
async def test_workspace_fixture_embeddings_ignore_live_dispatch_and_need_no_pricing(config, settings):
    import httpx
    from evidence_lab.domain import CallContext
    from evidence_lab.providers import ProviderHub, mock
    from test_providers import Ledger
    selected = select_runtime(config, 'live', json.dumps(workspace_settings(settings)))
    assert selected.role_profile('embeddings').pricing is None

    def forbidden(_request):
        raise AssertionError('Fixture embeddings must not use external HTTP')

    ledger = Ledger()
    hub = ProviderHub(selected, store=ledger, client=httpx.MockTransport(forbidden))
    embedding = selected.role_profile('embeddings')
    assert embedding.dimensions is not None and embedding.model is not None
    try:
        vectors = await hub.embed(['blue widget price'], CallContext.for_seconds('fixture-query', 'queries'))
        assert vectors == mock.embed(['blue widget price'], embedding.dimensions, embedding.model)
        assert ledger.rows[0]['limits']['mock'] is True
        assert ledger.rows[0]['detail']['fixture_only'] is True
    finally:
        await hub.aclose()


@pytest.mark.integration
@pytest.mark.native_postgres
@pytest.mark.asyncio
async def test_workspace_pgvector_reuse_with_fixture_embeddings_and_live_remote_adapters(
    config, settings, isolated_storage_dsn, monkeypatch,
):
    import httpx
    import psycopg
    from evidence_lab.byok import request_config
    from evidence_lab.domain import CallContext
    from evidence_lab.ingestion import ingest_job, pipeline_revision
    from evidence_lab.providers import ProviderHub, mock
    from evidence_lab.retrieval import retrieve_evidence
    from evidence_lab.storage import Store
    from test_providers import chat_result, clef_result

    store = Store(isolated_storage_dsn)
    manifest = space_manifest(config)
    corpus_id = 'workspace-reuse'
    store.ensure_corpus(corpus_id, manifest)
    source = 'The blue widget costs seven dollars.'
    upload = store.create_document('widget.txt', source.encode(), 'text/plain', corpus_id,
                                   pipeline_revision=pipeline_revision(config))
    job = store.claim_job('fixture-ingestion')
    assert job is not None

    def forbidden_embedding_http(_request):
        raise AssertionError('Workspace fixture embeddings must never use HTTP')

    fixture_hub = ProviderHub(config, store=store, client=httpx.MockTransport(forbidden_embedding_http))
    try:
        result = await ingest_job(job, store, fixture_hub, config)
        assert result['status'] == 'ready'
        store.finish_job(job['id'], job['token'], 'succeeded', result=result)
    finally:
        await fixture_hub.aclose()

    def vector_rows():
        with psycopg.connect(isolated_storage_dsn) as connection:
            return connection.execute(
                'SELECT chunk_id,space_id,embedding::text FROM evidence_chunk_embeddings ORDER BY chunk_id',
            ).fetchall()

    before_vectors = vector_rows()
    assert before_vectors
    before_corpus = store.get_corpus(corpus_id)
    live_settings = workspace_settings(settings)
    live_settings['budget_usd'] = 1
    selected = request_config(select_runtime(config, 'live', json.dumps(live_settings)),
                              json.dumps({'llm': 'browser-llm-secret', 'cloudflare': 'browser-cf-secret'}))
    assert space_manifest(selected) == manifest
    expected_profile = config.role_profile('embeddings')
    assert expected_profile.model is not None and expected_profile.dimensions is not None
    question = 'What does the blue widget cost?'
    expected_query = mock.embed([question], expected_profile.dimensions, expected_profile.model)
    original_embed = mock.embed
    embedded = []

    def capture_embed(texts, dimensions, model):
        vectors = original_embed(texts, dimensions, model)
        embedded.append((texts, vectors))
        return vectors

    monkeypatch.setattr(mock, 'embed', capture_embed)
    remote_paths = []
    chunk_id = before_vectors[0][0]

    def remote(request):
        remote_paths.append(request.url.path)
        if request.url.path == '/v1/chat/completions':
            assert request.headers['authorization'] == 'Bearer browser-llm-secret'
            return httpx.Response(200, json=chat_result({'blocks': [
                {'block_id': 'b1', 'text': source, 'citation_ids': [chunk_id]},
            ]}))
        if request.url.path.endswith('/ai/run/@cf/cloudflare/clef'):
            assert request.headers['authorization'] == 'Bearer browser-cf-secret'
            return httpx.Response(200, json=clef_result())
        raise AssertionError('No embedding endpoint may be called')

    live_hub = ProviderHub(selected, store=store, client=httpx.MockTransport(remote))
    ctx = CallContext.for_seconds('workspace-query', 'queries', seconds=30)
    try:
        evidence = await retrieve_evidence(question, corpus_id, store, live_hub, selected, ctx)
        assert evidence.items and evidence.items[0].version_id == upload['version_id']
        assert embedded == [([question], expected_query)]
        draft = await live_hub.generate(question, evidence, ctx)
        verdict = await live_hub.verify(question, draft, evidence, ctx)
        assert verdict.execution_status == 'ok'
        assert verdict.answer_hash == draft.content_hash
    finally:
        await live_hub.aclose()
    assert remote_paths == ['/v1/chat/completions',
                            '/client/v4/accounts/0123456789abcdef0123456789abcdef/ai/run/@cf/cloudflare/clef']
    assert vector_rows() == before_vectors
    assert store.get_corpus(corpus_id) == before_corpus
    calls = store.get_calls('workspace-query')
    embedding_calls = [call for call in calls if call['profile'] == selected.roles.embeddings]
    assert len(embedding_calls) == 1
    assert embedding_calls[0]['detail']['fixture_only'] is True
    assert embedding_calls[0]['detail']['mode'] == 'mock'
    assert 'browser-llm-secret' not in json.dumps(calls)
    assert 'browser-cf-secret' not in json.dumps(calls)
