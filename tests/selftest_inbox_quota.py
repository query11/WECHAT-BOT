# -*- coding: utf-8 -*-
"""收件夹的**总配额 + 问询 + 删除**（这一半全是不可逆动作，所以测得细）。

规矩来自用户拍板：满了**不自动删**，先问一句「要腾出 X，最老的是…，删掉吗？」；
只有回「删掉／删吧」才删；回「不删」或 5 分钟没回就当没收；**只删登记在册、自己命名
的文件**；删除一律**走回收站**（能找回）——回收站调不动就**一个都不删**。

这里把 `bridge_inbox._recycle_runner` 整个换成假的：记下删过哪些路径，**一个文件都
不会真删**。
"""

import os
import time

import bridge_config
import bridge_inbox
import bridge_text
import ilink_client

from tests.selftest_base import FakeILink, check, make_bridge, make_fake_claude, texts, wecom_msg

from tests.selftest_inbox_recv import (KEY, PLAIN_FILE, PLAIN_IMG, _bridge, _drain,
                                 _files_in, _serve, _wait_sent, file_item, image_item)


class FakeRecycle(object):
    """假回收站：记下删过谁；`fail_on` 里的路径当作删不掉。"""

    def __init__(self, fail_on=()):
        self.gone = []
        self.fail_on = set(fail_on)

    def __call__(self, path):
        if path in self.fail_on or os.path.basename(path) in self.fail_on:
            raise RuntimeError("假的：回收站拒绝了")
        if not os.path.exists(path):
            raise RuntimeError("假的：文件不存在")
        self.gone.append(path)
        os.remove(path)      # 回收站的行为：文件从收件夹里消失（这里只删测试造的东西）


def _use_fake_recycle(fail_on=()):
    """把真的回收站整个换掉。**一个真文件都不会被删**（只删测试自己造的）。"""
    fake = FakeRecycle(fail_on)
    bridge_inbox._recycle_runner = fake
    return fake


# ---------- ① 纯解析：只认那三个说法 ----------

def test_inbox_answer_parse():
    yes = ("删掉", "删吧", "删", "删掉吧", "删了", "删掉。", " 删掉 ")
    no = ("不删", "别删", "不删了", "算了", "不了")
    neither = ("确认", "", "删除", "把这个文件删掉", "好", "删 掉", "/stop")
    bad = [s for s in yes if bridge_text.parse_inbox_answer(s) != "yes"]
    check("认得出「删掉」这一批", not bad, bad)
    bad = [s for s in no if bridge_text.parse_inbox_answer(s) != "no"]
    check("认得出「不删」这一批", not bad, bad)
    bad = [s for s in neither if bridge_text.parse_inbox_answer(s) is not None]
    check("**刻意不认**这些（尤其「确认」——它已经有两个主人）", not bad, bad)


# ---------- ② 登记表的新方法 ----------

def test_store_oldest_find_remove(tmpdir):
    path = os.path.join(tmpdir, "store-oldest.json")
    st = bridge_inbox.InboxStore(path)
    st.add({"name": "新.txt", "bytes": 10, "at": 300.0})
    st.add({"name": "老.txt", "bytes": 20, "at": 100.0})
    st.add({"name": "中.txt", "bytes": 30, "at": 200.0})
    check("oldest 按收到的时刻排（最老的在最前）",
          [it["name"] for it in st.oldest(2)] == ["老.txt", "中.txt"], st.oldest(2))
    check("find 找得到", (st.find("中.txt") or {}).get("bytes") == 30, st.find("中.txt"))
    check("find 找不到给 None", st.find("没有.txt") is None, "不该有")
    gone = st.remove("老.txt")
    check("remove 返回被划掉的那条", gone and gone["name"] == "老.txt", gone)
    check("remove 之后册子里没有了", st.find("老.txt") is None, st.all())
    check("remove 不存在的名字给 None", st.remove("没有.txt") is None, "不该有")
    check("划掉的改动落盘了（重开也看不到）",
          bridge_inbox.InboxStore(path).find("老.txt") is None, "没落盘")
    check("总量跟着变", st.total_bytes() == 40, st.total_bytes())


def test_recycle_delete_wrapper():
    """recycle_delete 只报成没成；删不掉**不许**退回 os.remove。"""
    called = []
    ok, why = bridge_inbox.recycle_delete("/tmp/x", runner=lambda p: called.append(p))
    check("跑得动就报成功", ok is True and why == "", (ok, why))
    check("路径原样交给回收站", called == ["/tmp/x"], called)

    def boom(p):
        raise RuntimeError("回收站不干了")

    ok2, why2 = bridge_inbox.recycle_delete("/tmp/y", runner=boom)
    check("回收站不干时报失败（不抛出去）", ok2 is False, (ok2, why2))
    check("失败原因带回来", "回收站不干了" in why2, why2)
    check("项目里有真的回收站实现（Windows 上用 Microsoft.VisualBasic）",
          callable(bridge_inbox._recycle_runner), bridge_inbox._recycle_runner)


# ---------- ③ 配额：够就放行、不够先问 ----------

