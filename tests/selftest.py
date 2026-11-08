# -*- coding: utf-8 -*-
"""离线自测：起一个假的 iLink 服务端 + 一个假的 claude，把整条链路验一遍。

    python tests/selftest.py

完全不联网、不碰真微信、不消耗额度。验的是：字段解析、分片、游标推进、白名单、
触发前缀、去重、typing、长任务不阻塞轮询、权限边界、扫码登录的状态机。

也不碰这台机器的电源：关机锁屏那一组全程用假 runner，真的 shutdown.exe 一次都不会跑
（见 selftest_power.py 开头）。

拆开了（2026-09-02）：假服务端、假 claude、造桥那些零件在 selftest_base.py，
用例按功能分在 selftest_core / _login / _apps / _media / _power / _ops / _screen /
_chat / _meeting / _files 里，这儿只剩一个 main() 管顺序和记分。入口
现在是 python tests/selftest.py（2026-11 随目录整理从根目录移入 tests/）。
"""

import json
import os
import sys

# Run as a script, so the project root (for the tests package, which in turn puts app/
# on sys.path) is not on sys.path yet.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import threading
from http.server import ThreadingHTTPServer

from tests.selftest_base import (FAIL, PASS, PORT, FakeILink, make_bridge,
                           make_fake_claude)
from tests.selftest_apps import (test_app_chain, test_app_close, test_app_match,
                           test_app_open, test_app_parse, test_app_scan)
from tests.selftest_chat import (test_chat_mode_chain, test_chat_mode_expiry,
                           test_chat_mode_off, test_chat_switch_parse,
                           test_no_prefix_hint)
from tests.selftest_core import (test_chain_basic, test_chain_rules, test_guards,
                           test_nonblocking, test_parsing, test_send_probe,
                           test_split, test_unknown_msg_key)
from tests.selftest_files import (test_aes, test_send_file_chain,
                            test_send_file_guards, test_send_file_parse,
                            test_send_file_upload)
from tests.selftest_login import test_qr_login, test_qrcode_save
from tests.selftest_media import (test_abs_volume_chain, test_abs_volume_fallback,
                            test_abs_volume_mixer, test_abs_volume_parse,
                            test_abs_volume_script, test_brightness_chain,
                            test_brightness_facade, test_brightness_parse,
                            test_brightness_script, test_media_chain,
                            test_media_keys, test_media_parse,
                            test_volume_chain, test_volume_keys,
                            test_volume_parse)
from tests.selftest_meeting import (test_meeting_chain, test_meeting_invite,
                              test_meeting_parse, test_meeting_url,
                              test_schedule_parse, test_schedule_store,
                              test_timer_chain, test_timer_edges,
                              test_timer_thread)
from tests.selftest_ops import (test_cli_entries, test_config_bom, test_config_bot_token,
                          test_context_ttl,
                          test_log_redaction, test_log_rotation,
                          test_poll_timeout_is_normal,
                          test_scripts_are_ascii_and_read_utf8,
                          test_shared_doc_paragraphs)
from tests.selftest_power import (test_power_chain, test_power_factory, test_power_parse,
                            test_power_schedule, test_power_shutdown,
                            test_power_sleep, test_power_status_lock,
                            test_sleep_chain)
from tests.selftest_screen import (test_screen_ambiguous, test_screen_confirm,
                             test_screen_desktop, test_screen_dpi,
                             test_screen_look, test_screen_parse,
                             test_screen_probe_diag, test_screen_refuse,
                             test_screen_surface_only, test_screen_tree,
                             test_screen_vision)
from tests.selftest_style import test_reply_marks
from tests.selftest_parse_probe import (test_command_shape, test_order_matches_bridge,
                                 test_report_shape)

from tests.selftest_remind import (test_remind_chain, test_remind_config,
                            test_remind_daily_chain, test_remind_daily_parse,
                            test_remind_daily_store, test_remind_list_cancel_parse,
                            test_remind_off_and_gated,
                            test_remind_parse, test_remind_store,
                            test_task_chain, test_task_parse, test_task_store)

