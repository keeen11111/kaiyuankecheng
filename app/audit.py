"""审计日志：哈希链防篡改，且严格不写入明文薪酬等敏感数值。"""

import hashlib                                                  # 计算 SHA-256 摘要与链式哈希
from typing import Any                                          # 标注载荷类型

from app.db import now_utc                                      # 统一 UTC 时间戳

_SENSITIVE_KEYS = {                                             # 任何出现在载荷里的这些键，都会被强制抹掉
    "base_salary_cents",                                       # 明文薪酬金额（分）
    "salary",                                                  # 明文薪酬金额（其他命名）
    "id_card",                                                 # 身份证号
    "bank_account",                                            # 银行卡号
    "token",                                                   # 身份令牌
    "pin",                                                     # 账号口令
    "api_key",                                                 # 模型网关密钥
}                                                              # 敏感键集合结束
_GENESIS = "0" * 64                                            # 创世哈希，第一条记录的前序哈希


def sanitize_payload(payload: dict | None) -> dict:
    """清洗审计载荷，把敏感值替换成掩码。

    入参:
        payload: 原始事件载荷字典，可为 None。
    出参:
        顶层敏感键被替换为 "***" 的新字典，不修改入参对象。
    失败时:
        payload 非字典时返回空字典，保证审计写入不被脏数据阻塞。
    """
    if not isinstance(payload, dict):                          # 只处理字典，其他类型一律视为空载荷
        return {}                                              # 返回空字典，避免下游遍历出错
    clean: dict[str, Any] = {}                                 # 存放清洗后的键值
    for key, value in payload.items():                         # 逐键检查
        clean[key] = "***" if key in _SENSITIVE_KEYS else value  # 命中敏感键则抹掉明文
    return clean                                               # 返回新字典


def _row_hash(ts: str, actor_id: str, action: str, target: str,
              payload_hash: str, prev_hash: str) -> str:
    """计算单条审计记录的哈希。

    入参:
        ts: 事件时间字符串。
        actor_id: 操作人。
        action: 动作名。
        target: 操作对象。
        payload_hash: 清洗后载荷的摘要。
        prev_hash: 上一条记录哈希。
    出参:
        64 位十六进制 SHA-256 字符串。
    失败时:
        任一参数非字符串时抛 TypeError，由上层兜底，不静默降级。
    """
    raw = "|".join([ts, actor_id, action, target, payload_hash, prev_hash])  # 用分隔符拼接，避免字段边界歧义
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()    # 统一 UTF-8 编码后取摘要


def append_audit(conn, actor_id: str, action: str, target: str,
                 payload: dict | None = None) -> str:
    """追加一条不可篡改的审计记录，必须在业务事务内调用。

    入参:
        conn: 当前事务中的数据库连接。
        actor_id: 操作人 ID。
        action: 动作名，如 CHANGE_CREATE / REVIEW_APPROVE / CHANGE_EFFECTIVE。
        target: 操作对象 ID（版本 ID、会话 ID 等）。
        payload: 事件补充信息，可为 None。
    出参:
        本条记录的 row_hash。
    失败时:
        数据库写入失败时抛 sqlite3.Error，事务整体回滚，业务不能只成功一半。
    """
    # 为什么卡在这里：薪酬是高敏数据，一旦把明文金额写进审计日志，
    # 任何有日志查看权限的人都能绕过接口脱敏直接看到金额，等于权限体系失效。
    clean = sanitize_payload(payload)                          # 先清洗，敏感值一律替换成掩码
    payload_hash = hashlib.sha256(                             # 只保存清洗后载荷的摘要
        ("" if not clean else str(clean)).encode("utf-8")     # 空载荷用空串，保证哈希稳定
    ).hexdigest()                                              # 得到 64 位摘要
    last = conn.execute(                                       # 取链尾记录
        "SELECT row_hash FROM audit_log ORDER BY seq DESC LIMIT 1"  # 按序号倒序取一条
    ).fetchone()                                               # 可能为空（首次写入）
    prev_hash = last["row_hash"] if last else _GENESIS         # 首次写入使用创世哈希
    ts = now_utc()                                             # 生成事件时间
    row_hash = _row_hash(ts, actor_id, action, target, payload_hash, prev_hash)  # 串联前序哈希
    conn.execute(                                              # 写入审计表，注意不含任何金额字段
        "INSERT INTO audit_log(ts,actor_id,action,target,payload_hash,prev_hash,row_hash) VALUES(?,?,?,?,?,?,?)",  # 占位符
        (ts, actor_id, action, target, payload_hash, prev_hash, row_hash),  # 参数化绑定，防 SQL 注入
    )                                                          # 插入结束
    return row_hash                                            # 返回本条哈希，便于调用方记录


def verify_chain(conn) -> dict:
    """校验整条审计链是否完整、未被篡改。

    入参:
        conn: 数据库连接。
    出参:
        {"ok": bool, "count": 记录数, "broken_at": 首个断裂的 seq 或 None}。
    失败时:
        发现断裂不抛异常而是返回 ok=False，让管理员能看到断点；数据库读失败则原样抛出。
    """
    rows = conn.execute(                                       # 按序号顺序取出全部审计记录
        "SELECT seq,ts,actor_id,action,target,payload_hash,prev_hash,row_hash FROM audit_log ORDER BY seq ASC"  # 全字段
    ).fetchall()                                               # 取回列表
    prev = _GENESIS                                            # 从创世哈希开始逐条比对
    for r in rows:                                             # 顺序遍历，顺序本身也是链的一部分
        expected = _row_hash(r["ts"], r["actor_id"], r["action"], r["target"],  # 用当前记录内容重算
                             r["payload_hash"], r["prev_hash"])  # 传入存储的前序哈希
        if r["prev_hash"] != prev or r["row_hash"] != expected:  # 前序不连续或内容被改都会命中
            return {"ok": False, "count": len(rows), "broken_at": r["seq"]}  # 返回首个断点
        prev = r["row_hash"]                                   # 更新链尾，继续下一条
    return {"ok": True, "count": len(rows), "broken_at": None}  # 全部通过


def list_audit(conn, limit: int = 100) -> list[dict]:
    """读取审计流水（不含载荷明文）。

    入参:
        conn: 数据库连接。
        limit: 最多返回条数，默认 100。
    出参:
        审计记录字典列表，按 seq 倒序。
    失败时:
        limit 非整数时由 SQLite 报错；调用方应传入已校验的整数。
    """
    rows = conn.execute(                                       # 查询最近若干条
        "SELECT seq,ts,actor_id,action,target,payload_hash,prev_hash,row_hash FROM audit_log ORDER BY seq DESC LIMIT ?",  # 占位符
        (int(limit),),                                         # 绑定上限，防止拼接 SQL
    ).fetchall()                                               # 取回结果
    return [dict(r) for r in rows]                             # Row 转成普通字典，便于 JSON 序列化
