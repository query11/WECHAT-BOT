import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from bridge_runtime import Runtime, health, atomic_json
from tests import selftest_standby


def monotonic_at(value):
    """把 time.monotonic 钉在一个可控读数上（经过时长全靠它）。"""
    return patch('bridge_runtime.time.monotonic', return_value=value)


def wall_at(value):
    """把墙上时钟钉在 value，用来模拟手动改时间 / 系统对时。"""
    return patch('bridge_runtime.time.time', return_value=value)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = selftest_standby.StandbyTests()
        fixture.setUp()
        self.b = fixture.bridge
        self.b._mask = lambda s: s
        self.b._worker = Mock()
        self.b._worker.is_alive.return_value = True
        self.b._started_at = 1
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rt = Runtime(self.b, self.temp.name)

    def make_runtime(self):
        """另起一个干净的状态机，放进同一个临时目录（测试只读它的内存状态）。"""
        return Runtime(self.b, self.temp.name)

    def test_no_green_without_connection_and_worker(self):
        self.assertEqual(health('connecting', 0, True, 100)[0], 'reconnecting')
        self.assertEqual(health('online', 10, True, 200)[0], 'reconnecting')
        # 100 秒是"不新鲜"的边界：正好 100 秒还算在线，多一点就不算
        self.assertEqual(health('online', 0, True, 100)[0], 'online')
        self.assertEqual(health('online', 0, True, 101)[0], 'reconnecting')
        # 从未成功通信用 None 表示，不能拿 0 顶替
        self.assertEqual(health('online', None, True, 0)[0], 'reconnecting')
        self.assertEqual(health('online', 100, False, 100)[0], 'error')
        self.assertEqual(health('error', 100, True, 100)[0], 'error')
        self.rt.connected()
        self.assertEqual(self.rt.snapshot()['state'], 'standby')
        self.rt.command('wake')
        self.assertEqual(self.rt.snapshot()['state'], 'working')

    def test_control_refuses_busy_shutdown_and_standby(self):
        self.b.queue.put('task')
        for action in ('shutdown',):
            self.assertIn('还有任务', self.rt.command(action))
        self.assertFalse(self.b._desktop_stop.is_set())
        self.b.queue.get(); self.b.queue.task_done()
        self.rt.command('shutdown')
        self.assertTrue(self.b._desktop_stop.is_set())

    def test_old_process_command_never_executes(self):
        path = Path(self.temp.name) / 'command-test.json'
        atomic_json(path, {'pid': -1, 'at': 0, 'action': 'wake'})
        self.rt.tick()
        self.assertFalse(self.b._awake)
        self.assertIn('过期', json.loads((path.parent / 'reply-test.json').read_text(encoding='utf-8'))['message'])
        self.assertFalse(path.exists())

    def test_shutdown_during_poll_keeps_cursor(self):
        self.b.client = Mock()
        def received():
            self.b._desktop_stop.set()
            return [Mock()], 'next'
        self.b.client.get_updates.side_effect = received
        self.assertEqual(self.b.poll_once(), 0)
        self.b.client.commit_cursor.assert_not_called()

    def test_short_outage_after_online_is_not_reported(self):
        self.rt.connected()
        before = len(self.rt.events)
        self.rt.failed('SSL EOF')
        self.assertEqual(self.rt.connection, 'online')
        snapshot = self.rt.snapshot()
        self.assertEqual(snapshot['state'], 'standby')
        self.assertEqual(snapshot['error'], '')
        self.assertEqual(len(self.rt.events), before)
        self.rt.connected()
        self.assertEqual(len(self.rt.events), before)
        self.assertIsNone(self.rt._outage_since_mono)

    def test_outage_beyond_the_notice_window_is_reported(self):
        self.rt.connected()
        self.rt.failed('SSL EOF')
        self.rt._outage_since_mono = time.monotonic() - 31
        self.rt.failed('SSL EOF')
        self.assertEqual(self.rt.connection, 'reconnecting')
        self.assertEqual(self.rt.snapshot()['state'], 'reconnecting')
        self.assertIn('加密连接中断', self.rt.snapshot()['error'])

    def test_credential_rejection_is_reported_immediately(self):
        self.rt.connected()
        self.rt.failed('HTTP 401 unauthorized', True)
        self.assertEqual(self.rt.connection, 'error')
        self.assertIn('登录', self.rt.snapshot()['error'])

    def test_tick_reports_pending_outage_without_a_second_failure(self):
        with monotonic_at(1000.0), wall_at(1000.0):
            self.rt.connected()
            self.rt.failed('SSL EOF')
        with monotonic_at(1029.0), wall_at(1029.0):
            self.rt.tick()
            self.assertEqual(self.rt.snapshot()['state'], 'standby')
        with monotonic_at(1030.0), wall_at(1030.0):
            self.rt.tick()
            data = json.loads((Path(self.temp.name) / 'status.json').read_text(encoding='utf-8'))
            self.assertEqual(data['state'], 'reconnecting')
            self.assertIn('加密连接中断', data['error'])
            count = len(self.rt.events)
            self.rt.tick()
            self.assertEqual(len(self.rt.events), count)
        with monotonic_at(1180.0), wall_at(1180.0):
            self.assertEqual(self.rt.snapshot()['state'], 'error')
        self.rt.connected()
        self.assertEqual(self.rt.snapshot()['error'], '')

    def test_late_recovery_records_outage_even_if_status_loop_was_delayed(self):
        with monotonic_at(1000.0), wall_at(1000.0):
            self.rt.connected()
            before = len(self.rt.events)
            self.rt.failed('SSL EOF')
        with monotonic_at(1044.0), wall_at(1044.0):
            self.rt.connected()
        self.assertEqual(len(self.rt.events), before + 2)
        self.assertIn('加密连接中断', self.rt.events[-2])
        self.assertIn('恢复正常', self.rt.events[-1])
        self.assertIsNone(self.rt._outage_since_mono)

    # ---------- 单调时钟：墙上时间怎么跳都不影响连接判定 ----------

    def test_wall_clock_jump_does_not_change_the_connection_verdict(self):
        """同一段单调经过时长里，墙上时间前后各拨一小时，判定必须逐字一样。

        改动前这里用 time.time() 做差：往后拨一小时会让 30 秒静默期瞬间到点（故障
        提前升级），往前拨一小时会让静默期永远到不了（提示被无限推迟）。
        """
        observed = []
        for shift in (0, -3600, 3600):
            rt = self.make_runtime()
            with monotonic_at(1000.0), wall_at(1_000_000.0):
                rt.connected()
                rt.failed('SSL EOF')
            with monotonic_at(1029.0), wall_at(1_000_029.0 + shift):
                at_29 = rt.snapshot()['state']
            with monotonic_at(1030.0), wall_at(1_000_030.0 + shift):
                at_30 = rt.snapshot()['state']
            with monotonic_at(1179.0), wall_at(1_000_179.0 + shift):
                at_179 = rt.snapshot()['state']
            with monotonic_at(1180.0), wall_at(1_000_180.0 + shift):
                at_180 = rt.snapshot()['state']
            observed.append((at_29, at_30, at_179, at_180))
        self.assertEqual(observed,
                         [('standby', 'reconnecting', 'reconnecting', 'error')] * 3)

    def test_monotonic_zero_is_a_started_timer_not_an_absent_marker(self):
        """单调时钟可以从 0 开始：拿 0 当"没开始"会把静默期一直重置。"""
        rt = self.make_runtime()
        with monotonic_at(0.0), wall_at(5000.0):
            rt.connected()
            self.assertEqual(rt._last_success_mono, 0.0)
            self.assertIsNotNone(rt._last_success_mono)
            rt.failed('SSL EOF')
            self.assertEqual(rt._outage_since_mono, 0.0)
            self.assertIsNotNone(rt._outage_since_mono)
        with monotonic_at(10.0), wall_at(5010.0):
            rt.failed('SSL EOF')
            self.assertEqual(rt.connection, 'online')      # 仍在 30 秒静默期内
            self.assertIsNotNone(rt._outage_since_mono)    # 计时没有被重置
        with monotonic_at(29.999), wall_at(5029.999):
            self.assertEqual(rt.snapshot()['state'], 'standby')
        with monotonic_at(30.0), wall_at(5030.0):
            self.assertEqual(rt.snapshot()['state'], 'reconnecting')

    def test_freshness_boundary_follows_the_monotonic_clock(self):
        rt = self.make_runtime()
        with monotonic_at(500.0), wall_at(900000.0):
            rt.connected()
        with monotonic_at(600.0), wall_at(900100.0):
            self.assertEqual(rt.snapshot()['state'], 'standby')       # 正好 100 秒还算在线
        with monotonic_at(600.5), wall_at(900100.5):
            self.assertEqual(rt.snapshot()['state'], 'reconnecting')  # 超过就不新鲜了

    def test_status_json_keeps_unix_timestamps_for_display(self):
        """给界面显示时间的那几个字段必须还是 Unix 时间戳，不能混进单调读数。"""
        self.rt.connected()
        self.rt.tick()
        data = json.loads((Path(self.temp.name) / 'status.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(data['heartbeat'], time.time(), delta=30)
        self.assertAlmostEqual(data['last_success'], time.time(), delta=30)
        self.assertEqual(data['started'], self.b._started_at)
        self.assertNotIn('_last_success_mono', data)
        self.assertNotIn('_outage_since_mono', data)


if __name__ == '__main__':
    unittest.main()
