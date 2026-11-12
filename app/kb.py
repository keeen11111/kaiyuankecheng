"""知识库：SQLite FTS5 全文检索 + 降级 LIKE，不引入向量库或检索中间件。"""

import sqlite3                                                  # 捕获 FTS5 相关异常
from typing import Any                                          # 标注返回结构

from app.db import new_id, now_utc                             # 生成文档 ID 与时间戳
from app.response import BizError                              # 文档不存在时抛业务异常


def _fts_available(conn: sqlite3.Connection) -> bool:
    """探测当前连接对应的库是否存在 FTS5 虚表。

    入参:
        conn: 数据库连接。
    出参:
        True 表示可用全文索引，False 表示需降级为 LIKE。
    失败时:
        查询 sqlite_master 一般不失败；异常时按不可用处理，保证检索不中断。
    """
    try:                                                        # 保护元数据查询
        row = conn.execute(                                    # 查 sqlite_master 中是否有 kb_fts
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_fts'"  # 精确匹配表名
        ).fetchone()                                           # 取一行
        return row is not None                                 # 有表即可用
    except sqlite3.Error:                                      # 极端情况下元数据不可读
        return False                                           # 保守降级


def add_document(conn: sqlite3.Connection, title: str, content: str, source: str) -> dict:
    """写入一篇知识库文档，并同步全文索引。

    入参:
        conn: 数据库连接（调用方负责事务）。
        title: 文档标题。
        content: 文档正文。
        source: 来源标识。
    出参:
        {"doc_id", "title", "fts": bool}。
    失败时:
        写入失败抛 sqlite3.Error，由调用方事务回滚；重复标题不去重，交由调用方判断。
    """
    doc_id = new_id("doc")                                     # 生成唯一文档 ID
    conn.execute(                                              # 写入原文表
        "INSERT INTO kb_docs(doc_id,title,content,source,created_at) VALUES(?,?,?,?,?)",  # 占位符防注入
        (doc_id, title, content, source, now_utc()),          # 参数化绑定
    )                                                          # 插入结束
    fts = _fts_available(conn)                                 # 判断索引是否可用
    if fts:                                                    # 可用则同步写入虚表
        conn.execute(                                          # FTS5 虚表需要手工同步
            "INSERT INTO kb_fts(doc_id,title,content) VALUES(?,?,?)",  # 占位符
            (doc_id, title, content),                         # 与原文表内容一致
        )                                                      # 插入结束
    return {"doc_id": doc_id, "title": title, "fts": fts}     # 返回入库结果


def _escape_fts(query: str) -> str:
    """把用户输入包成 FTS5 短语，避免查询语法被注入。

    入参:
        query: 用户原始检索词。
    出参:
        形如 "xxx" 的短语表达式。
    失败时:
        输入中的双引号被替换成空格，不做静默截断。
    """
    safe = query.replace('"', " ").strip()                     # 去掉可能闭合短语的双引号
    return '"' + safe + '"'                                    # 用双引号包成整体短语，trigram 可匹配中文


def search(conn: sqlite3.Connection, query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """检索知识库，返回最相关的若干文档片段。

    入参:
        conn: 数据库连接。
        query: 检索词。
        top_k: 返回条数上限。
    出参:
        [{"doc_id","title","snippet","score"}]，按相关度排序。
    失败时:
        FTS5 语法或分词器异常时自动降级 LIKE；仍失败则抛 BizError("KB_SEARCH_FAILED")。
    """
    q = (query or "").strip()                                  # 去空白，避免空串触发 FTS 语法错误
    if not q:                                                  # 空查询直接返回空结果
        return []                                              # 不消耗数据库资源
    k = max(1, min(int(top_k), 20))                           # 把条数夹在 1~20，防止一次拉走整库
    if _fts_available(conn):                                   # 优先走全文索引
        try:                                                    # 某些输入仍可能不被 trigram 接受
            rows = conn.execute(                               # bm25 越小越相关
                "SELECT doc_id,title,content,bm25(kb_fts) AS score FROM kb_fts "
                "WHERE kb_fts MATCH ? ORDER BY score ASC LIMIT ?",  # 参数化 MATCH
                (_escape_fts(q), k),                          # 绑定短语与条数
            ).fetchall()                                       # 取回结果
            return [{"doc_id": r["doc_id"], "title": r["title"],  # 组装返回结构
                     "snippet": r["content"][:300], "score": round(float(r["score"]), 4)}  # 截断片段防响应过大
                    for r in rows]                             # 列表推导
        except sqlite3.Error:                                  # FTS5 不可用或表达式非法
            pass                                               # 落到下面的 LIKE 降级分支
    try:                                                        # 降级路径同样参数化
        rows = conn.execute(                                   # 用 LIKE 做子串匹配
            "SELECT doc_id,title,content FROM kb_docs WHERE title LIKE ? OR content LIKE ? LIMIT ?",  # 占位符
            (f"%{q}%", f"%{q}%", k),                          # 绑定通配模式
        ).fetchall()                                           # 取回结果
        return [{"doc_id": r["doc_id"], "title": r["title"],  # 组装相同结构，调用方无感知
                 "snippet": r["content"][:300], "score": 0.0}  # 降级路径无相关度分数
                for r in rows]                                 # 列表推导
    except sqlite3.Error as e:                                 # 数据库层彻底失败
        raise BizError("KB_SEARCH_FAILED", f"知识库检索失败：{type(e).__name__}")  # 不回显 SQL 细节


def get_document(conn: sqlite3.Connection, doc_id: str) -> dict:
    """按 ID 读取知识库原文。

    入参:
        conn: 数据库连接。
        doc_id: 文档 ID。
    出参:
        文档字典，含 doc_id/title/content/source/created_at。
    失败时:
        不存在时抛 BizError("KB_DOC_NOT_FOUND", 404)。
    """
    row = conn.execute(                                        # 按主键查询
        "SELECT doc_id,title,content,source,created_at FROM kb_docs WHERE doc_id=?",  # 占位符
        (doc_id,),                                             # 绑定 ID
    ).fetchone()                                               # 取一行
    if row is None:                                            # 未命中
        raise BizError("KB_DOC_NOT_FOUND", "知识库文档不存在", 404)  # 统一错误码
    return dict(row)                                           # 转成可序列化字典
