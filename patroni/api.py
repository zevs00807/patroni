import base64
import fcntl
import json
import logging
import psycopg2

from patroni.exceptions import PostgresConnectionException
from patroni.utils import Retry, RetryFailedError
from six.moves.BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver import ThreadingMixIn
from threading import Thread

logger = logging.getLogger(__name__)


def check_auth(func):
    """Decorator function to check authorization header.

    Usage example:
    @check_auth
    def do_PUT_foo():
        pass
    """
    def wrapper(handler):
        if handler.check_auth_header():
            return func(handler)
    return wrapper


class RestApiHandler(BaseHTTPRequestHandler):

    def send_auth_request(self, body):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm=\"Patroni\"')
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def check_auth_header(self):
        auth_header = self.headers.get('Authorization')
        status = self.server.check_auth_header(auth_header)
        return not status or self.send_auth_request(status)

    def do_GET(self):
        """Default method for processing all GET requests which can not be routed to other methods"""

        path = '/master' if self.path == '/' else self.path
        response = self.get_postgresql_status()

        patroni = self.server.patroni
        if 'role' in response and response['role'] in path:
            status_code = 200
        elif patroni.ha.restart_scheduled() and patroni.postgresql.role == 'master' and 'master' in path:
            # exceptional case for master node when the postgres is being restarted via API
            status_code = 200
        else:
            status_code = 503

        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    def do_GET_patroni(self):
        response = self.get_postgresql_status(True)

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(response).encode('utf-8'))

    @check_auth
    def do_POST_restart(self):
        action = self.server.patroni.ha.schedule_restart()
        if action is not None:
            status_code = 503
            data = (action + ' already in progress').encode('utf-8')
        else:
            status_code = 503
            data = b'restart failed'
            try:
                if self.server.patroni.ha.restart():
                    status_code = 200
                    data = b'restarted successfully'
            except:
                logger.exception('Exception during restart')

        self.send_response(status_code)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(data)

    @check_auth
    def do_POST_reinitialize(self):
        ha = self.server.patroni.ha
        cluster = ha.dcs.get_cluster()
        if cluster.is_unlocked():
            status_code = 503
            data = b'Cluster has no leader, can not reinitialize'
        elif cluster.leader.name == ha.state_handler.name:
            status_code = 503
            data = b'I am the leader, can not reinitialize'
        else:
            action = ha.schedule_reinitialize()
            if action is not None:
                status_code = 503
                data = (action + ' already in progress').encode('utf-8')
            else:
                status_code = 200
                data = b'reinitialize scheduled'

        self.send_response(status_code)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(data)

    def parse_request(self):
        """Override parse_request method to enrich basic functionality of `BaseHTTPRequestHandler` class

        Original class can only invoke do_GET, do_POST, do_PUT, etc method implementations if they are defined.
        But we would like to have at least some simple routing mechanism, i.e.:
        GET /uri1/part2 request should invoke `do_GET_uri1()`
        POST /other should invoke `do_POST_other()`

        If the `do_<REQUEST_METHOD>_<first_part_url>` method does not exists we'll fallback to original behavior."""

        ret = BaseHTTPRequestHandler.parse_request(self)
        if ret:
            mname = self.path.lstrip('/').split('/')[0]
            mname = self.command + ('_' + mname if mname else '')
            if hasattr(self, 'do_' + mname):
                self.command = mname
        return ret

    def query(self, sql, *params, **kwargs):
        if not kwargs.get('retry', False):
            return self.server.query(sql, *params)
        retry = Retry(delay=2, retry_exceptions=PostgresConnectionException)
        return retry(self.server.query, sql, *params)

    def get_postgresql_status(self, retry=False):
        try:
            row = self.query("""SELECT to_char(pg_postmaster_start_time(), 'YYYY-MM-DD HH24:MI:SS.MS TZ'),
                                       pg_is_in_recovery(),
                                       CASE WHEN pg_is_in_recovery()
                                            THEN null
                                            ELSE pg_current_xlog_location() END,
                                       pg_last_xlog_receive_location(),
                                       pg_last_xlog_replay_location(),
                                       pg_is_in_recovery() AND pg_is_xlog_replay_paused()""", retry=retry)[0]
            return {
                'state': self.server.patroni.postgresql.state,
                'postmaster_start_time': row[0],
                'role': 'replica' if row[1] else 'master',
                'xlog': ({
                    'received_location': row[3],
                    'replayed_location': row[4],
                    'paused': row[5]} if row[1] else {
                    'location': row[2]
                })
            }
        except (psycopg2.Error, RetryFailedError, PostgresConnectionException):
            logger.exception('get_postgresql_status')
            return {'state': self.server.patroni.postgresql.state}


class RestApiServer(ThreadingMixIn, HTTPServer, Thread):

    def __init__(self, patroni, config):
        self._auth_key = base64.b64encode(config['auth'].encode('utf-8')).decode('utf-8') if 'auth' in config else None
        host, port = config['listen'].split(':')
        HTTPServer.__init__(self, (host, int(port)), RestApiHandler)
        Thread.__init__(self, target=self.serve_forever)
        self._set_fd_cloexec(self.socket)

        protocol = 'http'

        # wrap socket with ssl if 'certfile' is defined in a config.yaml
        # Sometime it's also needed to pass reference to a 'keyfile'.
        options = {option: config[option] for option in ['certfile', 'keyfile'] if option in config}
        if options.get('certfile', None):
            import ssl
            self.socket = ssl.wrap_socket(self.socket, server_side=True, **options)
            protocol = 'https'

        self.connection_string = '{}://{}/patroni'.format(protocol, config.get('connect_address', config['listen']))

        self.patroni = patroni
        self.daemon = True

    def query(self, sql, *params):
        cursor = None
        try:
            with self.patroni.postgresql.connection().cursor() as cursor:
                cursor.execute(sql, params)
                return [r for r in cursor]
        except psycopg2.Error as e:
            if cursor and cursor.connection.closed == 0:
                raise e
            raise PostgresConnectionException('connection problems')

    @staticmethod
    def _set_fd_cloexec(fd):
        flags = fcntl.fcntl(fd, fcntl.F_GETFD)
        fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)

    def check_basic_auth_key(self, key):
        return self._auth_key == key

    def check_auth_header(self, auth_header):
        if self._auth_key:
            if auth_header is None:
                return 'no auth header received'
            if not auth_header.startswith('Basic ') or not self.check_basic_auth_key(auth_header[6:]):
                return 'not authenticated'
