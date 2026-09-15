"""main.py 单元测试:入典收录、去重、无引用提示、随机调用、图片回放、配置回退。"""
import sys
import types

from astrbot.api.message_components import At, Forward, Image, Node, Nodes, Plain

from astrbot_plugin_archiver import main as archiver_main

from conftest import (
    UMO_GROUP,
    UMO_OTHER,
    FakeContext,
    FakeEvent,
    collect,
    make_quote,
)
from conftest import make_reply, make_reply_event, write_png, _BASE


def _plugin_with_config(tmp_path, config):
    _BASE[0] = str(tmp_path)
    return archiver_main.ArchiverPlugin(FakeContext(), config=config)


class TestRudian:
    def test_archive_basic(self, plugin):
        event = make_reply_event(make_reply())
        results = collect(plugin.rudian(event))
        assert len(results) == 1
        kind, payload = results[0]
        # 默认以聊天记录(合并转发)形式回执:chain = [Nodes]
        assert kind == "chain"
        nodes_obj = payload[0]
        assert nodes_obj.nodes[0].name == "张三"
        assert nodes_obj.nodes[0].content[0].text == "哈哈哈哈"
        # 尾部节点为确认信息
        assert "已收录" in nodes_obj.nodes[-1].content[0].text
        assert "1 条" in nodes_obj.nodes[-1].content[0].text
        # 已写入语录库
        quotes = plugin._storage.session_quotes(UMO_GROUP)
        assert len(quotes) == 1
        q = quotes[0]
        assert q.sender_id == "20002"
        assert q.sender_name == "张三"
        assert q.text == "哈哈哈哈"
        assert q.message_id == "msg-001"
        assert q.archived_by == "10001"
        assert q.archived_by_name == "tester"
        assert q.session == UMO_GROUP

    def test_no_reply_prompt(self, plugin):
        event = make_reply_event(None)
        results = collect(plugin.rudian(event))
        assert "回复" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_empty_content_rejected(self, plugin):
        # 引用未回填内容(chain/message_str 均为空) → 收录失败
        reply = make_reply(chain=[], message_str="")
        event = make_reply_event(reply)
        results = collect(plugin.rudian(event))
        assert "无法读取" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_fallback_to_message_str(self, plugin):
        # 平台未回填 chain 时回退到引用解析出的纯文本
        reply = make_reply(chain=[], message_str="只有纯文本")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        assert plugin._storage.session_quotes(UMO_GROUP)[0].text == "只有纯文本"

    def test_sender_nickname_fallback_to_id(self, plugin):
        reply = make_reply(sender_nickname="", sender_id="20002")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_name == "20002"

    def test_dedup_same_message(self, plugin):
        event = make_reply_event(make_reply())
        collect(plugin.rudian(event))
        results = collect(plugin.rudian(event))
        assert "已经在语录库" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 1

    def test_message_with_image(self, plugin, tmp_path):
        # 设置 local_path → 模拟平台下载成功,图片应保存到本地
        img = Image(file="http://example.com/a.jpg", url="http://example.com/a.jpg")
        img.local_path = str(write_png(tmp_path / "a.png"))
        reply = make_reply(
            chain=[Plain("看图"), img],
            message_str="看图",
        )
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "看图"
        # 图片已下载保存到本地,并保留 URL 作为回退
        assert q.images[0]["path"].startswith("images/")
        assert (plugin._storage._base_dir / q.images[0]["path"]).is_file()
        assert q.images[0]["url"] == "http://example.com/a.jpg"
        assert "file" not in q.images[0]

    def test_archive_image_download_failure_falls_back_to_url(self, plugin):
        # 未设置 local_path → 模拟下载失败 → 回退为仅记录 URL(与旧版行为一致)
        img = Image(file="http://example.com/a.jpg", url="http://example.com/a.jpg")
        reply = make_reply(chain=[img], message_str="")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.images == [
            {"file": "http://example.com/a.jpg", "url": "http://example.com/a.jpg"}
        ]
        assert "path" not in q.images[0]

    def test_non_text_components_to_placeholder(self, plugin):
        # 表情/语音等以占位符记录(模拟不存在的组件类型)
        face = types.SimpleNamespace(type="Face", id=1)
        record = types.SimpleNamespace(type="Record", file="x")
        at = types.SimpleNamespace(type="At", qq="30003", name="李四")
        reply = make_reply(chain=[face, record, at], message_str="")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[表情]\n[语音]\n@李四"

    def test_session_limit_from_config(self, tmp_path):
        plugin = _plugin_with_config(tmp_path, {"session_quote_limit": 2})
        for i in range(4):
            reply = make_reply(id=f"m{i}", message_str="x", chain=[Plain("x")])
            event = make_reply_event(reply)
            collect(plugin.rudian(event))
        quotes = plugin._storage.session_quotes(UMO_GROUP)
        assert [q.message_id for q in quotes] == ["m2", "m3"]

    def test_archive_forward_disabled(self, tmp_path):
        # 关闭 use_forward → 收录回执回退为纯文本
        plugin = _plugin_with_config(tmp_path, {"use_forward": False})
        event = make_reply_event(make_reply())
        results = collect(plugin.rudian(event))
        assert results[0][0] == "plain"
        assert "已收录" in results[0][1] and "1 条" in results[0][1]

    def test_archive_with_at_owner(self, plugin):
        # /入典 跟随 At → 归属以 At 指定的人为准(以 QQ 号保存)
        event = FakeEvent(message=[At(qq="30003", name="李四"), make_reply()])
        results = collect(plugin.rudian(event))
        assert len(results) == 1
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_id == "30003"
        assert q.sender_name == "李四"
        # 收录内容仍为被回复消息的内容
        assert q.text == "哈哈哈哈"

    def test_archive_at_self_excluded(self, plugin):
        # @Bot 唤醒的自身 At 不影响归属(默认=回复消息发送者)
        event = FakeEvent(message=[At(qq="99999"), make_reply()], self_id="99999")
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_id == "20002"
        assert q.sender_name == "张三"

    def test_archive_at_without_name(self, plugin):
        # At 无昵称 → 归属显示名回退为 QQ 号
        event = FakeEvent(message=[At(qq="30003"), make_reply()])
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_id == "30003"
        assert q.sender_name == "30003"

    def test_archive_at_owner_overrides_chat_record(self, plugin):
        # 聊天记录 + At → At 显式指定覆盖"最后一条消息发送者"默认规则
        event = FakeEvent(
            message=[
                At(qq="40004", name="王五"),
                make_reply(chain=[Forward(id="fwd-1")], message_str=""),
            ],
            bot_responses=TestForward.FORWARD_RESPONSES,
        )
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_id == "40004"
        assert q.sender_name == "王五"
        # 聊天记录本体仍结构化保存
        assert q.text == "[聊天记录]"
        assert len(q.forward_nodes) == 2


