"""Stable archive fragments: readable new boundaries and unchanged old copies."""
from __future__ import annotations

import hashlib
import json
import re

from .render import safe
from .store import ClubError


CHUNK_LIMIT = 1500


def archive_body(snapshot, *, legacy=False):
    body = snapshot['content']
    attachments = [f'Вложение: {safe(a["filename"])}' +
                   ('' if a['copy'] else (' — см. оригинал' if legacy else ' — файл превышает лимит переноса'))
                   for a in snapshot['attachments']]
    if attachments:
        body += ('\n' if body else '') + '\n'.join(attachments)
    return body


def archive_chunks(snapshot, *, legacy=False, fixed=False):
    """Keep separators verbatim; Discord may strip edge whitespace on send.

    Prefer paragraph and then line boundaries, falling back to a word boundary.
    A single token longer than Discord's fragment limit necessarily gets split.
    ``legacy`` is the exact pre-v2 body, including the old attachment caption;
    ``fixed`` retains the released v2 1500-character boundaries.
    """
    body = archive_body(snapshot, legacy=legacy)
    if legacy or fixed:
        return [body[index:index + CHUNK_LIMIT] for index in range(0, len(body), CHUNK_LIMIT)] or ['']
    chunks = []
    while len(body) > CHUNK_LIMIT:
        window = body[:CHUNK_LIMIT]
        end = None
        for pattern in (r'(?:\r?\n){2,}', r'\r?\n', r'\s+'):
            # Do not make a whitespace-only fragment when an indented word
            # fits the next chunk. The retained separators reconstruct exactly.
            boundaries = [match.end() for match in re.finditer(pattern, window)
                          if window[:match.start()].strip()]
            if boundaries:
                end = boundaries[-1]
                break
        end = end or CHUNK_LIMIT
        chunks.append(body[:end])
        body = body[end:]
    if body or not chunks:
        chunks.append(body)
    return chunks


def _validated_plan(row, body, digest):
    if row['body_hash'] != digest or row['scheme'] not in ('fixed-v2', 'words-v3'):
        raise ClubError('Сохранённое разбиение эссе не соответствует исходному тексту. Копии сохранены.')
    try:
        chunks = json.loads(row['chunks'])
    except (TypeError, ValueError) as exc:
        raise ClubError('Сохранённое разбиение эссе повреждено. Копии сохранены.') from exc
    if (not isinstance(chunks, list) or not chunks
            or any(not isinstance(chunk, str) or len(chunk) > CHUNK_LIMIT for chunk in chunks)
            or ''.join(chunks) != body):
        raise ClubError('Сохранённое разбиение эссе повреждено. Копии сохранены.')
    return chunks


def publication_chunks(store, guild_id, item_key, snapshot, *, persist=False):
    """Freeze a source message's parts before sending any of them.

    Existing reservations (including a send with a lost acknowledgement) use the
    historical boundaries. A stored plan is authoritative across restarts and
    later splitter changes. Reading a restyle preview never persists a plan.
    """
    body = archive_body(snapshot)
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    source_id = int(snapshot['id'])
    params = (guild_id, item_key, source_id)
    query = ('SELECT * FROM bc_import_chunk_plans '
             'WHERE guild_id=? AND item_key=? AND source_id=?')
    row = store.one(query, params)
    if row:
        return _validated_plan(row, body, digest)

    prefix = f'{item_key}:message:{source_id}:'
    publication_query = 'SELECT 1 FROM bc_publications WHERE guild_id=? AND substr(key,1,?)=? LIMIT 1'
    publication_params = (guild_id, len(prefix), prefix)
    if not persist:
        fixed = store.one(publication_query, publication_params) is not None
        return archive_chunks(snapshot, fixed=fixed)

    with store.tx() as db:
        # Another attempt may have frozen a plan while this one was checking.
        row = db.execute(query, params).fetchone()
        if row:
            return _validated_plan(row, body, digest)
        fixed = db.execute(publication_query, publication_params).fetchone() is not None
        chunks = archive_chunks(snapshot, fixed=fixed)
        db.execute('''INSERT INTO bc_import_chunk_plans
            (guild_id,item_key,source_id,body_hash,scheme,chunks,created_at)
            VALUES(?,?,?,?,?,?,?)''',
            (*params, digest, 'fixed-v2' if fixed else 'words-v3',
             json.dumps(chunks, ensure_ascii=False), store.clock()))
    return chunks
