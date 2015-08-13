import time
import select

from weakref import ref as weakref
from threading import local, RLock

from redis import StrictRedis

from redis.exceptions import ConnectionError


_local = local()


def get_current_routing_client():
    try:
        return _local.routing_stack[-1]
    except (AttributeError, IndexError):
        return None


class EventualResult(object):

    def __init__(self, client, connection, command_name):
        self._client = weakref(client)
        self.connection = connection
        self.command_name = command_name
        self.value = None
        self.result_ready = False

    @property
    def client(self):
        client = self._client()
        if client is not None:
            return client
        raise RuntimeError('Client went away')

    def fileno(self):
        if self.connection is None or \
           self.connection._sock is None:
            raise ValueError('I/O operation on closed file')
        return self.connection._sock.fileno()

    def cancel(self):
        if self.result_ready or self.connection is None:
            return
        self.connection.disconnect()
        self.client.connection_pool.release(self.connection)
        self.connection = None
        self.client.notify_request_done(self)

    def wait_for_result(self):
        if not self.result_ready:
            try:
                self.value = self.client.parse_response(self.connection,
                                                        self.command_name)
            finally:
                self.client.notify_request_done(self)
                self.client.connection_pool.release(self.connection)
        return self.value


class RoutingPool(object):
    """The routing pool works together with the routing client to
    internally dispatch through the cluster's router to the correct
    internal connection pool.
    """

    def __init__(self, cluster):
        self.cluster = cluster

    def get_connection(self, command_name, shard_hint=None,
                       command_args=None):
        if command_args is None:
            raise TypeError('The routing pool requires that the command '
                            'arguments are provided.')

        router = self.cluster.get_router()
        host_id = router.get_host(command_name, command_args)
        if host_id is None:
            raise RuntimeError('Unable to determine host for command')

        real_pool = self.cluster.get_pool_for_host(host_id)

        con = real_pool.get_connection(command_name, shard_hint)
        con.__creating_pool = weakref(real_pool)
        return con

    def release(self, connection):
        # The real pool is referenced by the connection through an
        # internal weakref.  If the weakref is broken it means the
        # pool is already gone and we do not need to release the
        # connection.
        try:
            real_pool = connection.__creating_pool()
        except (AttributeError, TypeError):
            real_pool = None

        if real_pool is not None:
            real_pool.release(connection)

    def disconnect(self):
        self.cluster.disconnect_pools()

    def reset(self):
        pass


class BaseClient(StrictRedis):
    pass


class RoutingClient(BaseClient):
    """The routing client uses the cluster's router to target an individual
    node automatically based on the key of the redis command executed.
    """

    def __init__(self, cluster, max_concurrency=None,
                 connection_pool=None):
        if connection_pool is None:
            connection_pool = RoutingPool(cluster)
        BaseClient.__init__(self, connection_pool=connection_pool)
        self.max_concurrency = max_concurrency
        self.current_requests = []
        self._routing_lock = RLock()

    def pubsub(self, **kwargs):
        raise NotImplementedError('Pubsub is unsupported.')

    def pipeline(self, transaction=True, shard_hint=None):
        raise NotImplementedError('Pipelines are unsupported.')

    def execute_command(self, *args):
        command_name = args[0]
        connection = self.connection_pool.get_connection(
            command_name, command_args=args[1:])
        try:
            connection.send_command(*args)
            return self.eventual_parse_response(connection, command_name)
        except ConnectionError:
            connection.disconnect()
            connection.send_command(*args)
            return self.eventual_parse_response(connection, command_name)

    def eventual_parse_response(self, connection, command_name):
        er = EventualResult(self, connection, command_name)
        with self._routing_lock:
            # We need to make sure that we don't run too many tasks
            # concurrently.  Here we just start blocking if we hit the
            # max concurrency until some tasks free up.
            if self.max_concurrency is not None:
                while len(self.current_requests) >= self.max_concurrency:
                    for other_er in select.select(self.current_requests[:],
                                                  [], [], 1.0)[0]:
                        other_er.wait_for_result()

            self.current_requests.append(er)
        return er

    def notify_request_done(self, er):
        with self._routing_lock:
            try:
                self.current_requests.remove(er)
            except ValueError:
                pass

    def wait_for_outstanding_responses(self, timeout=None):
        """Waits for all outstanding responses to come back or the
        timeout to be hit.
        """
        remaining = timeout

        while self.current_requests and (remaining is None or
                                         remaining > 0):
            now = time.time()
            rv = select.select(self.current_requests[:], [], [], remaining)
            if remaining is not None:
                remaining -= (time.time() - now)
            for er in rv[0]:
                er.wait_for_result()

    def cancel_outstanding_requests(self):
        """Cancels all outstanding requests."""
        for er in self.current_requests:
            er.cancel()


class LocalClient(BaseClient):
    """The local client is just a convenient method to target one specific
    host.
    """

    def __init__(self, cluster, connection_pool=None, **kwargs):
        if connection_pool is None:
            raise TypeError('The local client needs a connection pool')
        BaseClient.__init__(self, cluster, connection_pool=connection_pool,
                            **kwargs)
