"""Planner contracts: evidence, immutable goals, bounded retries, and no model-owned actions."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import planner
from jev_ultrafast.agent import Agent


def payload():
    return {'task': 'Fill and submit the form', 'requirements': [], 'mode': None, 'current_observation': 'o2',
            'observations': [{'id': 'o2', 'url': 'https://example.test', 'text': 'Form submitted successfully',
                              'fields': [], 'after_action': 3}]}


def result():
    return {'mode': 'form', 'requirements': ['Complete and submit the form'],
            'checks': [{'id': 0, 'met': True, 'evidence': [
                {'observation': 'o2', 'quote': 'Form submitted successfully'}]}],
            'objective': '', 'complete': True, 'blocked': False, 'answer': 'Form submitted successfully.'}


@pytest.mark.parametrize('mutation', ['fake_quote', 'fake_id', 'no_evidence', 'missing_check',
                                     'invalid_field', 'no_actions', 'no_answer', 'contradictory', 'drop_requirement'])
def test_completion_cannot_accept_missing_or_invented_evidence(mutation):
    p, r = payload(), result()
    if mutation == 'fake_quote':
        r['checks'][0]['evidence'][0]['quote'] = 'Flights booked'
    elif mutation == 'fake_id':
        r['checks'][0]['evidence'][0]['observation'] = 'invented'
    elif mutation == 'no_evidence':
        r['checks'][0]['evidence'] = []
    elif mutation == 'missing_check':
        r['checks'] = []
    elif mutation == 'invalid_field':
        p['observations'][0]['fields'] = [{'required': True, 'valid': False, 'label': 'Upload'}]
    elif mutation == 'no_actions':
        p['observations'][0]['after_action'] = 0
    elif mutation == 'no_answer':
        r['answer'] = ''
    elif mutation == 'contradictory':
        r['blocked'] = True
    elif mutation == 'drop_requirement':
        p['requirements'] = ['Complete and submit the form', 'Verify the confirmation']
    with pytest.raises(ValueError):
        planner.validate(r, p)


def test_incomplete_result_preserves_original_requirements():
    p, r = payload(), result()
    p['requirements'] = r['requirements']
    p['mode'] = r['mode']
    r.update(complete=False, objective='Fill the missing upload, then submit')
    r['checks'][0].update(met=False, evidence=[])
    assert planner.validate(r, p)['objective'] == 'Fill the missing upload, then submit'
    r['requirements'] = ['An easier task']
    with pytest.raises(ValueError, match='drop or rewrite'):
        planner.validate(r, p)


def test_initial_existing_research_evidence_can_complete_without_mutation():
    p, r = payload(), result()
    r['mode'] = 'research'
    p['observations'][0]['after_action'] = 0
    assert planner.validate(r, p)['complete']


def test_remember_keeps_previous_source_evidence_and_ignores_pixel_motion():
    state = {'page': {'url': 'https://example.test', 'title': 'Form', 'document_text': 'Evidence',
                      'fields': [{'node': 1, 'value': 'correct', 'rect': {'x': 1}}]}, 'history': []}
    planner.remember(state)
    state['page']['fields'][0]['rect']['x'] = 100
    planner.remember(state)
    assert len(state['observations']) == 1
    state['page']['fields'][0]['value'] = 'changed'
    planner.remember(state)
    assert len(state['observations']) == 2
    assert state['observations'][0]['fields'][0]['value'] == 'correct'


def test_bad_planner_response_gets_one_bounded_retry(monkeypatch):
    state = {'planner_calls': []}
    monkeypatch.setattr(planner, 'context', lambda *_: payload())
    post = Mock(return_value={'choices': [{'message': {'content': 'not JSON'}}]})
    monkeypatch.setattr(planner.model, 'post_json', post)
    monkeypatch.setenv('TEXT_MODEL_API_KEY', 'test')
    with pytest.raises(ValueError, match='twice'):
        planner.checkpoint(state, 'review', 'test-model')
    assert post.call_count == 2 and len(state['planner_calls']) == 2
    assert all('error' in c for c in state['planner_calls'])


def test_planner_total_call_budget_includes_failed_calls(monkeypatch):
    state = {'planner_calls': [{}] * 16}
    monkeypatch.setattr(planner, 'context', lambda *_: payload())
    post = Mock()
    monkeypatch.setattr(planner.model, 'post_json', post)
    with pytest.raises(ValueError, match='16-call'):
        planner.checkpoint(state, 'review', 'test-model')
    post.assert_not_called()


def test_jev_done_returns_to_planner_instead_of_claiming_completion(monkeypatch):
    agent = Agent.__new__(Agent)
    agent.screenshots = False
    browser = Mock()
    page = {'fingerprint': 'observed'}
    agent.state = {'browser': browser, 'page': page, 'decision': {'choice': 'DONE'},
                   'planner_model': 'test', 'status': 'predicted'}
    checkpoint = Mock(side_effect=lambda _: agent.state.update(status='ready'))
    monkeypatch.setattr(agent, 'checkpoint', checkpoint)
    monkeypatch.setattr(agent, 'snapshot', lambda: deepcopy({'status': agent.state['status']}))
    assert agent.command('act', {'fingerprint': 'observed'})['status'] == 'ready'
    checkpoint.assert_called_once_with('Jev requests verification')
    browser.act.assert_not_called()


def test_checkpoint_interval_and_stale_recovery_are_explicit():
    agent = Agent.__new__(Agent)
    agent.state = {'controller': {'objective': 'Finish search'}, 'history': [{}] * 8}
    assert agent.checkpoint_reason() == 'Review progress after eight actions'
    agent.state.update(checkpoint_step=8, stale_count=3)
    assert agent.checkpoint_reason() == 'Three stale decisions without execution'
    agent.state.update(stale_count=0, no_progress=3)
    assert agent.checkpoint_reason() == 'Three actions without observable progress'