def _bridge_with_quota(tmpdir, fake_claude, quota_mb):
    b = _bridge(tmpdir, fake_claude)
    b.inbox_quota_bytes = int(quota_mb * 1024 * 1024)
    b.inbox_ask_seconds = 300
    return b


def _seed_files(b, n, size=2000):
    """先塞 n 个「已登记」的假收件（连文件一起造出来），返回它们的名字。"""
    os.makedirs(b.inbox_dir, exist_ok=True)
    names = []
    for i in range(n):
        name = "老文件%d.bin" % i
        with open(os.path.join(b.inbox_dir, name), "wb") as fh:
            fh.write(b"\0" * size)
        b.inbox_store.add({"name": name, "orig": name, "kind": "文件", "uid": "u",
                           "at": time.time() - (n - i) * 3600, "bytes": size,
                           "md5": ""})
        names.append(name)
    return names


def test_quota_under_limit_passes(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 1)          # 1 MB
    _drain(b, [wecom_msg("", "q1", items=[image_item()])])
    check("没超配额就照收，不问", any("收下了 1 个附件" in t for t in _wait_sent(1)))


def test_quota_asks_when_full(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)      # 约 1 KB
    names = _seed_files(b, 3, size=2000)                    # 已经超了
    _drain(b, [wecom_msg("", "q2", items=[image_item()])])
    sent = _wait_sent(1)
    txt = "\n".join(sent)
    check("满了先问一句，不是直接删", "收件夹快满了" in txt, sent)
    check("问话里说要腾出多少", "得先腾出" in txt, sent)
    check("问话里点名最老的那个", names[0] in txt, sent)
    check("告诉用户回什么（删掉 / 不删）", "回「删掉」" in txt and "回「不删」" in txt, sent)
    check("问话写明了不回就当没收", "当没收" in txt, sent)
    check("**先没删任何东西**", _files_in(b.inbox_dir) and
          len(_files_in(b.inbox_dir)) == 3, _files_in(b.inbox_dir))
    check("记下了「等用户回答」的那一条", "u-me" in b._inbox_pending, b._inbox_pending)
    check("这次没收（附件没落盘）", "180-图片" not in txt, sent)


def test_quota_nothing_to_free(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 0.0001)     # 约 104 字节，比图还小
    _drain(b, [wecom_msg("", "q3", items=[image_item()])])   # 册子是空的
    sent = _wait_sent(1)
    check("册子里没东西可删时如实说，并让用户自己去清",
          any("你自己去" in t and "清一下" in t for t in sent), sent)
    check("没记「等回答」（因为这题无解）", "u-me" not in b._inbox_pending,
          b._inbox_pending)


# ---------- ④ 回答「不删」 ----------

def test_quota_answer_no(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)
    names = _seed_files(b, 2)
    _drain(b, [wecom_msg("", "q4", items=[image_item()])])
    _wait_sent(1)
    _drain(b, [wecom_msg("不删", "q5")])
    sent = _wait_sent(2)
    check("回「不删」就明说这条没收", any("不删，这条我不收了" in t for t in sent), sent)
    check("**一个文件都没删**", len(_files_in(b.inbox_dir)) == 2, _files_in(b.inbox_dir))
    check("账也没动", b.inbox_store.total_bytes() == 4000, b.inbox_store.all())
    check("问话清掉了", "u-me" not in b._inbox_pending, b._inbox_pending)
    check("文件都还在（名字没变）", sorted(_files_in(b.inbox_dir)) == sorted(names),
          _files_in(b.inbox_dir))


# ---------- ⑤ 回答「删掉」→ 只删在册的、走回收站、接着把附件收下 ----------

def test_quota_answer_yes_deletes_and_resumes(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    fake = _use_fake_recycle()
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)
    names = _seed_files(b, 3)
    _drain(b, [wecom_msg("", "q6", items=[image_item()])])
    _wait_sent(1)
    _drain(b, [wecom_msg("删掉", "q7")])
    sent = _wait_sent(3)
    txt = "\n".join(sent)
    check("回「删掉」就去删", "删掉了" in txt, sent)
    check("删走的是**回收站**那条路（假的记下了路径）", len(fake.gone) >= 1, fake.gone)
    check("删的都是最老的（老文件0 在里头）",
          any(n.endswith("老文件0.bin") for n in fake.gone), fake.gone)
    check("删掉的那个文件真不在盘上了", not os.path.exists(
        os.path.join(b.inbox_dir, names[0])), _files_in(b.inbox_dir))
    check("册子里也划掉了", b.inbox_store.find(names[0]) is None, b.inbox_store.all())
    check("接着把这条附件收下了（腾完就下）", "收下了 1 个附件" in txt, sent)
    check("那条图片真落盘了",
          any(f.endswith(".jpg") for f in _files_in(b.inbox_dir)), _files_in(b.inbox_dir))
    check("问话清掉了", "u-me" not in b._inbox_pending, b._inbox_pending)


