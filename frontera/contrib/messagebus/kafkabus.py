# -*- coding: utf-8 -*-
from __future__ import absolute_import

from logging import getLogger
from os.path import join as os_path_join

import six
from kafka import KafkaConsumer, KafkaProducer, TopicPartition

from frontera.contrib.backends.partitioners import FingerprintPartitioner, Crc32NamePartitioner
from frontera.contrib.messagebus.kafka.offsets_fetcher import OffsetsFetcherAsync
from frontera.core.messagebus import BaseMessageBus, BaseSpiderLogStream, BaseSpiderFeedStream, \
    BaseStreamConsumer, BaseScoringLogStream, BaseStreamProducer, BaseStatsLogStream

DEFAULT_BATCH_SIZE = 1024 * 1024
DEFAULT_BUFFER_MEMORY = 130 * 1024 * 1024
DEFAULT_MAX_REQUEST_SIZE = 4 * 1024 * 1024

logger = getLogger("messagebus.kafka")


def _prepare_kafka_ssl_kwargs(cert_path):
    """Prepare SSL kwargs for Kafka producer/consumer."""
    return {
        'security_protocol': 'SSL',
        'ssl_cafile': os_path_join(cert_path, 'ca-cert.pem'),
        'ssl_certfile': os_path_join(cert_path, 'client-cert.pem'),
        'ssl_keyfile': os_path_join(cert_path, 'client-key.pem')
    }


def _prepare_kafka_sasl_kwargs(sasl_username, sasl_password):
    """Prepare SASL kwargs for Kafka producer/consumer."""
    return {
        'security_protocol': 'SASL_SSL',
        'sasl_mechanism': 'SCRAM-SHA-512',
        'sasl_plain_username': sasl_username,
        'sasl_plain_password': sasl_password
    }


class Consumer(BaseStreamConsumer):
    """
    Used in DB and SW worker. SW consumes per partition.
    """

    def __init__(self, location, enable_ssl, cert_path, topic, group, partition_id,
                 enable_sasl=False, sasl_username='', sasl_password=''):
        self._location = location
        self._group = group
        self._topic = topic
        kwargs = {}
        if enable_ssl:
            kwargs = _prepare_kafka_ssl_kwargs(cert_path)
        elif enable_sasl:
            kwargs = _prepare_kafka_sasl_kwargs(sasl_username, sasl_password)
        self._consumer = KafkaConsumer(
            bootstrap_servers=self._location,
            group_id=self._group,
            max_partition_fetch_bytes=10485760,
            consumer_timeout_ms=100,
            client_id="%s-%s" % (self._topic, str(partition_id) if partition_id is not None else "all"),
            request_timeout_ms=120 * 1000,
            heartbeat_interval_ms=10000,
            **kwargs
        )

        # explicitly causing consumer to bootstrap the cluster metadata
        self._consumer.topics()

        if partition_id is not None:
            self._partitions = [TopicPartition(self._topic, partition_id)]
            self._consumer.assign(self._partitions)
        else:
            self._partitions = [TopicPartition(self._topic, pid) for pid in
                                self._consumer.partitions_for_topic(self._topic)]
            self._consumer.subscribe(topics=[self._topic])

    def get_messages(self, timeout=0.1, count=1):
        result = []
        while count > 0:
            try:
                m = next(self._consumer)
                result.append(m.value)
                count -= 1
            except StopIteration:
                break
        return result

    def get_offset(self, partition_id):
        for tp in self._partitions:
            if tp.partition == partition_id:
                return self._consumer.position(tp)
        raise KeyError("Can't find partition %d", partition_id)

    def close(self):
        self._consumer.commit()
        self._consumer.close()


class SimpleProducer(BaseStreamProducer):
    def __init__(self, location, enable_ssl, cert_path, topic, compression,
                 enable_sasl=False, sasl_username='', sasl_password='',
                 **kwargs):
        self._location = location
        self._topic = topic
        self._compression = compression
        if enable_ssl:
            kwargs.update(_prepare_kafka_ssl_kwargs(cert_path))
        elif enable_sasl:
            kwargs.update(_prepare_kafka_sasl_kwargs(sasl_username, sasl_password))
        self._create(**kwargs)

    def _create(self, **kwargs):
        max_request_size = kwargs.pop('max_request_size', DEFAULT_MAX_REQUEST_SIZE)
        self._producer = KafkaProducer(bootstrap_servers=self._location,
                                       retries=5,
                                       compression_type=self._compression,
                                       max_request_size=max_request_size,
                                       **kwargs)

    def send(self, key, *messages):
        for msg in messages:
            self._producer.send(self._topic, value=msg)

    def flush(self):
        self._producer.flush()

    def close(self):
        self._producer.close()


