# Copyright 2019 SwiftStack
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import eventlet
import pystatsd.statsd


class AtomicStats(object):
    def __init__(self):
        self._semaphore = eventlet.semaphore.Semaphore(1)

    def update(self, **kwargs):
        self._semaphore.acquire()
        self._update_stats(**kwargs)
        self._semaphore.release()


class MigratorPassStats(AtomicStats):
    def __init__(self):
        super(MigratorPassStats, self).__init__()
        self.copied = 0
        self.scanned = 0
        self.bytes_copied = 0

    def _update_stats(self, copied=0, scanned=0, bytes_copied=0):
        self.copied += copied
        self.scanned += scanned
        self.bytes_copied += bytes_copied


class StatsReporter(object):
    def __init__(self, statsd_client, metric_prefix):
        self.statsd_client = statsd_client
        self.metric_prefix = metric_prefix

    def increment(self, metric, count):
        if self.statsd_client:
            stat_name = '.'.join([self.metric_prefix, metric])
            self.statsd_client.update_stats(stat_name, count)

    def timing(self, metric, timing):
        if self.statsd_client:
            stat_name = '.'.join([self.metric_prefix, metric])
            self.statsd_client.timing(stat_name, timing)


class StatsReporterFactory(object):
    def __init__(self, statsd_host, statsd_port, statsd_prefix,
                 handler_class=StatsReporter):
        self._handler_class = handler_class
        if statsd_host:
            self.statsd_client = pystatsd.statsd.Client(
                statsd_host, statsd_port, statsd_prefix
            )
        else:
            self.statsd_client = None

    def __str__(self):
        return 'StatsReporter'

    def instance(self, metric_prefix):
        return self._handler_class(self.statsd_client, metric_prefix)