def test_quota_delete_refused_when_recycle_fails(tmpdir, fake_claude):
    """回收站调不动时：**一个都不删**，册子也不动，如实说不收。"""
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)
    names = _seed_files(b, 2)
    fake = _use_fake_recycle(fail_on=names)            # 每个都会失败
    _drain(b, [wecom_msg("", "q8", items=[image_item()])])
    _wait_sent(1)
    _drain(b, [wecom_msg("删掉", "q9")])
    sent = _wait_sent(2)
    check("删不掉时如实说", any("删不掉" in t for t in sent), sent)
    check("一个都没删成", fake.gone == [], fake.gone)
    check("盘上文件一个不少", len(_files_in(b.inbox_dir)) == 2, _files_in(b.inbox_dir))
    check("册子一条没动", len(b.inbox_store.all()) == 2, b.inbox_store.all())
    check("这次也没收（文件没下）",
          not any(f.endswith(".jpg") for f in _files_in(b.inbox_dir)),
          _files_in(b.inbox_dir))


def test_quota_delete_only_registered(tmpdir, fake_claude):
    """册子里没有的名字、或者路径想爬出收件夹的，**一律不碰**。"""
    _serve(PLAIN_IMG)
    fake = _use_fake_recycle()
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)
    _seed_files(b, 1)
    outsider = os.path.join(tmpdir, "外面的东西.txt")
    with open(outsider, "w", encoding="utf-8") as fh:
        fh.write("别碰我")
    os.makedirs(b.inbox_dir, exist_ok=True)
    intruder = os.path.join(b.inbox_dir, "爬出去.txt")
    with open(intruder, "w", encoding="utf-8") as fh:
        fh.write("也别碰我")
    gone, failed = b._inbox_delete_planned("u-me", ["不存在的.txt", "..\\爬出去.txt",
                                                    "../外面的东西.txt"])
    check("没删任何东西", gone == [] and fake.gone == [], (gone, fake.gone))
    check("每一条都给了拒的理由", len(failed) == 3, failed)
    check("外面的文件还在", os.path.exists(outsider), "没了")
    check("收件夹里那个也在", os.path.exists(intruder), "没了")


def test_quota_answer_expires(tmpdir, fake_claude):
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)
    _seed_files(b, 2)
    _drain(b, [wecom_msg("", "qa", items=[image_item()])])
    _wait_sent(1)
    b._inbox_pending["u-me"]["at"] = time.time() - 4000        # 装作过了 1 小时
    fake = _use_fake_recycle()
    _drain(b, [wecom_msg("删掉", "qb")])
    sent = _wait_sent(2)
    check("超时后的「删掉」不作数，只说明作废了",
          any("过去太久了" in t and "我没收" in t for t in sent), sent)
    check("**超时也一个都不删**", fake.gone == [], fake.gone)
    check("盘上原样", len(_files_in(b.inbox_dir)) == 2, _files_in(b.inbox_dir))
    check("问话清掉了（要收得重发）", "u-me" not in b._inbox_pending, b._inbox_pending)


def test_quota_other_words_do_not_answer(tmpdir, fake_claude):
    """说别的（包括「确认」）不算回答：问话留着，那条消息照旧往下走。"""
    _serve(PLAIN_IMG)
    b = _bridge_with_quota(tmpdir, fake_claude, 0.001)
    _seed_files(b, 2)
    _drain(b, [wecom_msg("", "qc", items=[image_item()])])
    _wait_sent(1)
    fake = _use_fake_recycle()
    _drain(b, [wecom_msg("/cc 确认", "qd")])
    b.queue.join()
    time.sleep(0.2)
    check("「确认」不算回答，问话还在",
          "u-me" in b._inbox_pending, b._inbox_pending)
    check("「确认」没引发删除", fake.gone == [], fake.gone)
    check("那条「确认」照旧交给 Claude 了",
          any("已处理" in t for t in texts()), texts())


def test_inbox_cfg_derived(tmpdir, fake_claude):
    """配额和问话时限也得从 config 派生出来（一处算、写到 st 里）。"""
    import wechat_bridge
    cfg = dict(wechat_bridge.DEFAULTS)
    cfg.update({"workdir": tmpdir, "claude_bin": fake_claude, "claude_extra_args": [],
                "app_launch": {"enable": False}, "media_control": {"enable": False},
                "screen_control": {"enable": False}, "power_control": {"enable": False},
                "meeting": {"enable": False}, "remind": {"enable": False},
                "inbox": {"enable": True, "dir": os.path.join(tmpdir, "收件"),
                          "max_mb": 5, "quota_mb": 2, "ask_seconds": 60}})
    b = wechat_bridge.Bridge(cfg)
    check("单文件上限", b.inbox_max_bytes == 5 * 1024 * 1024, b.inbox_max_bytes)
    check("总配额按 MB 换算", b.inbox_quota_bytes == 2 * 1024 * 1024,
          b.inbox_quota_bytes)
    check("问话时限", b.inbox_ask_seconds == 60, b.inbox_ask_seconds)
    check("等回答的那个表建起来了", b._inbox_pending == {}, b._inbox_pending)
    check("默认配额是 1 GB", bridge_config.DEFAULTS["inbox"]["quota_mb"] == 1024,
          bridge_config.DEFAULTS["inbox"])
