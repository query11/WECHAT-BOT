import json
import io
import socket
import ssl
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request
import ilink_client


class NetworkRouteTests(unittest.TestCase):
    def setUp(self):
        self.cache = patch.object(ilink_client, '_DNS_CACHE', ((), 0))
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_only_official_ilink_bypasses_proxy(self):
        with patch.object(ilink_client._DIRECT_ILINK, 'open') as direct, patch('urllib.request.urlopen') as default:
            request = Request('https://ilinkai.weixin.qq.com/ilink/bot/getupdates')
            ilink_client._open_api(request, 40)
            direct.assert_called_once_with(request, timeout=40)
            default.assert_not_called()

    def test_custom_and_similar_hosts_keep_existing_route(self):
        for url in ('http://127.0.0.1:8888/test', 'https://ilinkai.weixin.qq.com.example.org/test'):
            with patch.object(ilink_client._DIRECT_ILINK, 'open') as direct, patch('urllib.request.urlopen') as default:
                request = Request(url)
                ilink_client._open_api(request, 5)
                default.assert_called_once_with(request, timeout=5)
                direct.assert_not_called()

    def test_real_system_address_does_not_query_external_dns(self):
        info = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('43.137.175.32', 443))]
        with patch('socket.getaddrinfo', return_value=info), \
                patch('http.client.HTTPSConnection.connect') as connect, \
                patch.object(ilink_client, '_query_ilink_dns') as dns:
            ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10).connect()
            connect.assert_called_once_with()
            dns.assert_not_called()

    def test_fake_ipv4_and_ipv6_use_verified_dns_and_preserve_tls_hostname(self):
        for ip in ('198.18.0.36', '2001:2::21'):
            self.assertTrue(ilink_client._is_fake_ip(ip))
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.36', 443))]), \
                patch.object(ilink_client, '_public_ilink_addresses', return_value=('43.137.175.32',)), \
                patch('socket.create_connection') as connect:
            context = ssl.create_default_context()
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10, context=context)
            with patch.object(context, 'wrap_socket') as tls:
                connection.connect()
            self.assertEqual(connect.call_args.args[0], ('43.137.175.32', 443))
            self.assertEqual(tls.call_args.kwargs['server_hostname'], 'ilinkai.weixin.qq.com')
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            connection.sock.settimeout.assert_called_once_with(10)

    def test_dns_ttl_cache_and_only_public_addresses(self):
        payload = dict(Status=0, Answer=[
            dict(type=1, data='198.18.0.36', TTL=120),
            dict(type=1, data='127.0.0.1', TTL=120),
            dict(type=1, data='224.0.0.1', TTL=120),
            dict(type=1, data='43.137.175.32', TTL=30)])
        raw = json.dumps(payload).encode()
        with patch.object(ilink_client, '_query_ilink_dns', return_value=raw) as dns, \
                patch('time.monotonic', return_value=100):
            self.assertEqual(ilink_client._public_ilink_addresses(40), ('43.137.175.32',))
            self.assertEqual(ilink_client._public_ilink_addresses(40), ('43.137.175.32',))
            self.assertEqual(dns.call_count, 1)
            self.assertEqual(ilink_client._DNS_CACHE[1], 130)
        with patch.object(ilink_client, '_query_ilink_dns', return_value=raw) as dns, \
                patch('time.monotonic', return_value=131):
            ilink_client._public_ilink_addresses(40)
            dns.assert_called_once()

    def test_dns_failure_does_not_retry_the_fake_address(self):
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.36', 443))]), \
                patch.object(ilink_client, '_query_ilink_dns', side_effect=OSError('DNS unavailable')), \
                patch('socket.create_connection') as connect:
            with self.assertRaises(OSError):
                ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10).connect()
            connect.assert_not_called()

    def test_other_connection_hosts_keep_system_resolution(self):
        with patch('http.client.HTTPSConnection.connect') as connect, patch('socket.getaddrinfo') as lookup:
            ilink_client._ILinkHTTPSConnection('example.org', timeout=10).connect()
            connect.assert_called_once_with()
            lookup.assert_not_called()

    def test_connect_falls_back_to_next_public_address(self):
        expected = Mock()
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.36', 443))]), \
                patch.object(ilink_client, '_public_ilink_addresses', return_value=('43.137.175.32', '43.137.191.185')), \
                patch('socket.create_connection', side_effect=[OSError('unreachable'), Mock()]) as connect:
            context = ssl.create_default_context()
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10, context=context)
            with patch.object(context, 'wrap_socket', return_value=expected):
                connection.connect()
            self.assertIs(connection.sock, expected)
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(connect.call_args.args[0], ('43.137.191.185', 443))

    def test_tls_eof_tries_next_address_before_sending_and_caches_success(self):
        ips = ('43.171.124.85', '43.137.175.32')
        first, second, secured = Mock(), Mock(), Mock()
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.36', 443))]), \
                patch.object(ilink_client, '_public_ilink_addresses', return_value=ips), \
                patch.object(ilink_client, '_DNS_CACHE', (ips, 500)), \
                patch('socket.create_connection', side_effect=[first, second]) as connect:
            context = ssl.create_default_context()
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=40, context=context)
            with patch.object(context, 'wrap_socket', side_effect=[ssl.SSLEOFError('EOF'), secured]):
                connection.connect()
            self.assertEqual(connect.call_count, 2)
            first.close.assert_called_once()
            first.sendall.assert_not_called()
            secured.settimeout.assert_called_once_with(40)
            self.assertEqual(ilink_client._DNS_CACHE, ((ips[1], ips[0]), 500))

    def test_certificate_rejection_is_not_hidden_by_address_fallback(self):
        with patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.36', 443))]), \
                patch.object(ilink_client, '_public_ilink_addresses', return_value=('43.137.175.32', '43.137.191.185')), \
                patch('socket.create_connection') as connect:
            context = ssl.create_default_context()
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10, context=context)
            with patch.object(context, 'wrap_socket', side_effect=ssl.SSLCertVerificationError('invalid certificate')):
                with self.assertRaises(ssl.SSLCertVerificationError):
                    connection.connect()
            self.assertEqual(connect.call_count, 1)


    def test_system_dns_failure_falls_back_to_encrypted_dns(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'getaddrinfo failed')), \
                patch.object(ilink_client, '_public_ilink_addresses',
                             return_value=('43.137.175.32',)) as fallback, \
                patch('socket.create_connection') as connect:
            context = ssl.create_default_context()
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10, context=context)
            with patch.object(context, 'wrap_socket'):
                connection.connect()
            self.assertEqual(connect.call_args.args[0], ('43.137.175.32', 443))
            self.assertEqual(fallback.call_args.args[1], 'dns_error')

    def test_system_dns_failure_reuses_unexpired_cache_without_querying_dns(self):
        cached = ('43.137.175.32',)
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'getaddrinfo failed')), \
                patch.object(ilink_client, '_DNS_CACHE', (cached, time.monotonic() + 300)), \
                patch.object(ilink_client, '_query_ilink_dns') as dns, \
                patch('socket.create_connection') as connect:
            context = ssl.create_default_context()
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10, context=context)
            with patch.object(context, 'wrap_socket'):
                connection.connect()
            dns.assert_not_called()
            self.assertEqual(connect.call_args.args[0], ('43.137.175.32', 443))

    def test_system_and_encrypted_dns_failure_keeps_original_error(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'getaddrinfo failed')), \
                patch.object(ilink_client, '_query_ilink_dns', side_effect=OSError('DNS unavailable')), \
                patch('socket.create_connection') as connect:
            with self.assertLogs('ilink', level='WARNING') as logs:
                with self.assertRaises(socket.gaierror) as caught:
                    ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10).connect()
            self.assertIn('DNS unavailable', '\n'.join(logs.output))
            self.assertIsInstance(caught.exception.__cause__, OSError)
            connect.assert_not_called()

    def dns_socket(self, body, close=False):
        sock = Mock()
        response = (b'HTTP/1.1 200 OK\r\n' + (b'Connection: close\r\n' if close else b'')
                    + b'Content-Length: ' + str(len(body)).encode()
                    + b'\r\n\r\n' + body)
        sock.makefile.return_value = io.BytesIO(response)
        return sock

    def test_cold_cache_uses_bootstrap_ip_and_keeps_both_tls_hostnames(self):
        body = json.dumps({'Status': 0, 'Answer': [
            {'type': 1, 'data': '43.137.175.32', 'TTL': 30}]}).encode()
        dns_socket, ilink_socket = self.dns_socket(body), Mock()
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'DNS down')) as lookup, \
                patch('socket.create_connection', side_effect=[dns_socket, ilink_socket]) as connect, \
                patch('ssl.SSLContext.wrap_socket', autospec=True,
                      side_effect=lambda context, sock, **kwargs: sock) as tls:
            connection = ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10)
            connection.connect()
        lookup.assert_called_once()
        self.assertEqual([call.args[0] for call in connect.call_args_list],
                         [('223.5.5.5', 443), ('43.137.175.32', 443)])
        self.assertEqual([call.kwargs['server_hostname'] for call in tls.call_args_list],
                         ['dns.alidns.com', 'ilinkai.weixin.qq.com'])
        for call in tls.call_args_list:
            self.assertTrue(call.args[0].check_hostname)
            self.assertEqual(call.args[0].verify_mode, ssl.CERT_REQUIRED)
        request = b''.join(call.args[0] for call in dns_socket.sendall.call_args_list)
        self.assertIn(b'Host: dns.alidns.com', request)
        self.assertNotIn(b'Authorization', request)
        ilink_socket.sendall.assert_not_called()

    def test_dns_and_all_connection_attempts_share_the_budget(self):
        now = [1000.0]
        def slow_dns(*args):
            now[0] += 8
            return ('43.137.175.32', '43.137.191.185')
        def slow_connect(address, timeout, source):
            now[0] += timeout
            raise TimeoutError('connect timeout')
        with patch('time.monotonic', side_effect=lambda: now[0]), \
                patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'DNS down')), \
                patch.object(ilink_client, '_public_ilink_addresses', side_effect=slow_dns), \
                patch('socket.create_connection', side_effect=slow_connect) as connect:
            with self.assertRaises(TimeoutError):
                ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10).connect()
        self.assertEqual(now[0] - 1000, 10)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args[1], 2)

    def test_no_tcp_attempt_after_dns_has_used_the_entire_budget(self):
        now = [1000.0]
        def slow_dns(*args):
            now[0] += 10
            return ('43.137.175.32',)
        with patch('time.monotonic', side_effect=lambda: now[0]), \
                patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'DNS down')), \
                patch.object(ilink_client, '_public_ilink_addresses', side_effect=slow_dns), \
                patch('socket.create_connection') as connect:
            with self.assertRaises(TimeoutError):
                ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10).connect()
            connect.assert_not_called()

    def test_tls_receives_only_the_remaining_handshake_budget(self):
        now, tcp = [1000.0], Mock()
        def connect_tcp(*args):
            now[0] += 2
            return tcp
        with patch('time.monotonic', side_effect=lambda: now[0]), \
                patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, 'DNS down')), \
                patch.object(ilink_client, '_public_ilink_addresses', return_value=('43.137.175.32',)), \
                patch('socket.create_connection', side_effect=connect_tcp):
            context = ssl.create_default_context()
            with patch.object(context, 'wrap_socket'):
                ilink_client._ILinkHTTPSConnection('ilinkai.weixin.qq.com', timeout=10, context=context).connect()
        tcp.settimeout.assert_called_once_with(4)

    def test_dns_lock_wait_is_bounded_by_the_query_budget(self):
        lock = Mock()
        lock.acquire.return_value = False
        with patch.object(ilink_client, '_DNS_LOCK', lock), \
                patch.object(ilink_client, '_query_ilink_dns') as query, \
                patch('time.monotonic', return_value=1000):
            with self.assertRaises(TimeoutError):
                ilink_client._public_ilink_addresses(2)
        lock.acquire.assert_called_once_with(timeout=2)
        lock.release.assert_not_called()
        query.assert_not_called()

    def test_doh_certificate_rejection_does_not_try_another_address(self):
        with patch('socket.create_connection') as connect, \
                patch('ssl.SSLContext.wrap_socket', side_effect=ssl.SSLCertVerificationError('invalid')):
            with self.assertRaises(ssl.SSLCertVerificationError):
                ilink_client._query_ilink_dns(time.monotonic() + 8)
        self.assertEqual(connect.call_count, 1)
        connect.return_value.sendall.assert_not_called()

    def test_doh_can_use_the_second_bootstrap_address(self):
        sock = self.dns_socket(b'{"Status": 0}')
        with patch('socket.create_connection', side_effect=[OSError('unreachable'), sock]) as connect, \
                patch('ssl.SSLContext.wrap_socket', return_value=sock):
            self.assertEqual(ilink_client._query_ilink_dns(time.monotonic() + 8), b'{"Status": 0}')
        self.assertEqual(connect.call_args.args[0], ('223.6.6.6', 443))

    def test_doh_rejects_oversized_body_and_closes_connection(self):
        sock = self.dns_socket(b'x' * 65537)
        with patch('socket.create_connection', return_value=sock), \
                patch('ssl.SSLContext.wrap_socket', return_value=sock):
            with self.assertRaisesRegex(OSError, '过大'):
                ilink_client._query_ilink_dns(time.monotonic() + 8)
        sock.close.assert_called_once()

    def test_doh_accepts_a_server_that_closes_after_the_response(self):
        sock = self.dns_socket(b'{"Status": 0}', close=True)
        def timeout_before_close(value):
            if sock.makefile.return_value.closed:
                raise OSError('socket is already closed after the final body read')
        sock.settimeout.side_effect = timeout_before_close
        with patch('socket.create_connection', return_value=sock), \
                patch('ssl.SSLContext.wrap_socket', return_value=sock):
            self.assertEqual(ilink_client._query_ilink_dns(time.monotonic() + 8), b'{"Status": 0}')

    def test_invalid_dns_json_is_a_reportable_network_failure(self):
        with patch.object(ilink_client, '_query_ilink_dns', return_value=b'not json'):
            with self.assertRaisesRegex(OSError, 'JSON'):
                ilink_client._public_ilink_addresses(8)
        self.assertEqual(ilink_client._DNS_CACHE, ((), 0))

if __name__ == '__main__':
    unittest.main()
