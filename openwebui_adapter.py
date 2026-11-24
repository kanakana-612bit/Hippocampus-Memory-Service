"""Pure OpenWebUI dialogue and budget adapters; no private host configuration."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from context_compiler import AUDIT, clean_display, Budget, estimate_tokens


def text_content(message: dict, *, for_storage: bool = False) -> str:
    value = message.get('content', '')
    if isinstance(value, list):
        value = '\n'.join(str(p.get('text', '')) for p in value if isinstance(p, dict) and p.get('type') in ('text', 'output_text'))
    text = str(value or '')
    return AUDIT.sub('', text).strip() if for_storage else clean_display(text)


def event_id(identity: str, content: str) -> str:
    return identity + ':v:' + hashlib.sha256(content.encode()).hexdigest()[:16]


def dialogue_payload(chat_id: str, chat: dict, user_id: str | None, *, logical_actor_id: str,
                     active_leaf: str | None = None) -> dict:
    history = chat.get('history') or {}
    messages = history.get('messages') or {}
    leaf = active_leaf or history.get('currentId')
    active = []
    visited = set()
    while leaf:
        if leaf in visited or leaf not in messages:
            raise ValueError('Incomplete or cyclic frontend dialogue path')
        visited.add(leaf)
        active.append(leaf)
        leaf = messages[leaf].get('parentId')
    active.reverse()
    records, ids = [], {}
    for identity, item in messages.items():
        role = item.get('role')
        if role not in ('user', 'assistant', 'tool') or (role == 'assistant' and item.get('done') is False):
            continue
        gate = item.get('attributionGate') or {}
        if gate.get('status') == 'failed' or gate.get('decision') in ('validation_failed', 'gate_error'):
            continue
        content = text_content(item, for_storage=True)
        if not content:
            continue
        version = event_id(identity, content)
        ids[identity] = version
        model = item.get('model') or item.get('selectedModelId')
        if isinstance(model, dict):
            model = model.get('id')
        meta = {'source_message_id': identity, 'path_state': 'branch',
                'logical_actor_id': logical_actor_id if role == 'assistant' else user_id if role == 'user' else None,
                'model_provenance': {'model_id': model, 'provider': item.get('provider'), 'revision': item.get('model_revision')} if role == 'assistant' else {}}
        event = {'id': version, 'role': role, 'content': content,
                 'actor_role': role, 'actor_id': user_id if role == 'user' else None,
                 'source_channel': 'openwebui_hook', 'content_origin': 'generated' if role == 'assistant' else 'original', 'meta': meta}
        stamp = item.get('timestamp') or item.get('event_time')
        if isinstance(stamp, (int, float)):
            stamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        if stamp:
            event.update(event_time=stamp, source_time=stamp, time_source='openwebui_message')
        records.append(event)
    ordered = [ids[i] for i in active if i in ids]
    return {'conversation_id': chat_id, 'messages': records, 'active_event_ids': ordered,
            'auto_capture': True}


def input_tokens(form_data: dict) -> int:
    values = {'messages': form_data.get('messages', []), 'tools': form_data.get('tools', []),
              'response_format': form_data.get('response_format')}
    return estimate_tokens(json.dumps(values, ensure_ascii=False, separators=(',', ':')))


def audit_candidates(chat_id: str, chat: dict) -> list[dict]:
    result = []
    for identity, message in ((chat.get('history') or {}).get('messages') or {}).items():
        for i, candidate in enumerate((message.get('attributionGate') or {}).get('rejected_candidates') or []):
            if not isinstance(candidate, dict) or not candidate.get('content'):
                continue
            digest = hashlib.sha256(str(candidate['content']).encode()).hexdigest()
            result.append({'conversation_id': chat_id, 'candidate_id': f'candidate_audit:{identity}:{i}:{digest[:16]}',
                           'content': str(candidate['content']),
                           'metadata': {**{k: v for k, v in candidate.items() if k != 'content'},
                                        'source_message_id': identity, 'record_kind': 'generation_candidate'}})
    return result


def enforce_budget(form_data: dict, injected_context: str = '', budget: dict | None = None) -> dict:
    settings = Budget.load(budget)
    requested = (form_data.get('params') or {}).get('max_tokens') or form_data.get('max_tokens')
    if requested:
        settings.reserved_generation_tokens = max(settings.reserved_generation_tokens, int(requested))
    else:
        form_data['max_tokens'] = settings.reserved_generation_tokens
    maximum = max(0, settings.runtime_context_limit - settings.reserved_generation_tokens - settings.template_overhead_reserve)
    messages = form_data.get('messages') or []
    removed_context = False
    if input_tokens(form_data) > maximum and injected_context:
        for message in messages:
            if message.get('role') == 'system' and isinstance(message.get('content'), str):
                message['content'] = message['content'].replace(injected_context, '').strip()
        removed_context = True
    dropped_turns = 0
    # Remove complete old user-led turns so tool results never lose their call.
    while input_tokens(form_data) > maximum:
        users = [i for i, m in enumerate(messages) if m.get('role') == 'user']
        if len(users) < 2:
            break
        start, end = users[0], users[1]
        messages[start:end] = [m for m in messages[start:end] if m.get('role') == 'system']
        dropped_turns += 1
    total = input_tokens(form_data) + settings.reserved_generation_tokens + settings.template_overhead_reserve
    return {'estimator': 'utf8_bytes_upper_bound', 'estimated_total_tokens': total,
            'runtime_context_limit': settings.runtime_context_limit,
            'template_overhead_reserve': settings.template_overhead_reserve,
            'reserved_generation_tokens': settings.reserved_generation_tokens,
            'removed_context': removed_context, 'dropped_old_turns': dropped_turns,
            'input_over_budget': total > settings.runtime_context_limit}
