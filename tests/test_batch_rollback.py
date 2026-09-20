"""POST /batches/{batch_id}/rollback-preview：批次撤销预演（只读）。

模拟撤销某批次首次归档且当前仍归属该批次的有效邮件：按保留期清理的
压缩语义在内存中为存活子孙重挂最近存活祖先，返回待删除 ID、父节点/
根节点将变化的存活邮件、撤销后的根分组与汇总；快照中的历史祖先、
环内 invalid 邮件与更晚批次重归档的同 ID 均不参与；预演不写库。
"""

from __future__ import annotations


def _m(mid, irt=None, refs=None, sent="2026-09-18T10:00:00+00:00", subject="hello",
       retain_until=None):
    msg = {
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
        "sent_at": sent,
        "subject": subject,
    }
    if retain_until is not None:
        msg["retain_until"] = retain_until
    return msg


def _preview(client, batch_id):
    return client.post(f"/batches/{batch_id}/rollback-preview")


def test_rollback_preview_unknown_batch_returns_404(client):
    r = _preview(client, "ghost-batch")
    assert r.status_code == 404
    assert r.json()["detail"] == "batch not found"


def test_rollback_preview_deletes_whole_batch_when_nothing_else(client):
    batch = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()

    r = _preview(client, batch["batch_id"])
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == batch["batch_id"]
    assert body["submitted_at"] == batch["submitted_at"]
    assert body["deleted_ids"] == ["A", "B", "C"]
    assert body["affected"] == []
    assert body["root_groups"] == []
    assert body["summary"] == {
        "total": 3, "deleted": 3, "surviving": 0, "affected": 0, "roots": 0,
    }