class KeyedProducer(BaseStreamProducer):
    def __init__(self, location, enable_ssl, cert_path, topic_done, partitioner, compression,
                 enable_sasl=False, sasl_username='', sasl_password='',
                 **kwargs):
        self._location = location
        self._topic_done = topic_done
        self._partitioner = partitioner
        self._compression = compression
        max_request_size = kwargs.pop('max_request_size', DEFAULT_MAX_REQUEST_SIZE)
        if enable_ssl:
            kwargs.update(_prepare_kafka_ssl_kwargs(cert_path))
        elif enable_sasl:
            kwargs.update(_prepare_kafka_sasl_kwargs(sasl_username, sasl_password))
        self._producer = KafkaProducer(bootstrap_servers=self._location,
                                       partitioner=partitioner,
                                       retries=5,
                                       compression_type=self._compression,
                                       max_request_size=max_request_size,
                                       **kwargs)

    def send(self, key, *messages):
        for msg in messages:
            self._producer.send(self._topic_done, key=key, value=msg)

    def flush(self):
        self._producer.flush()

    def get_offset(self, partition_id):
        pass


class SpiderLogStream(BaseSpiderLogStream):
    def __init__(self, messagebus):
        self._location = messagebus.kafka_location
        self._db_group = messagebus.spiderlog_dbw_group
        self._sw_group = messagebus.spiderlog_sw_group
        self._topic = messagebus.topic_done
        self._codec = messagebus.codec
        self._partitions = messagebus.spider_log_partitions
        self._enable_ssl = messagebus.enable_ssl
        self._cert_path = messagebus.cert_path
        self._enable_sasl = messagebus.enable_sasl
        self._sasl_username = messagebus.sasl_username
        self._sasl_password = messagebus.sasl_password
        self._kafka_max_block_ms = messagebus.kafka_max_block_ms

    def producer(self):
        return KeyedProducer(self._location, self._enable_ssl, self._cert_path, self._topic,
                             FingerprintPartitioner(self._partitions), self._codec,
                             batch_size=DEFAULT_BATCH_SIZE,
                             buffer_memory=DEFAULT_BUFFER_MEMORY,
                             max_block_ms=self._kafka_max_block_ms,
                             enable_sasl=self._enable_sasl,
                             sasl_username=self._sasl_username,
                             sasl_password=self._sasl_password)

    def consumer(self, partition_id, type):
        """
        Creates spider log consumer with BaseStreamConsumer interface
        :param partition_id: can be None or integer
        :param type: either 'db' or 'sw'
        :return:
        """
        group = self._sw_group if type == b'sw' else self._db_group
        c = Consumer(self._location, self._enable_ssl, self._cert_path, self._topic, group, partition_id,
                     enable_sasl=self._enable_sasl, sasl_username=self._sasl_username,
                     sasl_password=self._sasl_password)
        assert len(c._consumer.partitions_for_topic(self._topic)) == self._partitions
        return c


class SpiderFeedStream(BaseSpiderFeedStream):
    def __init__(self, messagebus):
        self._location = messagebus.kafka_location
        self._general_group = messagebus.spider_feed_group
        self._topic = messagebus.topic_todo
        self._max_next_requests = messagebus.max_next_requests
        self._hostname_partitioning = messagebus.hostname_partitioning
        self._enable_ssl = messagebus.enable_ssl
        self._cert_path = messagebus.cert_path
        self._enable_sasl = messagebus.enable_sasl
        self._sasl_username = messagebus.sasl_username
        self._sasl_password = messagebus.sasl_password
        kwargs = {
            'bootstrap_servers': self._location,
            'topic': self._topic,
            'group_id': self._general_group,
        }
        if self._enable_ssl:
            kwargs.update(_prepare_kafka_ssl_kwargs(self._cert_path))
        elif self._enable_sasl:
            kwargs.update(_prepare_kafka_sasl_kwargs(self._sasl_username, self._sasl_password))
        self._offset_fetcher = OffsetsFetcherAsync(**kwargs)
        self._codec = messagebus.codec
        self._partitions = messagebus.spider_feed_partitions
        self._kafka_max_block_ms = messagebus.kafka_max_block_ms

    def consumer(self, partition_id):
        c = Consumer(self._location, self._enable_ssl, self._cert_path, self._topic, self._general_group,
                     partition_id,
                     enable_sasl=self._enable_sasl, sasl_username=self._sasl_username,
                     sasl_password=self._sasl_password)
        assert len(c._consumer.partitions_for_topic(self._topic)) == self._partitions, \
            "Number of kafka topic partitions doesn't match value in config for spider feed"
        return c

    def available_partitions(self):
        partitions = []
        lags = self._offset_fetcher.get()
        for partition, lag in six.iteritems(lags):
            if lag < self._max_next_requests:
                partitions.append(partition)
        return partitions

    def producer(self):
        partitioner = Crc32NamePartitioner(self._partitions) if self._hostname_partitioning \
            else FingerprintPartitioner(self._partitions)
        return KeyedProducer(self._location, self._enable_ssl, self._cert_path, self._topic, partitioner,
                             self._codec,
                             batch_size=DEFAULT_BATCH_SIZE,
                             buffer_memory=DEFAULT_BUFFER_MEMORY,
                             max_block_ms=self._kafka_max_block_ms,
                             enable_sasl=self._enable_sasl, sasl_username=self._sasl_username,
                             sasl_password=self._sasl_password)