class TestLaidiandian:
    def test_empty_library_prompt(self, plugin):
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert "语录库还是空的" in results[0][1]

    def test_random_replay_text(self, plugin):
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        # chain = [Nodes],语录以聊天记录(合并转发)形式呈现
        nodes_obj = results[0][1][0]
        assert nodes_obj.nodes[0].name == "张三"
        assert "哈哈哈哈" in nodes_obj.nodes[0].content[0].text

    def test_random_replay_with_image(self, plugin):
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"file": "", "url": "http://example.com/a.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        node = results[0][1][0].nodes[0]
        assert len(node.content) == 2
        img = node.content[1]
        assert img.file == "http://example.com/a.jpg"

    def test_replay_includes_archive_info_node(self, plugin):
        # 收录人/时间有效时附带"语录档案"信息节点
        import time as _time

        plugin._storage.add_quote(
            UMO_GROUP, make_quote(archived_at_ts=_time.time())
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        nodes_obj = results[0][1][0]
        assert len(nodes_obj.nodes) == 2
        assert nodes_obj.nodes[1].name == "语录档案"
        assert "收录人：tester" in nodes_obj.nodes[1].content[0].text

    def test_forward_disabled_falls_back_to_text(self, tmp_path):
        # 关闭 use_forward → 回退为文本形式(chain[Plain+Image])
        plugin = _plugin_with_config(tmp_path, {"use_forward": False})
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"file": "", "url": "http://example.com/a.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        chain = results[0][1]
        assert len(chain) == 2
        assert "张三" in chain[0].text
        assert chain[1].file == "http://example.com/a.jpg"

    def test_unsupported_platform_falls_back_to_text(self, plugin):
        # 不支持自建合并转发的平台(如 webchat)自动回退为文本输出,避免输出被忽略
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        event = make_reply_event(None, platform_name="webchat")
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        chain = results[0][1]
        assert "张三" in chain[0].text

    def test_unsupported_platform_archive_falls_back_to_text(self, plugin):
        # 不支持合并转发的平台上,收录回执同样回退为纯文本
        event = make_reply_event(make_reply(), platform_name="webchat")
        results = collect(plugin.rudian(event))
        assert results[0][0] == "plain"
        assert "已收录" in results[0][1]

    def test_replay_prefers_url_over_file(self, plugin):
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"file": "local.jpg", "url": "http://example.com/b.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        assert results[0][1][0].nodes[0].content[1].file == "http://example.com/b.jpg"

    def test_replay_prefers_local_archive(self, plugin, tmp_path):
        # 本地存档存在 → 优先发送本地文件而非 URL
        img = write_png(tmp_path / "local.png")
        rel = plugin._storage.store_image(str(img))
        assert rel is not None
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(images=[{"path": rel, "url": "http://example.com/a.jpg"}]),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        node = results[0][1][0].nodes[0]
        assert len(node.content) == 2
        assert node.content[1].file == str((plugin._storage._base_dir / rel).resolve())

    def test_replay_falls_back_to_url_when_local_missing(self, plugin):
        # 本地存档文件不存在 → 回退发送 URL
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(
                images=[
                    {"path": "images/ghost.png", "url": "http://example.com/a.jpg"}
                ]
            ),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        node = results[0][1][0].nodes[0]
        assert len(node.content) == 2
        assert node.content[1].file == "http://example.com/a.jpg"

    def test_fallback_global_disabled_by_default(self, plugin):
        # 其他会话有典,本会话为空;默认不开全局回退 → 提示语录库为空
        plugin._storage.add_quote(UMO_OTHER, make_quote())
        event = make_reply_event(None, umo="aiocqhttp:GroupMessage:current")
        results = collect(plugin.laidiandian(event))
        assert "语录库还是空的" in results[0][1]

    def test_fallback_global_enabled(self, tmp_path):
        plugin = _plugin_with_config(tmp_path, {"fallback_global": True})
        plugin._storage.add_quote(UMO_OTHER, make_quote())
        event = make_reply_event(None, umo="aiocqhttp:GroupMessage:current")
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        # 聊天记录形式:发送人昵称在节点上,内容为语录文本
        nodes_obj = results[0][1][0]
        assert nodes_obj.nodes[0].name == "张三"
        assert nodes_obj.nodes[0].content[0].text == "哈哈哈哈"


class TestForward:
    # 模拟 OneBot get_forward_msg 响应(结构化子消息)
    FORWARD_RESPONSES = {
        "get_forward_msg": {
            "data": {
                "messages": [
                    {
                        "sender": {"nickname": "张三", "user_id": "20002"},
                        "message": [
                            {"type": "text", "data": {"text": "今天天气真好"}}
                        ],
                    },
                    {
                        "sender": {"nickname": "李四", "user_id": "30003"},
                        "message": [
                            {"type": "text", "data": {"text": "是啊"}},
                            {
                                "type": "image",
                                "data": {"url": "http://example.com/a.jpg"},
                            },
                        ],
                    },
                ]
            }
        }
    }

    def test_forward_stored_as_chat_record(self, plugin):
        # 引用内容为聊天记录(Forward) → 以聊天记录结构化保存,不展开为文本
        reply = make_reply(chain=[Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply, bot_responses=self.FORWARD_RESPONSES)
        results = collect(plugin.rudian(event))
        assert len(results) == 1
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[聊天记录]"
        # 归属为聊天记录最后一条消息的发送者(以 QQ 号保存)
        assert q.sender_id == "30003"
        assert q.sender_name == "李四"
        assert len(q.forward_nodes) == 2
        assert q.forward_nodes[0]["sender_name"] == "张三"
        assert q.forward_nodes[0]["sender_id"] == "20002"
        assert q.forward_nodes[0]["text"] == "今天天气真好"
        assert q.forward_nodes[1]["sender_name"] == "李四"
        assert q.forward_nodes[1]["text"] == "是啊"
        # 子消息图片保留 URL(下载失败回退)
        assert (
            q.forward_nodes[1]["images"][0]["url"] == "http://example.com/a.jpg"
        )
        # 回执:原样回显引用的聊天记录 + 最后收录信息(而非 [聊天记录] 占位符)
        kind, payload = results[0]
        assert kind == "chain"
        nodes_obj = payload[0]
        assert len(nodes_obj.nodes) == 3
        assert nodes_obj.nodes[0].name == "张三"
        assert nodes_obj.nodes[0].content[0].text == "今天天气真好"
        assert nodes_obj.nodes[1].name == "李四"
        assert nodes_obj.nodes[1].content[0].text == "是啊"
        assert nodes_obj.nodes[-1].name == "语录档案"
        assert "已收录" in nodes_obj.nodes[-1].content[0].text
        # 回执中不出现占位符
        for node in nodes_obj.nodes:
            for comp in node.content:
                assert getattr(comp, "text", "") != "[聊天记录]"

    def test_forward_extract_failure_falls_back_to_placeholder(self, plugin):
        # 拉取失败(平台无 bot 接口/接口失败) → 回退 [转发消息] 占位符
        reply = make_reply(chain=[Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply)  # 无 bot 接口,无 forward_text
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[转发消息]"
        assert q.forward_nodes == []

    def test_forward_placeholder_result_falls_back(self, plugin):
        # 展开文本回退结果仍为占位符 → 回退 [转发消息] 占位符
        reply = make_reply(chain=[Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply, forward_text="[转发消息]")
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[转发消息]"
        assert q.forward_nodes == []

    def test_nodes_with_embedded_content_stored(self, plugin):
        # 引用内容为 Node/Nodes(内嵌 content) → 结构化保存,不展开为文本
        node = Node(content=[Plain("天气真好")], name="张三", uin="20002")
        reply = make_reply(chain=[Nodes(nodes=[node])], message_str="")
        event = make_reply_event(reply)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[聊天记录]"
        assert len(q.forward_nodes) == 1
        assert q.forward_nodes[0]["sender_name"] == "张三"
        assert q.forward_nodes[0]["text"] == "天气真好"

    def test_mixed_text_and_forward(self, plugin):
        # 文本 + 聊天记录混合 → 外层文本保留,聊天记录结构化保存
        reply = make_reply(chain=[Plain("看这个"), Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply, bot_responses=self.FORWARD_RESPONSES)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "看这个\n[聊天记录]"
        assert len(q.forward_nodes) == 2

    def test_forward_owner_is_last_sender(self, plugin):
        # 归属:聊天记录取其中最后一条消息的发送者(以 QQ 号保存)
        reply = make_reply(chain=[Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply, bot_responses=self.FORWARD_RESPONSES)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_id == "30003"  # 最后一条消息发送者的 QQ 号
        assert q.sender_name == "李四"

    def test_forward_owner_falls_back_when_sender_id_missing(self, plugin):
        # 子消息无 QQ 号 → 归属回退为回复消息的发送者
        responses = {
            "get_forward_msg": {
                "data": {
                    "messages": [
                        {
                            "sender": {"nickname": "无名"},
                            "message": [
                                {"type": "text", "data": {"text": "无号码"}}
                            ],
                        },
                    ]
                }
            }
        }
        reply = make_reply(chain=[Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply, bot_responses=responses)
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.sender_id == "20002"  # 回复消息发送者
        assert q.sender_name == "无名"

    def test_forward_replay_keeps_chat_record(self, plugin):
        # 回放:聊天记录典保持聊天记录形态,最后一条为收录信息节点
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(
                text="[聊天记录]",
                forward_nodes=[
                    {
                        "sender_id": "20002",
                        "sender_name": "张三",
                        "text": "今天天气真好",
                        "images": [],
                    },
                    {
                        "sender_id": "30003",
                        "sender_name": "李四",
                        "text": "是啊",
                        "images": [],
                    },
                ],
                archived_at_ts=1757800000,
            ),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        nodes_obj = results[0][1][0]
        # 2 条子消息节点 + 1 条收录信息节点
        assert len(nodes_obj.nodes) == 3
        assert nodes_obj.nodes[0].name == "张三"
        assert nodes_obj.nodes[0].content[0].text == "今天天气真好"
        assert nodes_obj.nodes[1].name == "李四"
        # 最后为收录信息节点
        assert nodes_obj.nodes[-1].name == "语录档案"
        assert "收录人" in nodes_obj.nodes[-1].content[0].text

    def test_forward_replay_mixed_outer_text(self, plugin):
        # 回放:外层文本作为第一条节点(被收录者身份),后接聊天记录与收录信息
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(
                text="看这个\n[聊天记录]",
                forward_nodes=[
                    {
                        "sender_id": "20002",
                        "sender_name": "张三",
                        "text": "今天天气真好",
                        "images": [],
                    },
                ],
                archived_at_ts=1757800000,
            ),
        )
        event = make_reply_event(None)
        results = collect(plugin.laidiandian(event))
        nodes_obj = results[0][1][0]
        assert len(nodes_obj.nodes) == 3
        # 第一条为外层文本节点(被收录者身份)
        assert nodes_obj.nodes[0].name == "张三"
        assert nodes_obj.nodes[0].content[0].text == "看这个"
        # 中间为聊天记录子消息
        assert nodes_obj.nodes[1].content[0].text == "今天天气真好"
        # 最后为收录信息
        assert nodes_obj.nodes[-1].name == "语录档案"

    def test_forward_unsupported_platform_falls_back(self, plugin):
        # 非 QQ 平台引用聊天记录 → 回退 [转发消息] 占位符
        reply = make_reply(chain=[Forward(id="fwd-1")], message_str="")
        event = make_reply_event(reply, platform_name="webchat")
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q.text == "[转发消息]"
        assert q.forward_nodes == []


class TestOwner:
    def _seed_two_quotes(self, plugin):
        plugin._storage.add_quote(UMO_GROUP, make_quote(message_id="m1"))  # 张三 20002
        plugin._storage.add_quote(
            UMO_GROUP,
            make_quote(sender_id="30003", sender_name="李四", message_id="m2"),
        )

    def test_extract_by_at(self, plugin):
        # /来点典 @李四 → 抽取李四的语录
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[At(qq="30003", name="李四")])
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"
        nodes_obj = results[0][1][0]
        assert nodes_obj.nodes[0].name == "李四"
        assert nodes_obj.nodes[0].uin == "30003"

    def test_extract_by_name(self, plugin):
        # /来点典 李四 → 按昵称抽取李四的语录
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[])
        results = collect(plugin.laidiandian(event, "李四"))
        nodes_obj = results[0][1][0]
        assert nodes_obj.nodes[0].name == "李四"

    def test_extract_by_name_partial_match(self, plugin):
        # 昵称包含匹配:/来点典 张 → 匹配张三
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[])
        results = collect(plugin.laidiandian(event, "张"))
        nodes_obj = results[0][1][0]
        assert nodes_obj.nodes[0].name == "张三"

    def test_extract_owner_not_found(self, plugin):
        # 指定人没有典 → 提示
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[])
        results = collect(plugin.laidiandian(event, "赵六"))
        assert results[0][0] == "plain"
        assert "还没有" in results[0][1] and "赵六" in results[0][1]

    def test_extract_by_at_not_found(self, plugin):
        # At 指定的人没有典 → 提示
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[At(qq="88888", name="赵六")])
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "plain"
        assert "还没有" in results[0][1] and "88888" in results[0][1]

    def test_at_self_excluded(self, plugin):
        # @Bot 唤醒的自身 At 被排除 → 无目标,随机抽取
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[At(qq="99999")], self_id="99999")
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"

    def test_no_owner_random(self, plugin):
        # 无指定 → 随机抽取(现有行为)
        self._seed_two_quotes(plugin)
        event = FakeEvent(message=[])
        results = collect(plugin.laidiandian(event))
        assert results[0][0] == "chain"

    def test_owner_extract_global_fallback(self, tmp_path):
        # 会话内无该归属的语录,fallback_global 开启 → 从所有会话抽取
        plugin = _plugin_with_config(tmp_path, {"fallback_global": True})
        plugin._storage.add_quote(
            UMO_OTHER,
            make_quote(sender_id="30003", sender_name="李四", message_id="m2"),
        )
        event = FakeEvent(message=[])
        results = collect(plugin.laidiandian(event, "李四"))
        assert results[0][0] == "chain"
        assert results[0][1][0].nodes[0].name == "李四"