from tests.selftest_admin import (test_admin_parse, test_all_enqueue_goes_through_one_door,
                            test_enqueue_never_blocks, test_help_lists_admin,
                            test_kill_tree, test_kill_tree_uses_returncode, test_log,
                            test_queue_and_status_with_work,
                            test_queue_max, test_reload_enables_timer,
                            test_reload_guard_runs_before_build, test_reload_ok,
                            test_reload_guards_whitelist,
                            test_reload_survives_disk_error,
                            test_reload_warns_credentials,
                            test_reload_refuses_bad_config,
                            test_reload_refuses_while_busy,
                            test_run_timeout_and_proc, test_runner_cancel,
                            test_running_covers_every_job, test_status_and_queue,
                            test_stop, test_stop_does_not_touch_queue,
                            test_stop_hides_other_users_words, test_stop_words,
                            test_stopall_clears_queue, test_task_cancelled_chain)

from tests.selftest_inbox import (test_parse_items_unchanged, test_probe_main,
                            test_probe_reads_log, test_probe_verdict,
                            test_shape_limits, test_shape_log, test_shape_masks)

from tests.selftest_inbox_recv import (test_download_info_parse, test_inbox_config_state,
                                 test_media_key, test_receive_disabled,
                                 test_receive_file_with_check, test_receive_http_error,
                                 test_receive_image, test_receive_image_size_hint,
                                 test_receive_mixed_with_text, test_receive_no_url,
                                 test_receive_over_limit, test_receive_rejects_bad,
                                 test_safe_name, test_store)

from tests.selftest_inbox_quota import (test_inbox_answer_parse,
                                  test_inbox_cfg_derived,
                                  test_quota_answer_expires,
                                  test_quota_answer_no,
                                  test_quota_answer_yes_deletes_and_resumes,
                                  test_quota_asks_when_full,
                                  test_quota_delete_only_registered,
                                  test_quota_delete_refused_when_recycle_fails,
                                  test_quota_nothing_to_free,
                                  test_quota_other_words_do_not_answer,
                                  test_quota_under_limit_passes,
                                  test_recycle_delete_wrapper,
                                  test_store_oldest_find_remove)

