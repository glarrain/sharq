# -*- coding: utf-8 -*-
# Copyright (c) 2014 Plivo Team. See LICENSE.txt for details.
import os
import sys
import signal
import ConfigParser
import redis
from sharq.utils import (is_valid_identifier, is_valid_interval,
                         serialize_payload, deserialize_payload,
                         generate_epoch)
from sharq.exceptions import SharqException, BadArgumentException


class SharQ(object):
    """The SharQ object is the core of this queue.
    SharQ does the following.

        1. Accepts a configuration file.
        2. Initializes the queue.
        3. Exposes functions to interact with the queue.
    """

    def __init__(self, config_path):
        """Construct a SharQ object by doing the following.
            1. Read the configuration path.
            2. Load the config.
            3. Initialized SharQ.
        """
        self.config_path = config_path
        self._load_config()
        self._initialize()

    def _initialize(self):
        """Read the SharQ configuration and set appropriate
        variables. Open a redis connection pool and load all
        the Lua scripts.
        """
        self._key_prefix = self._config.get('redis', 'key_prefix')
        self._job_expire_interval = int(
            self._config.get('sharq', 'job_expire_interval'))

        # initalize redis
        redis_connection_type = self._config.get('redis', 'conn_type')
        db = self._config.get('redis', 'db')
        if redis_connection_type == 'unix_sock':
            self._r = redis.StrictRedis(
                db=db,
                unix_socket_path=self._config.get('redis', 'unix_socket_path')
            )
        elif redis_connection_type == 'tcp_sock':
            self._r = redis.StrictRedis(
                db=db,
                host=self._config.get('redis', 'host'),
                port=self._config.get('redis', 'port')
            )

        self._load_lua_scripts()

    def _load_config(self):
        """Read the configuration file and load it into memory."""
        self._config = ConfigParser.SafeConfigParser()
        self._config.read(self.config_path)

    def reload_config(self, config_path=None):
        """Reload the configuration from the new config file if provided
        else reload the current config file.
        """
        if config_path:
            self.config_path = config_path
        self._load_config()

    def _load_lua_scripts(self):
        """Loads all lua scripts required by SharQ."""
        # load lua scripts
        lua_script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'scripts/lua'
        )
        with open(os.path.join(
                lua_script_path,
                'enqueue.lua'), 'r') as enqueue_file:
            self._lua_enqueue_script = enqueue_file.read()
            self._lua_enqueue = self._r.register_script(
                self._lua_enqueue_script)

        with open(os.path.join(
                lua_script_path,
                'dequeue.lua'), 'r') as dequeue_file:
            self._lua_dequeue_script = dequeue_file.read()
            self._lua_dequeue = self._r.register_script(
                self._lua_dequeue_script)

        with open(os.path.join(
                lua_script_path,
                'finish.lua'), 'r') as finish_file:
            self._lua_finish_script = finish_file.read()
            self._lua_finish = self._r.register_script(self._lua_finish_script)

        with open(os.path.join(
                lua_script_path,
                'interval.lua'), 'r') as interval_file:
            self._lua_interval_script = interval_file.read()
            self._lua_interval = self._r.register_script(
                self._lua_interval_script)

        with open(os.path.join(
                lua_script_path,
                'requeue.lua'), 'r') as requeue_file:
            self._lua_requeue_script = requeue_file.read()
            self._lua_requeue = self._r.register_script(
                self._lua_requeue_script)

        with open(os.path.join(
                lua_script_path,
                'metrics.lua'), 'r') as metrics_file:
            self._lua_metrics_script = metrics_file.read()
            self._lua_metrics = self._r.register_script(
                self._lua_metrics_script)

    def reload_lua_scripts(self):
        """Lets user reload the lua scripts in run time."""
        self._load_lua_scripts()

    def enqueue(self, payload, interval, job_id,
                queue_id, queue_type='default'):
        """Enqueues the job into the specified queue_id
        of a particular queue_type
        """
        # validate all the input
        if not is_valid_interval(interval):
            raise BadArgumentException('`interval` has an invalid value.')

        if not is_valid_identifier(job_id):
            raise BadArgumentException('`job_id` has an invalid value.')

        if not is_valid_identifier(queue_id):
            raise BadArgumentException('`queue_id` has an invalid value.')

        if not is_valid_identifier(queue_type):
            raise BadArgumentException('`queue_type` has an invalid value.')

        try:
            serialized_payload = serialize_payload(payload)
        except TypeError as e:
            raise BadArgumentException(e.message)

        timestamp = str(generate_epoch())

        keys = [
            self._key_prefix,
            queue_type
        ]

        args = [
            timestamp,
            queue_id,
            job_id,
            '"%s"' % serialized_payload,
            interval
        ]

        self._lua_enqueue(keys=keys, args=args)

        response = {
            'status': 'queued'
        }
        return response

    def dequeue(self, queue_type='default'):
        """Dequeues a job from any of the ready queues
        based on the queue_type. If no job is ready,
        returns a failure status.
        """
        if not is_valid_identifier(queue_type):
            raise BadArgumentException('`queue_type` has an invalid value.')

        timestamp = str(generate_epoch())

        keys = [
            self._key_prefix,
            queue_type
        ]
        args = [
            timestamp,
            self._job_expire_interval
        ]

        dequeue_response = self._lua_dequeue(keys=keys, args=args)

        if len(dequeue_response) < 3:
            response = {
                'status': 'failure'
            }
            return response

        queue_id, job_id, payload = dequeue_response
        payload = deserialize_payload(payload[1:-1])

        response = {
            'status': 'success',
            'queue_id': queue_id,
            'job_id': job_id,
            'payload': payload
        }

        return response

    def finish(self, job_id, queue_id, queue_type='default'):
        """Marks any dequeued job as *completed successfully*.
        Any job which gets a finish will be treated as complete
        and will be removed from the SharQ.
        """
        if not is_valid_identifier(job_id):
            raise BadArgumentException('`job_id` has an invalid value.')

        if not is_valid_identifier(queue_id):
            raise BadArgumentException('`queue_id` has an invalid value.')

        if not is_valid_identifier(queue_type):
            raise BadArgumentException('`queue_type` has an invalid value.')

        keys = [
            self._key_prefix,
            queue_type
        ]

        args = [
            queue_id,
            job_id
        ]

        response = {
            'status': 'success'
        }

        finish_response = self._lua_finish(keys=keys, args=args)
        if finish_response == 0:
            # the finish failed.
            response.update({
                'status': 'failure'
            })

        return response

    def interval(self, interval, queue_id, queue_type='default'):
        """Updates the interval for a specific queue_id
        of a particular queue type.
        """
        # validate all the input
        if not is_valid_interval(interval):
            raise BadArgumentException('`interval` has an invalid value.')

        if not is_valid_identifier(queue_id):
            raise BadArgumentException('`queue_id` has an invalid value.')

        if not is_valid_identifier(queue_type):
            raise BadArgumentException('`queue_type` has an invalid value.')

        # generate the interval key
        interval_hmap_key = '%s:interval' % self._key_prefix
        interval_queue_key = '%s:%s' % (queue_type, queue_id)
        keys = [
            interval_hmap_key,
            interval_queue_key
        ]

        args = [
            interval
        ]
        interval_response = self._lua_interval(keys=keys, args=args)
        if interval_response == 0:
            # the queue with the id and type does not exist.
            response = {
                'status': 'failure'
            }
        else:
            response = {
                'status': 'success'
            }

        return response

    def requeue(self):
        """Re-queues any expired job (one which does not get an expire
        before the job_expiry_interval) back into their respective queue.
        This function has to be run at specified intervals to ensure the
        expired jobs are re-queued back.
        """
        timestamp = str(generate_epoch())
        # get all queue_types and requeue one by one.
        # not recommended to do this entire process
        # in lua as it might take long and block other
        # enqueues and dequeues.
        active_queue_type_list = self._r.smembers(
            '%s:active:queue_type' % self._key_prefix)
        for queue_type in active_queue_type_list:
            # requeue all expired jobs in all queue types.
            keys = [
                self._key_prefix,
                queue_type
            ]

            args = [
                timestamp
            ]
            self._lua_requeue(keys=keys, args=args)

    def metrics(self, queue_type=None, queue_id=None):
        """Provides a way to get statistics about various parameters like,
        * global enqueue / dequeue rates per min.
        * per queue enqueue / dequeue rates per min.
        * queue length of each queue.
        * list of queue ids for each queue type.
        """
        if queue_id is not None and not is_valid_identifier(queue_id):
            raise BadArgumentException('`queue_id` has an invalid value.')

        if queue_type is not None and not is_valid_identifier(queue_type):
            raise BadArgumentException('`queue_type` has an invalid value.')

        response = {
            'status': 'failure'
        }
        if not queue_type and not queue_id:
            # return global stats.
            # list of active queue types (ready + active)
            active_queue_types = self._r.smembers(
                '%s:active:queue_type' % self._key_prefix)
            ready_queue_types = self._r.smembers(
                '%s:ready:queue_type' % self._key_prefix)
            all_queue_types = active_queue_types | ready_queue_types
            # global rates for past 10 minutes
            timestamp = str(generate_epoch())
            keys = [
                self._key_prefix
            ]
            args = [
                timestamp
            ]

            enqueue_details, dequeue_details = self._lua_metrics(
                keys=keys, args=args)

            enqueue_counts = {}
            dequeue_counts = {}
            # the length of enqueue & dequeue details are always same.
            for i in xrange(0, len(enqueue_details), 2):
                enqueue_counts[str(enqueue_details[i])] = int(
                    enqueue_details[i + 1] or 0)
                dequeue_counts[str(dequeue_details[i])] = int(
                    dequeue_details[i + 1] or 0)

            response.update({
                'status': 'success',
                'queue_types': list(all_queue_types),
                'enqueue_counts': enqueue_counts,
                'dequeue_counts': dequeue_counts
            })
            return response
        elif queue_type and not queue_id:
            # return list of queue_ids.
            # get data from two sorted sets in a transaction
            pipe = self._r.pipeline()
            pipe.zrange('%s:%s' % (self._key_prefix, queue_type), 0, -1)
            pipe.zrange('%s:%s:active' % (self._key_prefix, queue_type), 0, -1)
            ready_queues, active_queues = pipe.execute()
            # extract the queue_ids from the queue_id:job_id string
            active_queues = [i.split(':')[0] for i in active_queues]
            all_queue_set = set(ready_queues) | set(active_queues)
            response.update({
                'status': 'success',
                'queue_ids': list(all_queue_set)
            })
            return response
        elif queue_type and queue_id:
            # return specific details.
            active_queue_types = self._r.smembers(
                '%s:active:queue_type' % self._key_prefix)
            ready_queue_types = self._r.smembers(
                '%s:ready:queue_type' % self._key_prefix)
            all_queue_types = active_queue_types | ready_queue_types
            # queue specific rates for past 10 minutes
            timestamp = str(generate_epoch())
            keys = [
                '%s:%s:%s' % (self._key_prefix, queue_type, queue_id)
            ]
            args = [
                timestamp
            ]

            enqueue_details, dequeue_details = self._lua_metrics(
                keys=keys, args=args)

            enqueue_counts = {}
            dequeue_counts = {}
            # the length of enqueue & dequeue details are always same.
            for i in xrange(0, len(enqueue_details), 2):
                enqueue_counts[str(enqueue_details[i])] = int(
                    enqueue_details[i + 1] or 0)
                dequeue_counts[str(dequeue_details[i])] = int(
                    dequeue_details[i + 1] or 0)

            # get the queue length for the job queue
            queue_length = self._r.llen('%s:%s:%s' % (
                self._key_prefix, queue_type, queue_id))

            response.update({
                'status': 'success',
                'queue_length': int(queue_length),
                'enqueue_counts': enqueue_counts,
                'dequeue_counts': dequeue_counts
            })
            return response
        elif not queue_type and queue_id:
            raise BadArgumentException(
                '`queue_id` should be accompanied by `queue_type`.')

        return response