class TestShandian:
    def test_sent_message_recorded_via_platform_api(self, plugin):
        # QQ 平台直接调用平台 API 发送语录消息 → 记录映射,回执不经框架输出
        responses = {
            **TestForward.FORWARD_RESPONSES,
            "send_group_forward_msg": {"data": {"message_id": "sent-100"}},
        }
        event = make_reply_event(make_reply(), bot_responses=responses)
        results = collect(plugin.rudian(event))
        assert results == []  # 直接平台发送,无框架输出
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        # 映射已记录:语录消息 message_id → 典
        assert (
            plugin._storage.find_quote_id_by_sent_message(UMO_GROUP, "sent-100")
            == q.id
        )

    def test_delete_by_sent_mapping(self, plugin):
        # 回复 bot 发送的语录消息 → 按映射精确删除(不依赖 get_forward_msg)
        q = make_quote()
        plugin._storage.add_quote(UMO_GROUP, q)
        plugin._storage.record_sent_message(UMO_GROUP, "sent-200", q.id)
        event = FakeEvent(message=[make_reply(id="sent-200", chain=[])], is_admin=True)
        results = collect(plugin.shandian(event))
        assert "已删除" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_delete_cleans_sent_mapping(self, plugin):
        # 删除典后映射记录同步清理
        q = make_quote()
        plugin._storage.add_quote(UMO_GROUP, q)
        plugin._storage.record_sent_message(UMO_GROUP, "sent-300", q.id)
        plugin._storage.delete_quote(UMO_GROUP, q.id)
        plugin._storage.delete_sent_message(UMO_GROUP, q.id)
        assert (
            plugin._storage.find_quote_id_by_sent_message(UMO_GROUP, "sent-300")
            is None
        )

    def test_delete_by_reply_text_receipt(self, tmp_path):
        # 文本回执(use_forward=False)也嵌入编号 → 回复文本回执删除
        plugin = _plugin_with_config(tmp_path, {"use_forward": False})
        event = make_reply_event(make_reply())
        collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        # 回复 bot 的文本回执(内容含编号),适配器回填到 Reply.chain/message_str
        receipt = (
            f"已收录「张三」的发言，本会话语录库共 1 条（编号：{q.id[:8]}）"
        )
        del_event = FakeEvent(
            message=[make_reply(id="text-1", chain=[Plain(receipt)], message_str=receipt)],
            is_admin=True,
        )
        results = collect(plugin.shandian(del_event))
        assert "已删除" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_delete_by_reply_original_message(self, plugin):
        # 回复被收录的原始消息 → 按原消息 ID(message_id)查找并删除
        event = make_reply_event(
            make_reply(id="orig-1"),
            bot_responses={
                **TestForward.FORWARD_RESPONSES,
                "send_group_forward_msg": {"data": {"message_id": "sent-100"}},
            },
        )
        collect(plugin.rudian(event))
        # 回复群友的原始消息(orig-1)删除,无需 bot 接口
        del_event = FakeEvent(message=[make_reply(id="orig-1", chain=[])], is_admin=True)
        results = collect(plugin.shandian(del_event))
        assert "已删除" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_delete_by_reply_original_message_not_found(self, plugin):
        # 回复的原始消息未被收录 → 提示
        event = make_reply_event(
            make_reply(id="orig-1"),
            bot_responses={
                **TestForward.FORWARD_RESPONSES,
                "send_group_forward_msg": {"data": {"message_id": "sent-100"}},
            },
        )
        collect(plugin.rudian(event))
        del_event = FakeEvent(message=[make_reply(id="orig-999", chain=[])], is_admin=True)
        results = collect(plugin.shandian(del_event))
        assert results[0][0] == "plain"
        assert "请回复" in results[0][1] or "编号" in results[0][1]

    def test_delete_by_reply(self, plugin):
        # 收录 → 回执含编号 → 回复语录消息(get_forward_msg 返回含编号文本) → /删典
        event = make_reply_event(
            make_reply(),
            bot_responses={
                "get_forward_msg": {
                    "data": {
                        "messages": [
                            {
                                "sender": {"nickname": "张三", "user_id": "20002"},
                                "message": [
                                    {
                                        "type": "text",
                                        "data": {"text": "哈哈哈哈"},
                                    }
                                ],
                            },
                            {
                                "sender": {"nickname": "入典成功"},
                                "message": [
                                    {
                                        "type": "text",
                                        "data": {"text": "已收录，本会话语录库共 1 条（编号：abcdef12）"},
                                    }
                                ],
                            },
                        ]
                    }
                }
            },
        )
        results = collect(plugin.rudian(event))
        q = plugin._storage.session_quotes(UMO_GROUP)[0]
        assert q is not None
        # 回复 bot 发送的语录消息(Reply.id 为合并转发消息 ID)
        del_event = FakeEvent(
            message=[make_reply(id="sent-1", chain=[])],
            is_admin=True,
            bot_responses={
                "get_forward_msg": {
                    "data": {
                        "messages": [
                            {
                                "sender": {"nickname": "入典成功"},
                                "message": [
                                    {
                                        "type": "text",
                                        "data": {
                                            "text": "已收录，本会话语录库共 1 条（编号："
                                            + q.id[:8]
                                            + "）"
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )
        results2 = collect(plugin.shandian(del_event))
        assert results2[0][0] == "plain"
        assert "已删除" in results2[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_delete_by_code(self, plugin):
        # /删除 <编号> → 直接删除
        q = make_quote()
        plugin._storage.add_quote(UMO_GROUP, q)
        event = FakeEvent(message=[], is_admin=True)
        results = collect(plugin.shandian(event, q.id[:8]))
        assert "已删除" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_delete_code_not_found(self, plugin):
        plugin._storage.add_quote(UMO_GROUP, make_quote())
        event = FakeEvent(message=[], is_admin=True)
        results = collect(plugin.shandian(event, "ffffffff"))
        assert "没有找到" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 1

    def test_delete_no_target(self, plugin):
        # 既未回复语录消息也无编号 → 使用提示
        event = FakeEvent(message=[], is_admin=True)
        results = collect(plugin.shandian(event))
        assert "请回复" in results[0][1] or "编号" in results[0][1]

    def test_reply_not_a_quote_message(self, plugin):
        # 回复的消息拉不到编号(非语录消息) → 提示
        event = make_reply_event(
            make_reply(id="sent-2", chain=[]),
            is_admin=True,
            bot_responses={
                "get_forward_msg": {
                    "data": {
                        "messages": [
                            {
                                "sender": {"nickname": "张三"},
                                "message": [
                                    {"type": "text", "data": {"text": "普通消息"}}
                                ],
                            }
                        ]
                    }
                }
            },
        )
        results = collect(plugin.shandian(event))
        assert results[0][0] == "plain"
        assert "请回复" in results[0][1] or "编号" in results[0][1]


class TestPermission:
    def test_upload_permission_denied(self, tmp_path):
        # upload_permission=Bot管理员 → 非 Bot 管理员拒绝上传
        plugin = _plugin_with_config(tmp_path, {"upload_permission": "Bot管理员"})
        event = make_reply_event(make_reply())
        results = collect(plugin.rudian(event))
        assert "权限" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_upload_permission_admin_allowed(self, tmp_path):
        # upload_permission=Bot管理员 → Bot 管理员允许上传
        plugin = _plugin_with_config(tmp_path, {"upload_permission": "Bot管理员"})
        event = make_reply_event(make_reply(), is_admin=True)
        results = collect(plugin.rudian(event))
        assert plugin._storage.session_count(UMO_GROUP) == 1

    def test_delete_permission_denied(self, tmp_path):
        # delete_permission=Bot管理员 → 非 Bot 管理员拒绝删除
        plugin = _plugin_with_config(tmp_path, {"delete_permission": "Bot管理员"})
        q = make_quote()
        plugin._storage.add_quote(UMO_GROUP, q)
        event = FakeEvent(message=[make_reply(id=q.message_id, chain=[])])
        results = collect(plugin.shandian(event))
        assert "权限" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 1

    def test_delete_permission_admin_allowed(self, tmp_path):
        # delete_permission=Bot管理员 → Bot 管理员允许删除
        plugin = _plugin_with_config(tmp_path, {"delete_permission": "Bot管理员"})
        q = make_quote()
        plugin._storage.add_quote(UMO_GROUP, q)
        event = FakeEvent(
            message=[make_reply(id=q.message_id, chain=[])], is_admin=True
        )
        results = collect(plugin.shandian(event))
        assert "已删除" in results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 0

    def test_default_permission(self, plugin):
        # 默认:上传=群员(所有人可上传),删除=管理员(非管理员拒绝删除)
        event = make_reply_event(make_reply())
        results = collect(plugin.rudian(event))
        assert plugin._storage.session_count(UMO_GROUP) == 1
        del_event = FakeEvent(message=[make_reply(id="msg-001", chain=[])])
        del_results = collect(plugin.shandian(del_event))
        assert "权限" in del_results[0][1]
        assert plugin._storage.session_count(UMO_GROUP) == 1


class TestConfig:
    def test_defaults_without_config(self, plugin):
        assert plugin._cfg_bool("fallback_global", False) is False
        assert plugin._cfg_int("session_quote_limit", 0) == 0

    def test_invalid_values_fallback(self, tmp_path):
        plugin = _plugin_with_config(
            tmp_path, {"session_quote_limit": "abc", "fallback_global": "yes"}
        )
        # 非法整型回退默认值;非空字符串转 bool 为 True
        assert plugin._cfg_int("session_quote_limit", 0) == 0
        assert plugin._cfg_bool("fallback_global", False) is True


class TestMetadata:
    def test_version_consistency(self):
        import re
        from pathlib import Path

        meta = (
            Path(__file__).resolve().parent.parent / "metadata.yaml"
        ).read_text(encoding="utf-8")
        version = re.search(r"^version:\s*v?([0-9.]+)", meta, re.M).group(1)
        assert archiver_main.PLUGIN_VERSION == version
        assert "astrbot_plugin_archiver" in meta