class ScoringLogStream(BaseScoringLogStream):
    def __init__(self, messagebus):
        self._topic = messagebus.topic_scoring
        self._group = messagebus.scoringlog_dbw_group
        self._location = messagebus.kafka_location
        self._codec = messagebus.codec
        self._cert_path = messagebus.cert_path
        self._enable_ssl = messagebus.enable_ssl
        self._enable_sasl = messagebus.enable_sasl
        self._sasl_username = messagebus.sasl_username
        self._sasl_password = messagebus.sasl_password
        self._kafka_max_block_ms = messagebus.kafka_max_block_ms

    def consumer(self):
        return Consumer(self._location, self._enable_ssl, self._cert_path, self._topic, self._group,
                        partition_id=None,
                        enable_sasl=self._enable_sasl, sasl_username=self._sasl_username,
                        sasl_password=self._sasl_password)

    def producer(self):
        return SimpleProducer(self._location, self._enable_ssl, self._cert_path, self._topic, self._codec,
                              batch_size=DEFAULT_BATCH_SIZE,
                              buffer_memory=DEFAULT_BUFFER_MEMORY,
                              max_block_ms=self._kafka_max_block_ms,
                              enable_sasl=self._enable_sasl, sasl_username=self._sasl_username,
                              sasl_password=self._sasl_password)


class StatsLogStream(ScoringLogStream, BaseStatsLogStream):
    """Stats log stream implementation for Kafka message bus.

    The interface is the same as for scoring log stream, so it's better
    to reuse it with proper topic and group.
    """

    def __init__(self, messagebus):
        super(StatsLogStream, self).__init__(messagebus)
        self._topic = messagebus.topic_stats
        self._group = messagebus.statslog_reader_group


class MessageBus(BaseMessageBus):
    def __init__(self, settings):
        self.topic_todo = settings.get('SPIDER_FEED_TOPIC')
        self.topic_done = settings.get('SPIDER_LOG_TOPIC')
        self.topic_scoring = settings.get('SCORING_LOG_TOPIC')
        self.topic_stats = settings.get('STATS_LOG_TOPIC')

        self.spiderlog_dbw_group = settings.get('SPIDER_LOG_DBW_GROUP')
        self.spiderlog_sw_group = settings.get('SPIDER_LOG_SW_GROUP')
        self.scoringlog_dbw_group = settings.get('SCORING_LOG_DBW_GROUP')
        self.statslog_reader_group = settings.get('STATS_LOG_READER_GROUP')
        self.spider_feed_group = settings.get('SPIDER_FEED_GROUP')
        self.spider_partition_id = settings.get('SPIDER_PARTITION_ID')
        self.max_next_requests = settings.MAX_NEXT_REQUESTS
        self.hostname_partitioning = settings.get('QUEUE_HOSTNAME_PARTITIONING')
        self.codec = settings.get('KAFKA_CODEC')
        self.kafka_location = settings.get('KAFKA_LOCATION')
        self.enable_ssl = settings.get('KAFKA_ENABLE_SSL')
        self.cert_path = settings.get('KAFKA_CERT_PATH')
        self.enable_sasl = settings.get('KAFKA_ENABLE_SASL')
        self.sasl_username = settings.get('KAFKA_SASL_USERNAME')
        self.sasl_password = settings.get('KAFKA_SASL_PASSWORD')
        self.spider_log_partitions = settings.get('SPIDER_LOG_PARTITIONS')
        self.spider_feed_partitions = settings.get('SPIDER_FEED_PARTITIONS')
        self.kafka_max_block_ms = settings.get('KAFKA_MAX_BLOCK_MS')

    def spider_log(self):
        return SpiderLogStream(self)

    def spider_feed(self):
        return SpiderFeedStream(self)

    def scoring_log(self):
        return ScoringLogStream(self)

    def stats_log(self):
        return StatsLogStream(self)
