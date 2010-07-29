# -*- coding: utf-8 -*-
"""
space_usage_getter.py

A class that performs a space_usage query.
"""
import uuid
from collections import defaultdict

import gevent
from gevent.pool import GreenletSet

from diyapi_web_server.exceptions import (
    AlreadyInProgress,
    SpaceUsageFailedError,
)


class SpaceUsageGetter(object):
    """Performs a space_usage query."""
    def __init__(self, data_readers, agreement_level):
        self.data_readers = data_readers
        self.agreement_level = agreement_level
        self._pending = GreenletSet()
        self._done = []

    def _join(self, timeout):
        self._pending.join(timeout)
        # make sure _done_link gets run first by cooperating
        gevent.sleep(0)
        if len(self._done) < self.agreement_level:
            raise SpaceUsageFailedError()
        self._pending.kill()

    def _done_link(self, task):
        if isinstance(task.value, gevent.GreenletExit):
            return
        if task.successful():
            self._done.append(task)

    def _spawn(self, data_reader, run, *args):
        task = self._pending.spawn(run, *args)
        task.link(self._done_link)
        task.data_reader = data_reader
        return task

    def get_space_usage(self, avatar_id, timeout=None):
        if self._pending:
            raise AlreadyInProgress()
        for data_reader in self.data_readers:
            request_id = uuid.uuid1().hex
            self._spawn(
                data_reader,
                data_reader.get_space_usage,
                request_id,
                avatar_id
            )
        self._join(timeout)