def main():
    tmpdir = tempfile.mkdtemp(prefix="ilink-selftest-")
    fake_claude = make_fake_claude(tmpdir)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), FakeILink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        print("=== 解析与分片 ===")
        test_parsing()
        test_split()
        print("\n=== 权限边界 ===")
        test_guards(tmpdir)
        print("\n=== 全链路 ===")
        bridge = make_bridge(tmpdir, fake_claude)
        test_chain_basic(bridge, tmpdir)
        print("\n=== 规则 ===")
        test_chain_rules(bridge, tmpdir, fake_claude)
        print("\n=== 对话模式（免前缀）===")
        test_chat_switch_parse()
        test_chat_mode_chain(tmpdir, fake_claude)
        test_chat_mode_expiry(tmpdir, fake_claude)
        test_chat_mode_off(tmpdir, fake_claude)
        test_no_prefix_hint(tmpdir, fake_claude)
        print("\n=== 并发 ===")
        test_nonblocking(bridge)
        print("\n=== 键名兜底 ===")
        test_unknown_msg_key(tmpdir, fake_claude)
        test_send_probe(tmpdir, fake_claude)
        print("\n=== 开关应用 ===")
        test_app_scan(tmpdir)
        test_app_match(tmpdir)
        test_app_parse()
        test_app_open(tmpdir)
        test_app_close(tmpdir)
        test_app_chain(tmpdir, fake_claude)
        print("\n=== 播放控制 ===")
        test_media_parse()
        test_media_keys()
        test_volume_parse()
        test_volume_keys()
        test_volume_chain(tmpdir, fake_claude)
        test_media_chain(tmpdir, fake_claude)
        print("\n=== 绝对音量（系统混音器） ===")
        test_abs_volume_parse()
        test_abs_volume_script()
        test_abs_volume_mixer()
        test_abs_volume_fallback()
        test_abs_volume_chain(tmpdir, fake_claude)
        print("\n=== 屏幕亮度（WMI 背光） ===")
        test_brightness_parse()
        test_brightness_script()
        test_brightness_facade()
        test_brightness_chain(tmpdir, fake_claude)
        print("\n=== 关机、锁屏 ===")
        test_power_parse()
        test_power_status_lock()
        test_power_shutdown()
        test_power_schedule()
        test_power_factory()
        test_power_chain(tmpdir, fake_claude)
        print("\n=== 睡眠 / 休眠 ===")
        test_power_sleep()
        test_sleep_chain(tmpdir, fake_claude)
        print("\n=== 进腾讯会议 ===")
        test_meeting_parse()
        test_meeting_invite()
        test_meeting_url()
        test_meeting_chain(tmpdir, fake_claude)
        print("\n=== 定时进会 ===")
        test_schedule_parse()
        test_schedule_store(tmpdir)
        test_timer_chain(tmpdir, fake_claude)
        test_timer_edges(tmpdir, fake_claude)
        test_timer_thread(tmpdir, fake_claude)
        print("\n=== 发文件 ===")
        test_aes()
        test_send_file_parse()
        test_send_file_guards(tmpdir)
        test_send_file_upload(tmpdir)
        test_send_file_chain(tmpdir, fake_claude)
        print("\n=== 扫码登录 ===")
        test_qrcode_save(tmpdir)
        test_qr_login(tmpdir)
        print("\n=== 日志留存 ===")
        test_log_redaction(tmpdir, fake_claude)
        test_log_rotation(tmpdir)
        print("\n=== 上下文保鲜期 ===")
        test_context_ttl(tmpdir)
        test_poll_timeout_is_normal(tmpdir, fake_claude)
        print("\n=== 诊断指令 /parse ===")
        test_report_shape()
        test_command_shape(tmpdir, fake_claude)
        test_order_matches_bridge(tmpdir, fake_claude)
        print("\n=== 定时提醒 ===")
        test_remind_parse()
        test_remind_daily_parse()
        test_remind_list_cancel_parse()
        test_remind_store(tmpdir)
        test_remind_daily_store(tmpdir)
        test_remind_chain(tmpdir, fake_claude)
        test_remind_daily_chain(tmpdir, fake_claude)
        test_remind_off_and_gated(tmpdir, fake_claude)
        test_remind_config(tmpdir)
        print("\n=== 到点跑一句话（定时任务） ===")
        test_task_parse()
        test_task_store(tmpdir)
        test_task_chain(tmpdir, fake_claude)
        print("\n=== 命令行入口 ===")
        test_cli_entries(tmpdir)
        test_scripts_are_ascii_and_read_utf8()
        test_config_bom(tmpdir)
        test_config_bot_token(tmpdir)
        test_shared_doc_paragraphs()
        print("\n=== 看画面与点鼠标 ===")
        test_screen_parse()
        test_screen_tree(tmpdir)
        test_screen_confirm(tmpdir, fake_claude)
        test_screen_ambiguous(tmpdir, fake_claude)
        test_screen_refuse(tmpdir, fake_claude)
        test_screen_vision(tmpdir, fake_claude)
        test_screen_dpi(tmpdir, fake_claude)
        test_screen_desktop(tmpdir, fake_claude)
        test_screen_probe_diag(tmpdir, fake_claude)
        test_screen_surface_only(tmpdir, fake_claude)
        test_screen_look(tmpdir, fake_claude)
        print("\n=== 管桥：状态 / 日志 / 重读配置 / 队列 / 叫停 ===")
        test_admin_parse()
        test_stop_words()
        test_status_and_queue(tmpdir, fake_claude)
        test_queue_and_status_with_work(tmpdir, fake_claude)
        test_queue_max(tmpdir, fake_claude)
        test_enqueue_never_blocks(tmpdir, fake_claude)
        test_all_enqueue_goes_through_one_door()
        test_running_covers_every_job(tmpdir, fake_claude)
        test_log(tmpdir, fake_claude)
        test_reload_ok(tmpdir, fake_claude)
        test_reload_refuses_bad_config(tmpdir, fake_claude)
        test_reload_guards_whitelist(tmpdir, fake_claude)
        test_reload_guard_runs_before_build(tmpdir, fake_claude)
        test_reload_survives_disk_error(tmpdir, fake_claude)
        test_reload_refuses_while_busy(tmpdir, fake_claude)
        test_reload_warns_credentials(tmpdir, fake_claude)
        test_reload_enables_timer(tmpdir, fake_claude)
        test_stop(tmpdir, fake_claude)
        test_stop_hides_other_users_words(tmpdir, fake_claude)
        test_stop_does_not_touch_queue(tmpdir, fake_claude)
        test_stopall_clears_queue(tmpdir, fake_claude)
        test_task_cancelled_chain(tmpdir, fake_claude)
        test_kill_tree(tmpdir)
        test_kill_tree_uses_returncode()
        test_run_timeout_and_proc(tmpdir, fake_claude)
        test_runner_cancel(tmpdir, fake_claude)
        test_help_lists_admin()
        print("\n=== 收到的图片和文件（形状日志 / inbox_probe）===")
        test_shape_masks()
        test_shape_limits()
        test_shape_log()
        test_parse_items_unchanged()
        test_probe_reads_log(tmpdir)
        test_probe_verdict()
        test_probe_main(tmpdir)
        print("\n=== 收下你发来的图片和文件 ===")
        test_download_info_parse()
        test_media_key()
        test_safe_name()
        test_store(tmpdir)
        test_receive_image(tmpdir, fake_claude)
        test_receive_image_size_hint(tmpdir, fake_claude)
        test_receive_file_with_check(tmpdir, fake_claude)
        test_receive_rejects_bad(tmpdir, fake_claude)
        test_receive_over_limit(tmpdir, fake_claude)
        test_receive_http_error(tmpdir, fake_claude)
        test_receive_no_url(tmpdir, fake_claude)
        test_receive_mixed_with_text(tmpdir, fake_claude)
        test_receive_disabled(tmpdir, fake_claude)
        test_inbox_config_state(tmpdir, fake_claude)
        print("\n=== 收件夹的配额、问询、删除 ===")
        test_inbox_answer_parse()
        test_store_oldest_find_remove(tmpdir)
        test_recycle_delete_wrapper()
        test_quota_under_limit_passes(tmpdir, fake_claude)
        test_quota_asks_when_full(tmpdir, fake_claude)
        test_quota_nothing_to_free(tmpdir, fake_claude)
        test_quota_answer_no(tmpdir, fake_claude)
        test_quota_answer_yes_deletes_and_resumes(tmpdir, fake_claude)
        test_quota_delete_refused_when_recycle_fails(tmpdir, fake_claude)
        test_quota_delete_only_registered(tmpdir, fake_claude)
        test_quota_answer_expires(tmpdir, fake_claude)
        test_quota_other_words_do_not_answer(tmpdir, fake_claude)
        test_inbox_cfg_derived(tmpdir, fake_claude)
        print("\n=== 回话文案 ===")
        test_reply_marks()
    finally:
        srv.shutdown()
        srv.server_close()
    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
    report_path = os.environ.get("SELFTEST_REPORT_JSON")
    if report_path:
        # 给统一验证入口 tools/verify.py 一份机器可读的结果。终端那行中文是给人看的，
        # 谁去正则匹配它，编码一变数字就漂；这里写的是真实结果对象里的计数。
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump({"kind": "checks", "checks": len(PASS) + len(FAIL),
                       "passed": len(PASS), "failed": len(FAIL),
                       "failures": list(FAIL)}, fh, ensure_ascii=False)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