def test_rollback_preview_compacts_survivors_like_retention_cleanup(client):
    # batch1：R -> A；batch2：C 挂 A 下、E 挂 C 下、D 挂 R 下
    batch1 = client.post("/batches", json={"messages": [
        _m("R", sent="2026-09-18T08:00:00+00:00"),
        _m("A", irt="R", refs=["R"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    client.post("/batches", json={"messages": [
        _m("C", irt="A", refs=["R", "A"], sent="2026-09-18T10:00:00+00:00"),
        _m("E", irt="C", refs=["C"], sent="2026-09-18T10:30:00+00:00"),
        _m("D", irt="R", refs=["R"], sent="2026-09-18T11:00:00+00:00"),
    ]})

    body = _preview(client, batch1["batch_id"]).json()
    # 仅删除 batch1 首次归档的 R、A；C/D/E 存活并按清理压缩语义重挂
    assert body["deleted_ids"] == ["A", "R"]
    assert [i["message_id"] for i in body["affected"]] == ["C", "D", "E"]
    by_id = {i["message_id"]: i for i in body["affected"]}
    # C：父 A 被删，父链上无存活祖先 -> 成为新根
    assert by_id["C"] == {
        "message_id": "C", "old_parent_id": "A", "new_parent_id": None,
        "old_root_id": "R", "new_root_id": "C",
    }
    # D：父 R 被删 -> 成为新根
    assert by_id["D"] == {
        "message_id": "D", "old_parent_id": "R", "new_parent_id": None,
        "old_root_id": "R", "new_root_id": "D",
    }
    # E：父 C 存活不变，仅根 R -> C
    assert by_id["E"] == {
        "message_id": "E", "old_parent_id": "C", "new_parent_id": "C",
        "old_root_id": "R", "new_root_id": "C",
    }
    # 撤销后的根分组：C 一棵（含 E）、D 一棵
    assert body["root_groups"] == [
        {"root_id": "C", "members": ["C", "E"]},
        {"root_id": "D", "members": ["D"]},
    ]
    assert body["summary"] == {
        "total": 5, "deleted": 2, "surviving": 3, "affected": 3, "roots": 2,
    }


def test_rollback_preview_keeps_historical_ancestors(client):
    # batch1 归档历史祖先 A；batch2 的快照树整棵挂在 A 下（含历史祖先）
    client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
    ]})
    batch2 = client.post("/batches", json={"messages": [
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    assert [t["message_id"] for t in batch2["threads"]] == ["A"]

    body = _preview(client, batch2["batch_id"]).json()
    # 历史祖先 A 归属更早批次，不随 batch2 撤销；仅 B、C 在删除集
    assert body["deleted_ids"] == ["B", "C"]
    assert body["affected"] == []
    assert body["root_groups"] == [{"root_id": "A", "members": ["A"]}]
    assert body["summary"] == {
        "total": 3, "deleted": 2, "surviving": 1, "affected": 0, "roots": 1,
    }


def test_rollback_preview_ignores_cycle_invalid_messages(client):
    batch = client.post("/batches", json={"messages": [
        _m("Z", sent="2026-09-18T08:00:00+00:00"),
        _m("x", irt="y", refs=["y"], sent="2026-09-18T09:00:00+00:00"),
        _m("y", irt="x", refs=["x"], sent="2026-09-18T10:00:00+00:00"),
    ]}).json()
    assert batch["totals"]["invalid"] == 2

    body = _preview(client, batch["batch_id"]).json()
    # 环内 invalid 邮件不属于有效森林，不参与撤销、不计入汇总
    assert body["deleted_ids"] == ["Z"]
    assert body["affected"] == []
    assert body["root_groups"] == []
    assert body["summary"] == {
        "total": 1, "deleted": 1, "surviving": 0, "affected": 0, "roots": 0,
    }
    # 预演后环内邮件仍在库中、仍为 invalid
    assert client.get("/messages/x").json()["is_invalid"] is True
    assert client.get("/messages/y").json()["is_invalid"] is True


def test_rollback_preview_all_cycle_batch_is_empty(client):
    # 整批成环：没有可撤销的有效邮件
    batch = client.post("/batches", json={"messages": [
        _m("x", irt="y", refs=["y"]),
        _m("y", irt="x", refs=["x"]),
    ]}).json()
    assert batch["threads"] == []

    body = _preview(client, batch["batch_id"]).json()
    assert body["deleted_ids"] == []
    assert body["affected"] == []
    assert body["root_groups"] == []
    assert body["summary"] == {
        "total": 0, "deleted": 0, "surviving": 0, "affected": 0, "roots": 0,
    }


def test_rollback_preview_ignores_ids_reused_by_later_batches(client):
    # batch1：M（带保留期）-> K；清理删除 M 后，batch2 以同 ID 重归档 M
    batch1 = client.post("/batches", json={"messages": [
        _m("M", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
        _m("K", irt="M", refs=["M"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    client.post("/retention/cleanups", json={"cutoff": "2026-09-20T00:00:00+00:00"})
    batch2 = client.post("/batches", json={"messages": [
        _m("root2", sent="2026-09-20T08:00:00+00:00"),
        _m("M", irt="root2", refs=["root2"], sent="2026-09-20T09:00:00+00:00"),
    ]}).json()
    assert client.get("/messages/M").json()["batch_id"] == batch2["batch_id"]

    body = _preview(client, batch1["batch_id"]).json()
    # M 当前归属更晚批次 -> 不随 batch1 撤销；仅 K 在删除集
    assert body["deleted_ids"] == ["K"]
    assert body["affected"] == []
    assert body["root_groups"] == [{"root_id": "root2", "members": ["M", "root2"]}]
    assert body["summary"] == {
        "total": 3, "deleted": 1, "surviving": 2, "affected": 0, "roots": 1,
    }

    # batch2 自己的预演：root2 与 M 都在删除集，K 存活自成一根
    body2 = _preview(client, batch2["batch_id"]).json()
    assert body2["deleted_ids"] == ["M", "root2"]
    assert body2["affected"] == []
    assert body2["root_groups"] == [{"root_id": "K", "members": ["K"]}]
    assert body2["summary"] == {
        "total": 3, "deleted": 2, "surviving": 1, "affected": 0, "roots": 1,
    }


def test_rollback_preview_uses_current_topology_after_correction(client):
    # batch1：A -> B；batch2：Q；纠偏把 B（连同子树）移到 Q 下；batch3：W 挂 B 下
    batch1 = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    client.post("/batches", json={"messages": [
        _m("Q", sent="2026-09-18T07:00:00+00:00"),
    ]})
    rev = client.get("/threads/B").json()["revision"]
    r = client.post("/thread-corrections", json={
        "message_id": "B", "new_parent_id": "Q", "expected_revision": rev,
    })
    assert r.status_code == 200
    client.post("/batches", json={"messages": [
        _m("W", irt="B", refs=["B"], sent="2026-09-18T10:00:00+00:00"),
    ]})

    body = _preview(client, batch1["batch_id"]).json()
    assert body["deleted_ids"] == ["A", "B"]
    # W 的最近存活祖先是 B 的当前父节点 Q（纠偏后的拓扑），而非快照中的 A
    assert body["affected"] == [{
        "message_id": "W", "old_parent_id": "B", "new_parent_id": "Q",
        "old_root_id": "Q", "new_root_id": "Q",
    }]
    assert body["root_groups"] == [{"root_id": "Q", "members": ["Q", "W"]}]
    assert body["summary"] == {
        "total": 4, "deleted": 2, "surviving": 2, "affected": 1, "roots": 1,
    }


def test_rollback_preview_after_batch_fully_cleaned_up(client):
    # 批次内邮件已被保留期清理全部删除：删除集为空，森林也为空
    batch = client.post("/batches", json={"messages": [
        _m("M", sent="2026-09-18T08:00:00+00:00",
           retain_until="2026-09-19T00:00:00+00:00"),
    ]}).json()
    client.post("/retention/cleanups", json={"cutoff": "2026-09-20T00:00:00+00:00"})
    assert client.get("/messages/M").status_code == 404

    body = _preview(client, batch["batch_id"]).json()
    assert body["deleted_ids"] == []
    assert body["affected"] == []
    assert body["root_groups"] == []
    assert body["summary"] == {
        "total": 0, "deleted": 0, "surviving": 0, "affected": 0, "roots": 0,
    }


def test_rollback_preview_is_read_only(client):
    batch = client.post("/batches", json={"messages": [
        _m("A", sent="2026-09-18T08:00:00+00:00"),
        _m("B", irt="A", refs=["A"], sent="2026-09-18T09:00:00+00:00"),
    ]}).json()
    client.post("/batches", json={"messages": [
        _m("C", irt="B", refs=["A", "B"], sent="2026-09-18T10:00:00+00:00"),
    ]})

    snap_before = client.get(f"/batches/{batch['batch_id']}").json()
    msg_before = client.get("/messages/A").json()
    thread_before = client.get("/threads/A").json()
    batches_before = client.get("/batches").json()

    first = _preview(client, batch["batch_id"])
    assert first.status_code == 200
    # 重复预演结果一致
    assert _preview(client, batch["batch_id"]).json() == first.json()

    # 预演不写库：批次快照、邮件、会话树、批次列表均不变
    assert client.get(f"/batches/{batch['batch_id']}").json() == snap_before
    assert client.get("/messages/A").json() == msg_before
    assert client.get("/threads/A").json() == thread_before
    assert client.get("/batches").json() == batches_before
