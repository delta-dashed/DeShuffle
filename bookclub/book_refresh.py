"""Prioritize saved book edits without concurrent Discord publication writers."""
from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
from time import perf_counter

log = logging.getLogger(__name__)


class BookRefreshQueue:
    def __init__(self, service):
        self.service = service
        self.pending = defaultdict(dict)
        self.tasks = {}
        self.draining = set()
        self.failures = {}
        self.closed = False

    def request(self, guild, book_id):
        if self.closed:
            return  # The committed SQLite change will be reconciled on startup.
        self.pending[guild.id].setdefault(book_id, perf_counter())
        task = self.tasks.get(guild.id)
        if task is None or task.done():
            self.tasks[guild.id] = asyncio.create_task(self._worker(guild), name=f'book-refresh:{guild.id}')

    async def _worker(self, guild):
        try:
            async with self.service.locks[guild.id]:
                await self.drain(guild)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failures[guild.id, 'catalog'] = type(exc).__name__
            log.warning('Book update queue deferred guild=%s error=%s', guild.id, type(exc).__name__)
        finally:
            self.tasks.pop(guild.id, None)

    async def drain(self, guild):
        """Caller owns the guild lock; safe checkpoints let us overtake a scan.

        Never call from inside upsert, where an intent can still await its POST.
        Repeated saves are coalesced, and a save during I/O schedules another pass.
        """
        if guild.id in self.draining or not self.pending.get(guild.id):
            return
        if ((self.service.guild_ids is not None and guild.id not in self.service.guild_ids)
                or not self.service.store.settings(guild.id)['published']):
            self.pending.pop(guild.id, None)
            return
        self.draining.add(guild.id)
        try:
            while self.pending.get(guild.id):
                batch = self.pending.pop(guild.id)
                for book_id, requested in batch.items():
                    try:
                        await self.service.refresh_book(guild, book_id)
                        self.failures.pop((guild.id, book_id), None)
                        log.info('Book card updated guild=%s book=%s elapsed_ms=%.1f',
                                 guild.id, book_id, (perf_counter() - requested) * 1000)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Keep private text out of logs; ordinary reconciliation
                        # retries the committed state without replaying the edit.
                        self.failures[guild.id, book_id] = type(exc).__name__
                        log.warning('Book card update deferred guild=%s book=%s error=%s',
                                    guild.id, book_id, type(exc).__name__)
                try:
                    await self.service.refresh_catalog(guild)
                    self.failures.pop((guild.id, 'catalog'), None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.failures[guild.id, 'catalog'] = type(exc).__name__
                    log.warning('Book catalog update deferred guild=%s error=%s', guild.id, type(exc).__name__)
        finally:
            self.draining.discard(guild.id)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.pending.clear()
